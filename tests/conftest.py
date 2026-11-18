import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest

# Textual's test driver waits for the app to be idle by sleeping in steps of 1/50 s, about
# 2800 times in one of the client's test files. A shorter step saves over a minute of the unit
# layer (measured 2026-11-18: the six `test_tui_*` files 169 s -> 95 s). The attribute is
# private to Textual: if a release renames it, the assertion says so instead of the tests
# silently getting slow again.
import textual._wait as _textual_wait
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic_ai import models

from calu.config import PROVIDER_KEYS_BY_PREFIX, load_secrets
from calu.credentials import CredentialStore, KdfParams

assert hasattr(_textual_wait, "SLEEP_GRANULARITY"), "Textual renamed its idle sleep step"
_textual_wait.SLEEP_GRANULARITY = 0.004

# Keep every test, and every `calu` process a test starts, away from
# `/var/lib/calu/database.json` of the machine the tests run on: where calu is set up for
# root, that file names the real database, and after `calu setup --no-password` it holds a
# login for it (D228).
os.environ["CALU_PLACE_FILE"] = "/nonexistent/calu-tests/database.json"

load_secrets()
"""Checker API keys from `~/.config/calu/.env`, for the live tests."""

LIVE_CHECKER_MODEL = "fireworks:accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b"
"""Default checker model of live tests: follows instructions far better than Mistral Small."""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Block real model requests unless the run selects live tests (`-m live`)."""
    markexpr = config.getoption("markexpr") or ""
    models.ALLOW_MODEL_REQUESTS = "live" in markexpr and "not live" not in markexpr


@pytest.fixture
def checker_test_model() -> str:
    """The checker model for live tests: `CALU_TEST_CHECKER_MODEL` or Nemotron Lightning.

    Skips the test if no key of the model's provider is set. Mistral models: small ones only
    (see AGENTS.md, never Medium or Large).
    """
    model = os.environ.get("CALU_TEST_CHECKER_MODEL", LIVE_CHECKER_MODEL)
    keys = PROVIDER_KEYS_BY_PREFIX.get(model.partition(":")[0], ())
    if keys and not any(os.environ.get(k) for k in keys):
        pytest.skip(f"{' or '.join(keys)} not set")
    return model


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
        # Roles belong to the whole test server, not to one test database. A test that ran
        # `calu setup` leaves `calu_admin` with a login, and `calu web` then offers the
        # password login instead of its token (D216): each test starts without that login.
        await admin.execute(
            "DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'calu_admin') THEN "
            "ALTER ROLE calu_admin NOLOGIN; END IF; END $$"
        )
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        params = conninfo_to_dict(base)
        params["dbname"] = name
        yield make_conninfo("", **params)
    finally:
        await admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        await admin.close()
