"""Login tokens for an AWS RDS instance with IAM database authentication (PLAN.md D205).

The owner login of the variant `rds-iam` uses them (`calu.db.target`): `calu setup`, the admin
commands and `calu web`. By default the session login `calu_session` uses them too (it is made a
member of `rds_iam`), so `calu run` needs the session's AWS profile and makes a token for every
connection and reconnect. With `calu setup --session-auth password` a session logs in with a
generated password instead and needs no AWS credential.

A token is a signed request, made locally with the credentials of one AWS profile; it is valid
for new logins for 15 minutes and is sent as the password, which is why the connection must
verify the server's certificate (`sslmode=verify-full`).

This module is the only place that imports boto3, and `token_provider` and `identity_provider`
are what a test replaces.
"""

import re
from collections.abc import Callable

_ENDPOINT = re.compile(
    r"\.(?:(?P<a>[a-z]{2}(?:-[a-z]+)+-\d+)\.rds\.amazonaws\.com"
    r"|rds\.(?P<b>cn(?:-[a-z]+)+-\d+)\.amazonaws\.com\.cn)\.?$"
)
"""The region in an RDS endpoint name: `<name>.<id>.<region>.rds.amazonaws.com`, and in the
China regions `<name>.<id>.rds.<region>.amazonaws.com.cn`; a trailing dot is accepted."""


def region_of(host: str) -> str | None:
    """Read the AWS region out of an RDS endpoint name.

    Args:
        host: The host name.

    Returns:
        The region, e.g. "eu-central-1", or None if `host` is not an endpoint name as AWS
        writes it.
    """
    found = _ENDPOINT.search(host.lower())
    return (found.group("a") or found.group("b")) if found else None


def _boto_token(host: str, port: int, user: str, region: str, profile: str) -> str:
    """Sign a login token with boto3.

    Args:
        host: The instance's endpoint, exactly as AWS names it.
        port: Its port.
        user: The database role.
        region: The instance's region.
        profile: The AWS profile that signs.

    Returns:
        The token.
    """
    import boto3

    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("rds").generate_db_auth_token(
        DBHostname=host, Port=port, DBUsername=user, Region=region
    )


def _boto_identity(profile: str | None) -> str | None:
    """Ask AWS who the credentials of a profile are.

    Args:
        profile: The AWS profile; None for the default credential chain.

    Returns:
        The ARN of the caller, or None if there are no usable credentials.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        return str(session.client("sts").get_caller_identity()["Arn"])
    except BotoCoreError, ClientError:
        return None


token_provider: Callable[[str, int, str, str, str], str] = _boto_token
"""Makes a token for (host, port, user, region, profile); tests replace it."""

identity_provider: Callable[[str | None], str | None] = _boto_identity
"""Names the AWS identity of a profile (None: the default chain); tests replace it."""


def mint_token(host: str, port: int, user: str, region: str, profile: str) -> str:
    """Make a login token for one connection attempt.

    Side effects: reads the AWS configuration of the profile; resolving it (single sign-on, an
    assumed role) may use the network. The signing itself is local. Blocking: call it in a
    worker thread.

    Args:
        host: The instance's endpoint, exactly as AWS names it.
        port: Its port.
        user: The database role, which must have the role `rds_iam`.
        region: The instance's region.
        profile: The AWS profile whose identity may connect.

    Returns:
        The token, to be sent as the password.

    Raises:
        Exception: Whatever boto3 raises for a profile that does not exist or whose sign-in
            has expired; `Target.conninfo_now` turns it into `psycopg.OperationalError`.
    """
    return token_provider(host, port, user, region, profile)


def caller_arn(profile: str | None) -> str | None:
    """Name the AWS identity of a profile, or of the default credential chain.

    Side effects: one request to AWS STS. Blocking.

    Args:
        profile: The AWS profile; None for the credentials every program on the machine gets
            without naming a profile.

    Returns:
        The ARN, or None if no credentials are usable.
    """
    return identity_provider(profile)
