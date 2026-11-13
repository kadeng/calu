"""What calu stores about the database it uses: where it is, how to log in, how it is run.

`calu setup` offers three variants (PLAN.md D205):

    variant     the database is ...                          the login authenticates with ...
    docker      a PostgreSQL container calu starts itself    a password calu generated
    server      a PostgreSQL server that exists already      a password
    rds-iam     an AWS RDS instance                          an IAM token (the session login
                                                             too, unless setup was told
                                                             `--session-auth password`)

A `Target` is one login of one such database. It is the secret of an entry of the credential
store (`calu.credentials`), so everything in it is encrypted and authenticated by the entry's
password: no unauthenticated file decides which database a session trusts, which directory a
container mounts or which port it listens on.

The secret has two forms:

- Form 2, written by `calu setup`: a JSON object, `Target.dump()`.
- Form 1, written by earlier versions and by `calu credential set`: a libpq connection string
  or URL. It is read as the variant `server`, except the string an earlier `calu db up` wrote
  (`calu.db.local.owner_url`: a loopback host, the database `calu`, one of calu's three role
  names and no `require_auth`), which is read as the variant `docker` on the named volume.
"""

import json
from typing import Any, Literal, Self

import anyio.to_thread
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, Field, ValidationError

FORMAT = 2
"""The number in `calu_db` of a form 2 secret."""

VOLUME_DATA = "volume:calu-db_data"
"""`DockerBlock.data` of a database in Docker's named volume (what `calu db up` created before
a data directory could be chosen)."""

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
"""Host names that mean this machine."""

LEGACY_DOCKER_USERS = frozenset({"calu_owner", "calu_session", "calu_operator"})
"""The role names of calu's own container, for recognising a form 1 secret of it."""

TOKEN_TIMEOUT_S = 15.0
"""How long making one IAM token may take (`Target.conninfo_now`); resolving an AWS profile can
wait for the network."""

INHERITED = ("host", "hostaddr", "port", "dbname", "sslmode", "sslrootcert", "require_auth")
"""The connection parameters a login for another role takes over (`Target.for_role`), and the
only ones besides `user` that a form 2 secret may hold."""

Variant = Literal["docker", "server", "rds-iam"]


class TargetError(ValueError):
    """A stored database record cannot be read, or a requested one makes no sense."""


class _Model(BaseModel):
    """Base of the stored models: immutable, and unknown fields are an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DockerBlock(_Model):
    """How calu runs the container of the variant `docker`."""

    port: int = Field(ge=1, le=65535)
    """The port on 127.0.0.1 the container publishes."""
    data: str
    """Where the cluster's files are: an absolute host directory, or `VOLUME_DATA`."""


class PasswordAuth(_Model):
    """A login with a password."""

    method: Literal["password"] = "password"
    password: str


class RdsIamAuth(_Model):
    """A login with a token that AWS signs for an IAM identity (`calu.db.rds`)."""

    method: Literal["rds-iam"] = "rds-iam"
    region: str
    """The AWS region of the instance."""
    profile: str
    """The AWS profile whose identity may connect; never the default credential chain."""


class Target(_Model):
    """One login of calu's database (see the module docstring)."""

    variant: Variant
    conninfo: dict[str, str]
    """libpq parameters without `password`. In a form 2 secret only `user` and `INHERITED`."""
    auth: PasswordAuth | RdsIamAuth = Field(discriminator="method")
    docker: DockerBlock | None = None
    """Set for the variant `docker` only."""
    schema_name: str = Field(default="public", alias="schema")
    """The schema that holds calu's tables. Only "public" is supported; the field keeps a
    schema per installation possible without a new format."""

    @classmethod
    def parse(cls, secret: str | bytes | bytearray) -> Self:
        """Read a stored secret in either form.

        Args:
            secret: The secret of a credential store entry. A `bytearray` is not changed.

        Returns:
            The target.

        Raises:
            TargetError: If the secret is neither a form 2 object this version understands
                nor a libpq connection string, or if it names a schema other than "public".
        """
        text = secret if isinstance(secret, str) else bytes(secret).decode("utf-8", "replace")
        if text.lstrip().startswith("{"):
            return cls._from_json(text)
        try:
            params = {key: str(value) for key, value in conninfo_to_dict(text).items()}
        except psycopg.ProgrammingError as exc:
            raise TargetError(f"the stored database record is no connection string: {exc}") from exc
        password = params.pop("password", "")
        docker = None
        # What an earlier `calu db up` stored, and nothing newer: this version writes form 2
        # for its own container, and the strings it hands out for it carry `require_auth`.
        legacy_docker = (
            params.get("host") in LOOPBACK
            and params.get("dbname") == "calu"
            and params.get("user") in LEGACY_DOCKER_USERS
            and "require_auth" not in params
            and (params.get("port") or "5432").isdigit()
        )
        if legacy_docker:
            docker = DockerBlock(port=int(params.get("port") or 5432), data=VOLUME_DATA)
        try:
            return cls(
                variant="docker" if docker else "server",
                conninfo=params,
                auth=PasswordAuth(password=password),
                docker=docker,
            )
        except ValidationError as exc:
            raise TargetError(f"the stored database record is invalid: {exc}") from exc

    @classmethod
    def _from_json(cls, text: str) -> Self:
        """Read a form 2 secret.

        Args:
            text: The JSON text.

        Returns:
            The target.

        Raises:
            TargetError: As `parse`.
        """
        try:
            data: Any = json.loads(text)
        except ValueError as exc:
            raise TargetError(f"the stored database record is damaged: {exc}") from exc
        if not isinstance(data, dict) or data.pop("calu_db", None) != FORMAT:
            raise TargetError(
                "the stored database record has a format this version of calu does not know; "
                "install the calu that wrote it, or run `calu setup` again"
            )
        try:
            target = cls.model_validate(data)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_input=False, include_url=False)
            )
            raise TargetError(f"the stored database record is invalid: {problems}") from exc
        unknown = sorted(set(target.conninfo) - {"user", *INHERITED})
        if unknown:
            raise TargetError(
                f"the stored database record holds parameters calu does not store: {unknown}"
            )
        target._check()
        return target

    def _check(self) -> None:
        """Refuse a combination of fields that no setup writes.

        Raises:
            TargetError: For a schema other than "public", a `docker` block on another
                variant or none on `docker`, and IAM authentication on another variant than
                `rds-iam`.
        """
        if self.schema_name != "public":
            raise TargetError("only the schema `public` is supported in this version of calu")
        if (self.variant == "docker") != (self.docker is not None):
            raise TargetError("the stored database record mixes the Docker variant with another")
        if isinstance(self.auth, RdsIamAuth) and self.variant != "rds-iam":
            raise TargetError("IAM authentication belongs to the variant rds-iam only")

    def dump(self) -> bytes:
        """Return the form 2 secret of this target.

        Returns:
            UTF-8 JSON. It contains the password of a password login, so it goes nowhere but
            into `CredentialStore.save`.
        """
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        return json.dumps({"calu_db": FORMAT, **data}, sort_keys=True).encode()

    def for_role(self, user: str, auth: PasswordAuth | RdsIamAuth) -> Self:
        """Describe the login of another role of the same database.

        Only where the database is and how its server is verified are taken over (`INHERITED`)
        and how the container is run; nothing else of this login, in particular no parameter
        that a hand-written connection string of the owner carried.

        Args:
            user: The role that logs in.
            auth: How it authenticates.

        Returns:
            The target of that role.

        Raises:
            TargetError: If this login finds its server through a libpq service file
                (`service=`): such an entry can also carry a user and a password, so the
                place alone cannot be taken over from it.
        """
        if "service" in self.conninfo:
            raise TargetError(
                "the stored owner login names a libpq service; calu needs host, port and "
                "database spelled out to derive the logins of its roles (`calu credential set "
                "admin` with an explicit connection string)"
            )
        conninfo = {key: self.conninfo[key] for key in INHERITED if key in self.conninfo}
        conninfo["user"] = user
        return type(self)(
            variant=self.variant if isinstance(auth, RdsIamAuth) else self._password_variant(),
            conninfo=conninfo,
            auth=auth,
            docker=self.docker,
        )

    def _password_variant(self) -> Variant:
        """Name the variant of a password login of this database.

        Returns:
            "docker" for calu's own container, else "server": a role with a password on an RDS
            instance is an ordinary server login.
        """
        return "docker" if self.docker is not None else "server"

    @property
    def user(self) -> str:
        """The role this target logs in as.

        Returns:
            The `user` parameter; "" if the record names none (libpq then uses the OS user).
        """
        return self.conninfo.get("user", "")

    @property
    def place(self) -> str:
        """Where the database is, for messages.

        Returns:
            `host:port/dbname` with what the record names; never a secret.
        """
        host = self.conninfo.get("host", "localhost")
        port = self.conninfo.get("port", "5432")
        return f"{host}:{port}/{self.conninfo.get('dbname', self.user)}"

    async def conninfo_now(self) -> str:
        """Build the connection string for one connection attempt.

        Side effects: for IAM authentication, asks AWS's local signing code for a token in a
        worker thread (`calu.db.rds.mint_token`); resolving the profile may use the network.
        The token is valid for new logins for 15 minutes and is not kept.

        Returns:
            A libpq connection string with the password or token. It must not be logged.

        Raises:
            psycopg.OperationalError: If no token can be obtained (an expired AWS sign-in, an
                unknown profile, no answer within `TOKEN_TIMEOUT_S`). Not `LoginRefused`: the
                server was not asked.
        """
        if isinstance(self.auth, PasswordAuth):
            return make_conninfo("", **self.conninfo, password=self.auth.password)
        from calu.db import rds

        auth = self.auth
        host = self.conninfo.get("host", "")
        port = int(self.conninfo.get("port") or 5432)
        try:
            # Bounded, and abandoned when the caller is cancelled: a profile that waits for
            # the network must not hold up a reconnect's own limit or a shutdown.
            with anyio.fail_after(TOKEN_TIMEOUT_S):
                token = await anyio.to_thread.run_sync(
                    rds.mint_token,
                    host,
                    port,
                    self.user,
                    auth.region,
                    auth.profile,
                    abandon_on_cancel=True,
                )
        except TimeoutError as exc:
            raise psycopg.OperationalError(
                f"cannot get an RDS login token with the AWS profile {auth.profile!r}: "
                f"no answer within {TOKEN_TIMEOUT_S:.0f} s"
            ) from exc
        except Exception as exc:
            raise psycopg.OperationalError(
                f"cannot get an RDS login token with the AWS profile {auth.profile!r}: {exc}"
            ) from exc
        return make_conninfo("", **self.conninfo, password=token)

    def libpq_string(self) -> str:
        """Return this password login as a libpq connection string (a form 1 secret).

        For handing a login to another machine (`calu credential new operator` prints it, and
        `calu credential set` stores it there).

        Returns:
            The connection string with the password.

        Raises:
            TargetError: If this login does not use a password.
        """
        if not isinstance(self.auth, PasswordAuth):
            raise TargetError("a login with IAM authentication has no connection string")
        return make_conninfo("", **self.conninfo, password=self.auth.password)

    def __repr__(self) -> str:
        """Return a representation without the password.

        Returns:
            The variant, the role and the place.
        """
        return f"Target({self.variant}, {self.user}@{self.place}, {self.auth.method})"

    __str__ = __repr__


def from_fields(
    host: str,
    port: int,
    dbname: str,
    user: str,
    auth: PasswordAuth | RdsIamAuth,
    sslmode: str | None = None,
    sslrootcert: str | None = None,
) -> Target:
    """Build the target of a server that exists already, from what `calu setup` asked.

    Without `sslmode`, a host other than this machine gets `sslmode=require` (encrypted, the
    server's certificate not verified) and a loopback host or socket path gets libpq's default.
    IAM authentication always gets `verify-full`, because the token is sent as a password.

    Args:
        host: Host name, address or socket directory of the server.
        port: Its port.
        dbname: The database.
        user: The role that logs in.
        auth: How it authenticates.
        sslmode: A libpq `sslmode` to use instead of the derived one.
        sslrootcert: The file with the certificate authority that signs the server's
            certificate; needed for `verify-ca` and `verify-full`.

    Returns:
        The target, of the variant `rds-iam` for IAM authentication and `server` otherwise.

    Raises:
        TargetError: If a field is empty or the port is out of range, if `verify-ca` or
            `verify-full` is asked for without `sslrootcert`, or if IAM authentication is
            combined with another `sslmode` than `verify-full`.
    """
    if not host or not dbname or not user:
        raise TargetError("host, database and user must not be empty")
    if not 1 <= port <= 65535:
        raise TargetError(f"{port} is no port number")
    iam = isinstance(auth, RdsIamAuth)
    if iam:
        if sslmode not in (None, "verify-full"):
            raise TargetError("IAM authentication needs sslmode verify-full")
        sslmode = "verify-full"
    elif sslmode is None and host not in LOOPBACK and not host.startswith("/"):
        sslmode = "require"
    if sslmode in ("verify-ca", "verify-full") and not sslrootcert:
        raise TargetError(f"sslmode {sslmode} needs the CA file (--sslrootcert)")
    conninfo = {"host": host, "port": str(port), "dbname": dbname, "user": user}
    if sslmode:
        conninfo["sslmode"] = sslmode
    if sslrootcert:
        conninfo["sslrootcert"] = sslrootcert
    return Target(variant="rds-iam" if iam else "server", conninfo=conninfo, auth=auth)
