import os
import uuid
from collections.abc import AsyncIterator, Iterator
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
    """Block real model requests unless the run selects live tests (`-m live`), and skip the
    fuzz tests unless it selects them (`-m fuzz`), also when a `-m` given on the command line
    replaces the default one (as in CI)."""
    markexpr = config.getoption("markexpr") or ""
    models.ALLOW_MODEL_REQUESTS = "live" in markexpr and "not live" not in markexpr
    if "fuzz" not in markexpr or "not fuzz" in markexpr:
        skip = pytest.mark.skip(reason="randomized load; run with `-m fuzz`")
        for item in items:
            if item.get_closest_marker("fuzz") is not None:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def fresh_cli_state() -> Iterator[None]:
    """Give every test the module state of `calu.cli` a fresh process has, and restore it after.

    `calu.cli` keeps what one command learned for the rest of that command in module lists
    (`_held`, `_admin_place`, `_hold`, `_typed`). A test that runs a command leaves them
    filled, and a later test in the same process then behaves as if a database login were
    held. Touches nothing outside `calu.cli`'s module state.

    Yields:
        Nothing; the test runs between emptying and restoring.
    """
    from calu import cli

    saved = (list(cli._held), list(cli._admin_place), list(cli._hold), dict(cli._typed))
    cli._held.clear()
    cli._admin_place.clear()
    cli._hold.clear()
    cli._typed.clear()
    yield
    cli._held[:], cli._admin_place[:], cli._hold[:] = saved[0], saved[1], saved[2]
    cli._typed.clear()
    cli._typed.update(saved[3])


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


@pytest.fixture(autouse=True)
def _no_change_to_the_machines_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every `docker update`, `docker compose ... down` and `docker volume rm` a test
    would send to the machine's own Docker.

    On a developer's machine the compose project `calu-db` is their real database (PLAN.md
    D269: `calu.db.local.set_restart`; D267: `calu uninstall`). A test that means to check
    such a command replaces `calu.db.local._run` (or the uninstaller's runner) with a fake of
    its own, or puts a fake `docker` program into a temporary directory on `PATH`; this guard
    lets both through.
    """
    import shutil
    import tempfile

    from calu.db import local

    real = local._run

    def guarded(args: list[str], env: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        changing = args[:2] == ["docker", "update"] or args[:3] == ["docker", "volume", "rm"]
        changing = changing or (args[:2] == ["docker", "compose"] and "down" in args)
        found = shutil.which("docker") or ""
        if changing and not found.startswith(tempfile.gettempdir()):
            raise AssertionError(f"a test sent {args!r} to the machine's own Docker")
        return real(args, env)

    monkeypatch.setattr(local, "_run", guarded)
