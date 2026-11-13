"""The stored record of a database login, in both of its forms (PLAN.md D205)."""

import anyio
import psycopg
import pytest

from calu.db import rds
from calu.db.target import (
    VOLUME_DATA,
    DockerBlock,
    PasswordAuth,
    RdsIamAuth,
    Target,
    TargetError,
    from_fields,
)


def test_a_connection_string_of_an_existing_server_is_read_as_such() -> None:
    target = Target.parse("postgresql://admin:s3cret@db.example.org:6432/calu?sslmode=verify-full")
    assert target.variant == "server" and target.docker is None
    assert target.conninfo == {
        "host": "db.example.org",
        "port": "6432",
        "dbname": "calu",
        "user": "admin",
        "sslmode": "verify-full",
    }
    assert target.auth == PasswordAuth(password="s3cret")
    assert (target.user, target.place) == ("admin", "db.example.org:6432/calu")
    again = Target.parse(bytearray(b"host=h dbname=d user=u password=p"))
    assert Target.parse(again.libpq_string()) == again, "a password login is a string again"


def test_what_an_earlier_calu_db_up_stored_is_the_local_database_in_its_volume() -> None:
    """Existing installations need no conversion: their record names calu's own container."""
    target = Target.parse("postgresql://calu_owner:pw@127.0.0.1:5433/calu")
    assert target.variant == "docker"
    assert target.docker == DockerBlock(port=5433, data=VOLUME_DATA)
    # The same host with another database or user is somebody's own server.
    assert Target.parse("postgresql://calu_owner:pw@127.0.0.1:5433/other").variant == "server"
    assert Target.parse("postgresql://me:pw@127.0.0.1:5433/calu").variant == "server"


def test_the_new_form_round_trips_and_shows_no_secret() -> None:
    owner = Target(
        variant="docker",
        conninfo={"host": "127.0.0.1", "port": "5433", "dbname": "calu", "user": "calu_owner"},
        auth=PasswordAuth(password="s3cret"),
        docker=DockerBlock(port=5433, data="/var/lib/calu/db"),
    )
    stored = owner.dump()
    assert stored.startswith(b"{") and b'"calu_db": 2' in stored and b'"schema": "public"' in stored
    assert Target.parse(stored) == owner
    assert "s3cret" not in repr(owner) and "s3cret" not in str(owner)
    assert repr(owner) == "Target(docker, calu_owner@127.0.0.1:5433/calu, password)"


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("{not json", "damaged"),
        ('{"calu_db": 3, "variant": "server"}', "format this version of calu does not know"),
        (
            '{"calu_db": 2, "variant": "cloud", "conninfo": {}, "auth": {"method": "password",'
            ' "password": "p"}}',
            "invalid",
        ),
        (
            '{"calu_db": 2, "variant": "server", "conninfo": {"host": "h", "options": "-c x=1"},'
            ' "auth": {"method": "password", "password": "p"}}',
            "does not store",
        ),
        (
            '{"calu_db": 2, "variant": "server", "conninfo": {"host": "h"}, "auth": {"method":'
            ' "password", "password": "p"}, "schema": "team_a"}',
            "only the schema `public`",
        ),
        (
            '{"calu_db": 2, "variant": "server", "conninfo": {"host": "h"}, "auth": {"method":'
            ' "password", "password": "p"}, "docker": {"port": 1, "data": "/d"}}',
            "mixes",
        ),
        (
            '{"calu_db": 2, "variant": "server", "conninfo": {"host": "h"}, "auth": {"method":'
            ' "rds-iam", "region": "r", "profile": "p"}}',
            "rds-iam only",
        ),
        ("not a = connection = string", "no connection string"),
    ],
)
def test_a_record_this_version_cannot_use_is_refused_with_a_reason(
    secret: str, message: str
) -> None:
    with pytest.raises(TargetError, match=message):
        Target.parse(secret)


def test_another_role_takes_over_where_the_database_is_and_nothing_else() -> None:
    """The session login was built by copying every parameter of the owner's string."""
    owner = Target.parse(
        "host=db port=5432 dbname=calu user=boss password=pw sslmode=require "
        "options='-c role=postgres' application_name=mine"
    )
    session = owner.for_role("calu_session", PasswordAuth(password="other"))
    assert session.conninfo == {
        "host": "db",
        "port": "5432",
        "dbname": "calu",
        "sslmode": "require",
        "user": "calu_session",
    }
    assert session.variant == "server" and Target.parse(session.dump()) == session
    local = Target.parse("postgresql://calu_owner:pw@127.0.0.1:5433/calu")
    assert local.for_role("calu_session", PasswordAuth(password="x")).docker == local.docker


def test_the_questions_of_setup_become_a_record_with_a_sensible_tls_mode() -> None:
    password = PasswordAuth(password="pw")
    assert "sslmode" not in from_fields("127.0.0.1", 5432, "calu", "u", password).conninfo
    assert "sslmode" not in from_fields("/var/run/postgresql", 5432, "calu", "u", password).conninfo
    remote = from_fields("db.example.org", 5432, "calu", "u", password)
    assert remote.conninfo["sslmode"] == "require" and remote.variant == "server"
    assert from_fields("db", 5432, "c", "u", password, "disable").conninfo["sslmode"] == "disable"
    with pytest.raises(TargetError, match="needs the CA file"):
        from_fields("db", 5432, "c", "u", password, "verify-full")
    with pytest.raises(TargetError, match="must not be empty"):
        from_fields("", 5432, "c", "u", password)
    with pytest.raises(TargetError, match="no port number"):
        from_fields("db", 70000, "c", "u", password)
    iam = RdsIamAuth(region="eu-central-1", profile="calu")
    with pytest.raises(TargetError, match="needs the CA file"):
        from_fields("x.rds.amazonaws.com", 5432, "c", "u", iam)
    rds_target = from_fields("x.rds.amazonaws.com", 5432, "c", "u", iam, None, "/ca.pem")
    assert rds_target.variant == "rds-iam" and rds_target.conninfo["sslmode"] == "verify-full"
    with pytest.raises(TargetError, match="verify-full"):
        from_fields("x.rds.amazonaws.com", 5432, "c", "u", iam, "require", "/ca.pem")
    with pytest.raises(TargetError, match="no connection string"):
        rds_target.libpq_string()


def test_a_login_with_iam_gets_a_new_token_for_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, int, str, str, str]] = []

    def provider(host: str, port: int, user: str, region: str, profile: str) -> str:
        asked.append((host, port, user, region, profile))
        return f"token-{len(asked)}"

    monkeypatch.setattr(rds, "token_provider", provider)
    target = from_fields(
        "calu.abc.eu-central-1.rds.amazonaws.com",
        5432,
        "calu",
        "calu_session",
        RdsIamAuth(region="eu-central-1", profile="calu-prod"),
        None,
        "/etc/calu/db-ca.pem",
    )
    first = anyio.run(target.conninfo_now)
    second = anyio.run(target.conninfo_now)
    assert "password=token-1" in first and "password=token-2" in second
    assert "sslmode=verify-full" in first and "sslrootcert=/etc/calu/db-ca.pem" in first
    assert asked[0] == (
        "calu.abc.eu-central-1.rds.amazonaws.com",
        5432,
        "calu_session",
        "eu-central-1",
        "calu-prod",
    )
    assert b"token" not in target.dump(), "a token is never stored"

    def expired(*_args: object) -> str:
        raise RuntimeError("the SSO session has expired")

    monkeypatch.setattr(rds, "token_provider", expired)
    # Not a refused login: the server was never asked, so nothing gives up for good.
    with pytest.raises(psycopg.OperationalError, match="cannot get an RDS login token"):
        anyio.run(target.conninfo_now)


def test_only_what_an_earlier_calu_wrote_is_taken_for_calus_own_container() -> None:
    """A string this version hands out for the local database (it carries require_auth) was
    read as the old named volume, and a stopped database was then "started" from the wrong
    place. And an owner record that finds its server through a service file cannot be taken
    over: the session login would point somewhere else."""
    handed_out = (
        "host=127.0.0.1 port=5433 dbname=calu require_auth=scram-sha-256 "
        "user=calu_operator password=p"
    )
    assert Target.parse(handed_out).variant == "server"
    by_address = Target.parse("hostaddr=10.0.0.5 dbname=calu user=boss password=p")
    assert by_address.for_role("calu_session", PasswordAuth(password="x")).conninfo == {
        "hostaddr": "10.0.0.5",
        "dbname": "calu",
        "user": "calu_session",
    }
    with pytest.raises(TargetError, match="names a libpq service"):
        Target.parse("service=calu").for_role("calu_session", PasswordAuth(password="x"))
    # A port that is no number is somebody's string, not calu's own container.
    assert Target.parse("host=127.0.0.1 port=x dbname=calu user=calu_owner").variant == "server"
    with pytest.raises(TargetError, match="invalid"):
        Target.parse('{"calu_db": 2, "variant": "docker"}')


def test_the_region_is_read_from_an_endpoint_name() -> None:
    assert rds.region_of("calu.abc.rds.cn-north-1.amazonaws.com.cn") == "cn-north-1"
    assert rds.region_of("calu.abc.rds.cn-northwest-1.amazonaws.com.cn") == "cn-northwest-1"
    assert rds.region_of("calu.c9akciq32.eu-central-1.rds.amazonaws.com.") == "eu-central-1"
    assert rds.region_of("calu.c9akciq32.eu-central-1.rds.amazonaws.com") == "eu-central-1"
    assert rds.region_of("db.cluster-x.us-gov-west-1.rds.amazonaws.com") == "us-gov-west-1"
    assert rds.region_of("db.example.org") is None


def test_a_refused_iam_token_never_ends_a_session_for_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three refused logins end a session, because a password that is wrong stays wrong. A
    refused IAM token is different: the AWS sign-in may have expired, and the next attempt
    signs a new token once the person has signed in again."""
    from calu.db import connection
    from calu.db.connection import ConnectionSource, LoginRefused

    async def refused_connect(_conninfo: str) -> psycopg.AsyncConnection:
        raise psycopg.OperationalError("password authentication failed")

    async def answer(self: ConnectionSource, conninfo: str | None = None) -> str:
        return "28P01"

    monkeypatch.setattr(connection, "connect", refused_connect)
    monkeypatch.setattr(ConnectionSource, "refusal", answer)
    monkeypatch.setattr(rds, "token_provider", lambda *_args: "an-expired-token")

    with pytest.raises(LoginRefused):
        anyio.run(ConnectionSource("host=db dbname=calu user=calu_session password=old").open)

    iam = from_fields(
        "calu.abc.eu-central-1.rds.amazonaws.com",
        5432,
        "calu",
        "calu_session",
        RdsIamAuth(region="eu-central-1", profile="calu-prod"),
        None,
        "/ca.pem",
    )
    with pytest.raises(psycopg.OperationalError, match="sign-in may have expired") as raised:
        anyio.run(ConnectionSource(iam).open)
    assert not isinstance(raised.value, LoginRefused)
    assert "an-expired-token" not in str(raised.value)
