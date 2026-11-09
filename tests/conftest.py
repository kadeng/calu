import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from calu.credentials import CredentialStore, KdfParams

TEST_KDF = KdfParams(time_cost=1, memory_cost=8, parallelism=1)
"""Cheap Argon2id parameters, so tests stay fast."""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(tmp_path / "calu" / "credentials.json", kdf=TEST_KDF)


@pytest.fixture
async def db_url() -> AsyncIterator[str]:
    """A fresh, empty database for one test; dropped afterwards.

    Needs `CALU_TEST_DSN` (owner connection of the test server, see docker-compose.yml);
    default: the `caludb` service as seen from the host.
    """
    base = os.environ.get("CALU_TEST_DSN", "postgresql://calu_owner:calu-dev@127.0.0.1:55432/calu")
    name = f"calu_test_{uuid.uuid4().hex[:12]}"
    try:
        admin = await psycopg.AsyncConnection.connect(base, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"test database not reachable: {exc}")
    try:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        params = conninfo_to_dict(base)
        params["dbname"] = name
        yield make_conninfo("", **params)
    finally:
        await admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        await admin.close()
