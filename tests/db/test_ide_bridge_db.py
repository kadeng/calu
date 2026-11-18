"""`calu ide` against the test database: settings, models, checkers, sessions, `ide.start`."""

import json
import logging
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import anyio
import psycopg
import pytest

from calu import admin, herd
from calu.config import BEDROCK_EU_HAIKU
from calu.credentials import CredentialStore
from calu.db import Db, connect
from calu.db.target import PasswordAuth, Target
from calu.setup import enable_admin_login, enable_operator_login
from tests.db.test_run import prepare
from tests.db.test_serve import owner_db, serving
from tests.unit.ide_helpers import Failed, bridged

pytestmark = [pytest.mark.db, pytest.mark.anyio]

USER = "ide-test-user"
ADMIN_PASSWORD = "what a person types"


@pytest.fixture(autouse=True)
def own_runtime_directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the token-free sockets at an empty directory (see tests/unit/test_ide_bridge.py)."""
    with tempfile.TemporaryDirectory(prefix="calu-ide-") as directory:
        monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
        yield Path(directory)


async def operator_login(db_url: str) -> str:
    """Give `calu_operator` a login.

    Args:
        db_url: The owner login.

    Returns:
        The operator's connection string.
    """
    owner = await connect(db_url)
    try:
        return await enable_operator_login(owner, db_url)
    finally:
        await owner.close()


async def test_settings_are_read_set_validated_and_unset(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    await prepare(db_url, tmp_path)
    store.save("admin", db_url, None)
    async with owner_db(db_url) as db:
        await admin.add_project(db, "demo")
    async with bridged(tmp_path, store) as editor:
        view = await editor.request("ide.settings.get", {"project": None})
        rows = {row["key"]: row for row in view["rows"]}
        assert view["schema"]["properties"]["claude"]
        assert "demo" in view["projects"]
        assert {"command", "edit"} <= {checker["name"] for checker in view["checkers"]}
        assert set(view["checkers"][0]) == {
            "name",
            "route",
            "model",
            "enabled",
            "version",
            "request_limit",
            "output_tokens_limit",
            "max_tokens",
        }
        for row in view["rows"]:
            assert set(row) >= {
                "key",
                "type",
                "default",
                "description",
                "value",
                "is_set",
                "kind",
                "choices",
            }
        assert rows["claude"]["kind"] == "section"
        assert rows["claude.model"]["kind"] == "text" and rows["claude.model"]["value"] == "opus"
        assert rows["claude.model"]["is_set"] is False
        assert rows["command_checker.model"]["kind"] == "model"
        assert rows["claude.effort"]["kind"] == "choice"
        assert "high" in rows["claude.effort"]["choices"]
        assert rows["claude.use_bedrock"]["kind"] == "bool"
        # `prepare` stored this one in the global scope.
        assert rows["claude.sandbox.enabled"]["is_set"] is True
        assert rows["claude.sandbox.enabled"]["value"] is False

        assert await editor.request(
            "ide.settings.set", {"key": "claude.model", "value": "sonnet", "project": None}
        ) == {"value": "sonnet"}
        again = {r["key"]: r for r in (await editor.request("ide.settings.get"))["rows"]}
        assert (again["claude.model"]["value"], again["claude.model"]["is_set"]) == (
            "sonnet",
            True,
        )
        # The project inherits the global value until it stores its own.
        scoped = await editor.request("ide.settings.get", {"project": "demo"})
        inherited = {row["key"]: row for row in scoped["rows"]}["claude.model"]
        assert (inherited["value"], inherited["is_set"], inherited["inherits"]) == (
            "sonnet",
            False,
            "global",
        )

        with pytest.raises(Failed) as invalid:
            await editor.request("ide.settings.set", {"key": "claude.effort", "value": "enormous"})
        assert invalid.value.code == -32602 and "claude.effort" in invalid.value.message
        with pytest.raises(Failed) as unknown_project:
            await editor.request("ide.settings.get", {"project": "nowhere"})
        assert unknown_project.value.code == -32602
        with pytest.raises(Failed) as no_value:
            await editor.request("ide.settings.set", {"key": "claude.model"})
        assert no_value.value.code == -32602

        assert await editor.request("ide.settings.unset", {"key": "claude.model"}) == {}
        final = {r["key"]: r for r in (await editor.request("ide.settings.get"))["rows"]}
        assert (final["claude.model"]["value"], final["claude.model"]["is_set"]) == ("opus", False)


async def test_one_model_for_every_role_and_the_default(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    await prepare(db_url, tmp_path)
    store.save("admin", db_url, None)
    async with bridged(tmp_path, store) as editor:
        before = await editor.request("ide.models.presets")
        assert before["claude"] == {"model": "opus", "effort": None, "use_bedrock": False}
        roles = {role["key"]: role for role in before["roles"]}
        assert {
            "command_checker.model",
            "edit_checker.model",
            "context.narrator_model",
            "context.summarizer_model",
            "honesty.reviewer_model",
        } <= set(roles)
        assert roles["command_checker.model"]["model"] == ""
        assert before["default_model"] == "anthropic:claude-haiku-5-5"

        # A preset id stands for its model; the effort goes to the checkers.
        changed = await editor.request(
            "ide.models.set_all", {"model": "bedrock-eu-haiku", "effort": "low"}
        )
        assert "command_checker.model" in changed["changed"]
        assert "honesty.reviewer_model" in changed["changed"]
        assert "checker:command" in changed["changed"]
        async with owner_db(db_url) as db:
            values = await admin.settings(db)
            assert values["command_checker"] == {"model": BEDROCK_EU_HAIKU, "effort": "low"}
            assert values["context"]["narrator_model"] == BEDROCK_EU_HAIKU
            assert values["honesty"]["reviewer_model"] == BEDROCK_EU_HAIKU
            assert {row.model for row in await admin.checkers(db)} == {BEDROCK_EU_HAIKU}
        after = {r["key"]: r for r in (await editor.request("ide.models.presets"))["roles"]}
        assert after["edit_checker.model"]["model"] == BEDROCK_EU_HAIKU

        # One role alone, by a model string.
        assert (
            await editor.request(
                "ide.models.set", {"role": "context.narrator_model", "model": "claude:haiku"}
            )
            == {}
        )
        with pytest.raises(Failed) as unknown:
            await editor.request("ide.models.set", {"role": "nothing.model", "model": "x:y"})
        assert unknown.value.code == -32602
        async with owner_db(db_url) as db:
            assert (await admin.settings(db))["context"]["narrator_model"] == "claude:haiku"

        # "default" stores no model, as `calu config models default` does.
        await editor.request("ide.models.set_all", {"model": "default"})
        async with owner_db(db_url) as db:
            values = await admin.settings(db)
            assert "model" not in values.get("command_checker", {})
            assert "effort" not in values.get("command_checker", {})
            assert "narrator_model" not in values.get("context", {})
            assert "reviewer_model" not in values.get("honesty", {})
            assert {row.model for row in await admin.checkers(db)} == {""}


async def test_checker_instructions_are_saved_with_the_version_they_were_read_at(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    await prepare(db_url, tmp_path)
    store.save("admin", db_url, None)
    async with bridged(tmp_path, store) as editor:
        read = await editor.request("ide.checker.get", {"name": "command"})
        assert set(read) == {"name", "route", "model", "version", "enabled", "instructions"}
        assert read["route"] == "command" and read["instructions"].strip()
        saved = await editor.request(
            "ide.checker.save",
            {"name": "command", "instructions": "Be strict.", "base_version": read["version"]},
        )
        assert saved["version"] > read["version"]
        assert (await editor.request("ide.checker.get", {"name": "command"}))[
            "instructions"
        ] == "Be strict."

        # A second editor that still holds the old version does not overwrite the change.
        with pytest.raises(Failed) as conflict:
            await editor.request(
                "ide.checker.save",
                {"name": "command", "instructions": "Be lax.", "base_version": read["version"]},
            )
        assert conflict.value.code == -32016
        assert conflict.value.data == {"version": saved["version"], "instructions": "Be strict."}
        async with owner_db(db_url) as db:
            assert await admin.instructions(db, "command") == "Be strict."

        with pytest.raises(Failed) as unknown:
            await editor.request("ide.checker.get", {"name": "nothing"})
        assert unknown.value.code == -32602
        with pytest.raises(Failed) as empty:
            await editor.request(
                "ide.checker.save",
                {"name": "command", "instructions": "  ", "base_version": saved["version"]},
            )
        assert empty.value.code == -32602


async def test_a_session_is_started_named_listed_renamed_and_attached_again(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    session_url = await prepare(db_url, tmp_path)
    # The operator's login alone: it files the request, mints the token and names the session.
    store.save("operator", await operator_login(db_url), None)
    workspace = tmp_path / "project"
    workspace.mkdir()
    host = socket.gethostname()
    async with serving(session_url, hostname=host, user=USER):
        async with bridged(workspace, store, user=USER) as editor:
            assert (await editor.request("ide.sessions"))["running"] == []
            started = await editor.request(
                "ide.start", {"kind": "new", "prompt": "have a look", "name": "  first try  "}
            )
            assert started["attached"] is True
            assert started["snapshot"]["workspace"] == str(workspace)
            session_id = started["session_id"]

            # Attached: its escalation is decided here. The prompt's turn began before the
            # attach, so the question is in the replayed events or arrives as a notification.
            with anyio.fail_after(20):
                while not (open_now := (await editor.call("state.get"))["escalations"]):
                    await anyio.sleep(0.05)
            await editor.call(
                "escalation.decide",
                {"escalation_id": open_now[0]["escalation_id"], "decision": "allow", "note": ""},
            )
            await editor.wait_event("turn.result")
            assert editor.links()[0] == "attached"
            seqs = [event["params"]["seq"] for event in started["events"] + editor.events()]
            assert seqs == sorted(set(seqs))

            listed = await editor.request("ide.sessions")
            (row,) = listed["running"]
            assert set(row) >= {
                "id",
                "name",
                "directory",
                "state",
                "claude_session_id",
                "open_escalations",
                "started_at",
                "stale",
                "here",
            }
            assert (row["id"], row["name"], row["directory"]) == (
                session_id,
                "first try",
                str(workspace),
            )
            assert row["here"] is True and row["stale"] is False
            assert isinstance(listed["stored"], list)
            for conversation in listed["stored"]:
                assert set(conversation) == {
                    "claude_session_id",
                    "last_input",
                    "turns",
                    "age_s",
                    "running",
                    "outside",
                }

            renamed = await editor.request(
                "ide.session.rename", {"session_id": session_id, "name": " payments "}
            )
            assert renamed == {"name": "payments"}
            assert (await editor.request("ide.sessions"))["running"][0]["name"] == "payments"
            with pytest.raises(Failed) as too_long:
                await editor.request(
                    "ide.session.rename", {"session_id": session_id, "name": "x" * 81}
                )
            assert too_long.value.code == -32602
            with pytest.raises(Failed) as not_an_id:
                await editor.request("ide.session.rename", {"session_id": "abc", "name": "x"})
            assert not_an_id.value.code == -32602
            with pytest.raises(Failed) as unknown:
                await editor.request(
                    "ide.session.rename",
                    {"session_id": "00000000-0000-0000-0000-000000000000", "name": "x"},
                )
            assert unknown.value.code == -32011

            async with owner_db(db_url) as db:
                (shown,) = await herd.sessions(db)
                assert shown.name == "payments" and '"payments"' in shown.line()
                assert await db.all(
                    "SELECT revoked FROM client_tokens WHERE label = 'calu ide'"
                ) == [(False,)]

            # Detaching revokes the token the bridge minted.
            await editor.request("ide.detach")
            async with owner_db(db_url) as db:
                assert await db.all(
                    "SELECT revoked FROM client_tokens WHERE label = 'calu ide'"
                ) == [(True,)]

            # The session of the workspace is found through the database, by nothing or by
            # the beginning of its id.
            again = await editor.request("ide.attach", {"session": None})
            assert again["session_id"] == session_id
            assert "turn.result" in [event["method"] for event in again["events"]]
            by_prefix = await editor.request("ide.attach", {"session": session_id[:8]})
            assert by_prefix["session_id"] == session_id
            with pytest.raises(Failed) as none:
                await editor.request("ide.attach", {"session": "ffffffff-no"})
            assert none.value.code == -32011
            # The failed attach left the attached session as it was.
            assert (await editor.call("state.get"))["workspace"] == str(workspace)

            # An operator reads the settings and may not change them.
            assert (await editor.request("ide.settings.get"))["rows"]
            with pytest.raises(Failed) as needs_admin:
                await editor.request("ide.settings.set", {"key": "claude.model", "value": "x"})
            assert needs_admin.value.code == -32010
            assert needs_admin.value.data == {"needs": "admin", "unlocked": []}

    # The session login may not name a session: it must not be able to relabel itself.
    session = await connect(session_url)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await session.execute("UPDATE sessions SET name = 'mine'")
    finally:
        await session.close()


async def test_without_a_server_the_start_says_which_command_to_run(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    await prepare(db_url, tmp_path)
    store.save("operator", await operator_login(db_url), None)
    async with bridged(tmp_path, store, user=USER, claim_wait_s=0.3) as editor:
        with pytest.raises(Failed) as nobody:
            await editor.request("ide.start", {"kind": "new"})
        assert nobody.value.code == -32013 and "calu serve" in nobody.value.message
        with pytest.raises(Failed) as contradiction:
            await editor.request("ide.start", {"kind": "resume"})
        assert contradiction.value.code == -32602
    async with owner_db(db_url) as db:
        assert await db.all("SELECT state FROM session_requests") == [("withdrawn",)]


async def test_a_waiting_start_is_taken_back_by_the_editor(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    await prepare(db_url, tmp_path)
    store.save("operator", await operator_login(db_url), None)
    async with bridged(tmp_path, store, user=USER, claim_wait_s=30) as editor:
        # Nothing waits yet.
        assert await editor.request("ide.start.cancel") == {"cancelled": 0}
        outcome: list[Failed] = []

        async def start() -> None:
            with pytest.raises(Failed) as taken_back:
                await editor.request("ide.start", {"kind": "new"})
            outcome.append(taken_back.value)

        async with anyio.create_task_group() as tg:
            tg.start_soon(start)
            async with owner_db(db_url) as db:
                with anyio.fail_after(10):
                    while not await db.all("SELECT 1 FROM session_requests"):
                        await anyio.sleep(0.05)
            assert await editor.request("ide.start.cancel") == {"cancelled": 1}
        # The request failed long before the 30 s a server is waited for, and its row was
        # withdrawn, so that no server starts a session nobody waits for.
        assert outcome[0].code == -32018
        # The bridge still serves requests.
        assert (await editor.request("ide.sessions"))["running"] == []
    async with owner_db(db_url) as db:
        assert await db.all("SELECT state FROM session_requests") == [("withdrawn",)]


async def test_a_protected_credential_is_asked_for_and_never_prompted(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    await prepare(db_url, tmp_path)
    store.save("admin", db_url, "store password")
    async with bridged(tmp_path, store) as editor:
        with pytest.raises(Failed) as asked:
            await editor.request("ide.sessions")
        assert asked.value.code == -32010 and asked.value.data == {"needs": "admin", "unlocked": []}
        with pytest.raises(Failed) as wrong:
            await editor.request("ide.sessions", {"password": "not it"})
        assert wrong.value.code == -32010 and "wrong password" in wrong.value.message
        listed = await editor.request("ide.sessions", {"password": "store password"})
        assert listed["running"] == []

        with pytest.raises(Failed) as change:
            await editor.request("ide.settings.set", {"key": "claude.model", "value": "haiku"})
        assert change.value.code == -32010 and change.value.data == {
            "needs": "admin",
            "unlocked": [],
        }
        assert await editor.request(
            "ide.settings.set",
            {"key": "claude.model", "value": "haiku", "password": "store password"},
        ) == {"value": "haiku"}

        status = await editor.request("ide.status")
        assert status["database"]["reachable"] is False
        assert status["database"]["needs"] == "admin"


async def test_with_an_operator_credential_a_change_takes_the_admin_password(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    await prepare(db_url, tmp_path)
    owner = await connect(db_url)
    try:
        await enable_admin_login(owner, ADMIN_PASSWORD)
        store.save("operator", await enable_operator_login(owner, db_url), None)
    finally:
        await owner.close()
    async with bridged(tmp_path, store) as editor:
        status = await editor.request("ide.status")
        assert status["database"]["reachable"] is True and status["database"]["problem"] == ""
        assert status["server"] == {"sessions_running": 0}
        with pytest.raises(Failed) as wrong:
            await editor.request(
                "ide.settings.set",
                {"key": "honesty.mode", "value": "track", "password": "not the admin's"},
            )
        assert wrong.value.code == -32010 and wrong.value.data == {"needs": "admin", "unlocked": []}
        assert await editor.request(
            "ide.settings.set",
            {"key": "honesty.mode", "value": "track", "password": ADMIN_PASSWORD},
        ) == {"value": "track"}
    async with owner_db(db_url) as db:
        assert (await admin.settings(db))["honesty"]["mode"] == "track"


async def test_the_operator_may_set_a_session_name_and_no_other_column(db_url: str, tmp_path: Path):
    """Migration 0036: the grant is on the one column, and the migration can run again."""
    await prepare(db_url, tmp_path)
    operator_url = await operator_login(db_url)
    migration = Path(herd.__file__).parent / "db" / "migrations" / "0036_session_name.sql"
    owner = await connect(db_url)
    try:
        await owner.execute(migration.read_text(encoding="utf-8"))  # type: ignore[arg-type]
    finally:
        await owner.close()
    async with owner_db(db_url) as db:
        host = await db.scalar("INSERT INTO hosts (hostname) VALUES ('box') RETURNING id")
        session_id = str(
            await db.scalar(
                "INSERT INTO sessions (host_id, os_user, directory, pid, state)"
                " VALUES (%s, 'u', '/w', 1, 'idle') RETURNING id::text",
                (host,),
            )
        )
    operator = await connect(operator_url)
    try:
        assert await herd.rename_session(Db(operator), session_id, " night shift ") == "night shift"
        assert await herd.rename_session(Db(operator), session_id, "") == ""
        with pytest.raises(ValueError, match="at most 80"):
            await herd.rename_session(Db(operator), session_id, "x" * 81)
        with pytest.raises(LookupError):
            await herd.rename_session(Db(operator), "00000000-0000-0000-0000-000000000000", "x")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await operator.execute("UPDATE sessions SET directory = '/elsewhere'")
    finally:
        await operator.close()
    # The database enforces the length for whoever writes the column directly.
    async with owner_db(db_url) as db:
        with pytest.raises(psycopg.errors.CheckViolation):
            await db.execute("UPDATE sessions SET name = %s", ("x" * 81,))


def secrets_of(*logins: str) -> list[str]:
    """Return the database passwords inside connection strings, for "is not logged" checks.

    Args:
        *logins: Connection strings as the credential store holds them.
    """
    found = []
    for login in logins:
        auth = Target.parse(login).auth
        assert isinstance(auth, PasswordAuth) and auth.password
        found.append(auth.password)
    return found


async def test_one_password_at_the_start_covers_reading_and_changing(
    db_url: str, tmp_path: Path, store: CredentialStore, caplog: pytest.LogCaptureFixture
):
    await prepare(db_url, tmp_path)
    store.save("admin", db_url, "the one password")
    caplog.set_level(logging.DEBUG)
    async with bridged(tmp_path, store) as editor:
        status = await editor.request("ide.status")
        assert status["locked"] is True and status["unlocked"] == []
        with pytest.raises(Failed) as wrong:
            await editor.request("ide.unlock", {"password": "not it"})
        assert wrong.value.code == -32010 and "wrong password" in wrong.value.message
        assert wrong.value.data == {"needs": "admin", "unlocked": []}
        with pytest.raises(Failed) as blank:
            await editor.request("ide.unlock", {"password": ""})
        assert blank.value.code == -32010
        with pytest.raises(Failed) as missing:
            await editor.request("ide.unlock", {})
        assert missing.value.code == -32602

        assert await editor.request("ide.unlock", {"password": "the one password"}) == {
            "unlocked": ["admin"],
            "reading": True,
            "changing": True,
        }
        # From here on no request carries a password.
        assert (await editor.request("ide.sessions"))["running"] == []
        assert await editor.request(
            "ide.settings.set", {"key": "claude.model", "value": "haiku"}
        ) == {"value": "haiku"}
        read = await editor.request("ide.checker.get", {"name": "command"})
        await editor.request(
            "ide.checker.save",
            {"name": "command", "instructions": "Be strict.", "base_version": read["version"]},
        )
        await editor.request("ide.models.set_all", {"model": "default"})
        status = await editor.request("ide.status")
        assert status["locked"] is False and status["unlocked"] == ["admin"]
        assert status["database"]["reachable"] is True
        # A second unlock with any password has nothing left to open and is no error.
        assert (await editor.request("ide.unlock", {"password": "whatever"}))["changing"] is True
        written = json.dumps(editor.wire)
    # Neither the password nor the login it opened reached a log record or the editor.
    for secret in ("the one password", *secrets_of(db_url)):
        assert secret not in caplog.text and secret not in written

    # A new bridge process holds nothing.
    async with bridged(tmp_path, store) as editor:
        assert (await editor.request("ide.status"))["locked"] is True
        with pytest.raises(Failed) as again:
            await editor.request("ide.sessions")
        assert again.value.code == -32010


async def test_a_blank_password_is_a_password_like_any_other(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    await prepare(db_url, tmp_path)
    store.save("admin", db_url, "")
    async with bridged(tmp_path, store) as editor:
        with pytest.raises(Failed) as asked:
            await editor.request("ide.sessions")
        assert asked.value.code == -32010
        # Per request, and once for the whole process.
        assert (await editor.request("ide.sessions", {"password": ""}))["running"] == []
        assert (await editor.request("ide.unlock", {"password": ""}))["unlocked"] == ["admin"]
        assert (await editor.request("ide.sessions"))["running"] == []


async def test_a_second_password_is_needed_only_where_the_first_does_not_reach(
    db_url: str, tmp_path: Path, store: CredentialStore, caplog: pytest.LogCaptureFixture
):
    await prepare(db_url, tmp_path)
    owner = await connect(db_url)
    try:
        await enable_admin_login(owner, ADMIN_PASSWORD)
        operator_url = await enable_operator_login(owner, db_url)
    finally:
        await owner.close()
    store.save("operator", operator_url, "password A")
    caplog.set_level(logging.DEBUG)
    async with bridged(tmp_path, store) as editor:
        assert (await editor.request("ide.status"))["locked"] is True
        assert await editor.request("ide.unlock", {"password": "password A"}) == {
            "unlocked": ["operator"],
            "reading": True,
            "changing": False,
        }
        assert (await editor.request("ide.sessions"))["running"] == []
        assert (await editor.request("ide.settings.get"))["rows"]
        with pytest.raises(Failed) as change:
            await editor.request("ide.settings.set", {"key": "honesty.mode", "value": "track"})
        assert change.value.code == -32010
        assert change.value.data == {"needs": "admin", "unlocked": ["operator"]}
        # The first password again opens nothing new and does not reach the change.
        with pytest.raises(Failed) as same:
            await editor.request("ide.unlock", {"password": "password A"})
        assert same.value.code == -32010
        assert same.value.data == {"needs": "admin", "unlocked": ["operator"]}

        assert await editor.request("ide.unlock", {"password": ADMIN_PASSWORD}) == {
            "unlocked": ["operator", "calu_admin role"],
            "reading": True,
            "changing": True,
        }
        assert await editor.request(
            "ide.settings.set", {"key": "honesty.mode", "value": "track"}
        ) == {"value": "track"}
        # The per-request password keeps working for clients that send one.
        assert await editor.request(
            "ide.settings.set",
            {"key": "honesty.mode", "value": "enforce", "password": ADMIN_PASSWORD},
        ) == {"value": "enforce"}
        written = json.dumps(editor.wire)
    for secret in ("password A", ADMIN_PASSWORD, *secrets_of(operator_url)):
        assert secret not in caplog.text and secret not in written
    async with owner_db(db_url) as db:
        assert (await admin.settings(db))["honesty"]["mode"] == "enforce"
