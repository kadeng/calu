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

from calu import admin, herd, servers
from calu.config import BEDROCK_EU_HAIKU
from calu.credentials import CredentialStore
from calu.db import Db, connect
from calu.db.target import PasswordAuth, Target
from calu.defaults.instructions import default_redaction_instructions
from calu.store import DatabaseSessionStore, SessionIdentity
from tests.db.test_run import prepare
from tests.db.test_serve import owner_db, serving
from tests.unit.ide_helpers import Failed, bridged

pytestmark = [pytest.mark.db, pytest.mark.anyio]

USER = "ide-test-user"


@pytest.fixture(autouse=True)
def own_runtime_directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the token-free sockets at an empty directory (see tests/unit/test_ide_bridge.py)."""
    with tempfile.TemporaryDirectory(prefix="calu-ide-") as directory:
        monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
        yield Path(directory)


async def test_settings_are_read_set_validated_and_unset(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    user_url = await prepare(db_url, tmp_path)
    store.save("user", user_url, None)
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
        # D245: the redaction agent's instructions are a Markdown setting holding the shipped
        # text, like the honesty rules.
        redaction = rows["traces.redactor_instructions"]
        assert redaction["kind"] == "markdown" and redaction["is_set"] is False
        assert redaction["value"] == default_redaction_instructions()
        assert rows["traces.layout"]["kind"] == "choice"
        assert set(rows["traces.layout"]["choices"]) == {"date/user", "user", "date", "flat"}
        # `prepare` stored this one in the global scope.
        assert rows["claude.sandbox.enabled"]["is_set"] is True
        assert rows["claude.sandbox.enabled"]["value"] is False

        assert await editor.request(
            "ide.settings.set", {"key": "claude.model", "value": "sonnet", "project": None}
        ) == {"value": "sonnet"}
        # D253: switching human diffs on offers the traces; the editor asks the user.
        offered = await editor.request(
            "ide.settings.set", {"key": "human_diffs.enabled", "value": True, "project": None}
        )
        assert offered["value"] is True and offered["offer"]["key"] == "traces.enabled"
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
    user_url = await prepare(db_url, tmp_path)
    store.save("user", user_url, None)
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
                "ide.models.set",
                {"role": "context.narrator_model", "model": "anthropic:claude-haiku-4-5"},
            )
            == {}
        )
        with pytest.raises(Failed) as unknown:
            await editor.request("ide.models.set", {"role": "nothing.model", "model": "x:y"})
        assert unknown.value.code == -32602
        async with owner_db(db_url) as db:
            assert (await admin.settings(db))["context"][
                "narrator_model"
            ] == "anthropic:claude-haiku-4-5"
            names = {row.name for row in await admin.checkers(db)}

        # A checker's model by its role key; an unknown name is refused and creates nothing.
        assert (
            await editor.request(
                "ide.models.set",
                {"role": "checker:command", "model": "anthropic:claude-sonnet-4-5"},
            )
            == {}
        )
        with pytest.raises(Failed) as no_checker:
            await editor.request("ide.models.set", {"role": "checker:nosuch", "model": "x:y"})
        assert "nosuch" in no_checker.value.message
        async with owner_db(db_url) as db:
            rows = {row.name: row for row in await admin.checkers(db)}
            assert set(rows) == names and rows["command"].model == "anthropic:claude-sonnet-4-5"

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
    user_url = await prepare(db_url, tmp_path)
    store.save("user", user_url, None)
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
    # The user login alone (D265): it files the request, mints the token, names the session
    # and changes settings.
    store.save("user", session_url, None)
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

            # The same login reads the settings and changes them.
            assert (await editor.request("ide.settings.get"))["rows"]
            assert await editor.request(
                "ide.settings.set", {"key": "claude.model", "value": "sonnet"}
            ) == {"value": "sonnet"}


async def test_a_start_the_server_refuses_says_why_and_an_unnamed_start_has_no_name(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    session_url = await prepare(db_url, tmp_path)
    store.save("user", session_url, None)
    workspace = tmp_path / "project"
    workspace.mkdir()
    async with serving(session_url, hostname=socket.gethostname(), user=USER):
        async with bridged(workspace, store, user=USER) as editor:
            with pytest.raises(Failed) as refused:
                await editor.request(
                    "ide.start", {"kind": "resume", "claude_session_id": "not-a-uuid"}
                )
            assert refused.value.code == -32014
            assert "no Claude session id" in refused.value.message

            started = await editor.request("ide.start", {"kind": "new"})
            assert started["attached"] is True
            (row,) = (await editor.request("ide.sessions"))["running"]
            assert (row["id"], row["name"]) == (started["session_id"], "")
    async with owner_db(db_url) as db:
        assert sorted(state for (state,) in await db.all("SELECT state FROM session_requests")) == [
            "failed",
            "started",
        ]


async def test_without_a_server_the_start_says_which_command_to_run(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    user_url = await prepare(db_url, tmp_path)
    store.save("user", user_url, None)
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
    user_url = await prepare(db_url, tmp_path)
    store.save("user", user_url, None)
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


async def test_a_cancel_after_the_server_started_the_session_does_not_abort_the_start(
    db_url: str, tmp_path: Path, store: CredentialStore, monkeypatch: pytest.MonkeyPatch
):
    """The cancel scope covered the naming, the token and the attach as well, so a cancel in
    that moment failed the request and left the started session with nobody attached."""
    session_url = await prepare(db_url, tmp_path)
    store.save("user", session_url, None)
    workspace = tmp_path / "project"
    workspace.mkdir()
    named, go = anyio.Event(), anyio.Event()
    original = herd.rename_session

    async def slow_rename(db: Db, session_id: str, name: str) -> str:
        named.set()
        await go.wait()
        return await original(db, session_id, name)

    monkeypatch.setattr(herd, "rename_session", slow_rename)
    async with serving(session_url, hostname=socket.gethostname(), user=USER):
        async with bridged(workspace, store, user=USER) as editor:
            started: list[dict] = []

            async def start() -> None:
                started.append(
                    await editor.request("ide.start", {"kind": "new", "name": "late cancel"})
                )

            async with anyio.create_task_group() as tg:
                tg.start_soon(start)
                with anyio.fail_after(30):
                    await named.wait()
                assert await editor.request("ide.start.cancel") == {"cancelled": 0}
                go.set()
            assert started[0]["attached"] is True


async def test_status_sessions_and_attach_close_the_escalations_of_ended_sessions(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    """An editor's look at the herd closes the questions nobody waits for any more, as `calu
    sessions` does (`herd.close_gone`): a session that ended keeps no open escalation."""
    user_url = await prepare(db_url, tmp_path)
    store.save("user", user_url, None)
    async with owner_db(db_url) as db:

        async def ended_with_a_question() -> int:
            """Store a session with an open escalation, then end it without closing it."""
            recorder = DatabaseSessionStore(db)
            identity = SessionIdentity(hostname="elsewhere", os_user="u", directory="/w", pid=7)
            session_id = await recorder.start(identity)
            escalation = await recorder.open_escalation("why", {"rendered": "$ x"})
            await db.execute(
                "UPDATE sessions SET ended_at = now() WHERE id = %s::uuid", (session_id,)
            )
            return escalation

        async def state(escalation: int) -> str:
            return str(
                await db.scalar("SELECT state FROM escalations WHERE id = %s", (escalation,))
            )

        async with bridged(tmp_path, store) as editor:
            for method, params in (
                ("ide.status", {}),
                ("ide.sessions", {}),
                ("ide.attach", {"session": "ffffffff"}),
            ):
                escalation = await ended_with_a_question()
                assert await state(escalation) == "open"
                try:
                    await editor.request(method, params)
                except Failed as refused:
                    assert method == "ide.attach" and refused.code == -32011  # no such session
                assert await state(escalation) == "cancelled", method


async def test_a_protected_credential_is_asked_for_and_never_prompted(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    user_url = await prepare(db_url, tmp_path)
    store.save("user", user_url, "store password")
    async with bridged(tmp_path, store) as editor:
        with pytest.raises(Failed) as asked:
            await editor.request("ide.sessions")
        assert asked.value.code == -32010 and asked.value.data == {"needs": "user", "unlocked": []}
        with pytest.raises(Failed) as wrong:
            await editor.request("ide.sessions", {"password": "not it"})
        assert wrong.value.code == -32010
        assert "That is not the calu user password" in wrong.value.message
        listed = await editor.request("ide.sessions", {"password": "store password"})
        assert listed["running"] == []

        with pytest.raises(Failed) as change:
            await editor.request("ide.settings.set", {"key": "claude.model", "value": "haiku"})
        assert change.value.code == -32010 and change.value.data == {
            "needs": "user",
            "unlocked": [],
        }
        assert await editor.request(
            "ide.settings.set",
            {"key": "claude.model", "value": "haiku", "password": "store password"},
        ) == {"value": "haiku"}

        status = await editor.request("ide.status")
        assert status["database"]["reachable"] is False
        assert status["database"]["needs"] == "user"


async def test_the_status_counts_the_running_servers_and_a_change_needs_no_second_password(
    db_url: str, tmp_path: Path, store: CredentialStore
):
    """A `user` entry without a password reads the herd and changes settings (D265)."""
    user_url = await prepare(db_url, tmp_path)
    store.save("user", user_url, None)
    async with bridged(tmp_path, store) as editor:
        status = await editor.request("ide.status")
        assert status["database"]["reachable"] is True and status["database"]["problem"] == ""
        assert status["server"] == {"sessions_running": 0, "servers_running": 0}
        # A server of this host whose process runs counts (this test's own pid); one whose
        # process is gone does not, nor one of another host.
        async with owner_db(db_url) as db:
            await servers.record_start(db, socket.gethostname(), USER, "ws://127.0.0.1:1")
            gone = await servers.record_start(db, socket.gethostname(), USER, "")
            await db.execute("UPDATE servers SET pid = 2147483646 WHERE id = %s", (gone,))
            await servers.record_start(db, "another-host", USER, "")
        status = await editor.request("ide.status")
        assert status["server"] == {"sessions_running": 0, "servers_running": 1}
        assert await editor.request(
            "ide.settings.set", {"key": "honesty.mode", "value": "track"}
        ) == {"value": "track"}
    async with owner_db(db_url) as db:
        assert (await admin.settings(db))["honesty"]["mode"] == "track"


async def test_the_user_login_names_a_session(db_url: str, tmp_path: Path):
    """A session's name has at most 80 characters, and the user login sets it."""
    user_url = await prepare(db_url, tmp_path)
    async with owner_db(db_url) as db:
        host = await db.scalar("INSERT INTO hosts (hostname) VALUES ('box') RETURNING id")
        session_id = str(
            await db.scalar(
                "INSERT INTO sessions (host_id, os_user, directory, pid, state)"
                " VALUES (%s, 'u', '/w', 1, 'idle') RETURNING id::text",
                (host,),
            )
        )
    user = await connect(user_url)
    try:
        assert await herd.rename_session(Db(user), session_id, " night shift ") == "night shift"
        assert await herd.rename_session(Db(user), session_id, "") == ""
        with pytest.raises(ValueError, match="at most 80"):
            await herd.rename_session(Db(user), session_id, "x" * 81)
        with pytest.raises(LookupError):
            await herd.rename_session(Db(user), "00000000-0000-0000-0000-000000000000", "x")
    finally:
        await user.close()
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
    user_url = await prepare(db_url, tmp_path)
    store.save("user", user_url, "the one password")
    caplog.set_level(logging.DEBUG)
    async with bridged(tmp_path, store) as editor:
        status = await editor.request("ide.status")
        assert status["locked"] is True and status["unlocked"] == []
        with pytest.raises(Failed) as wrong:
            await editor.request("ide.unlock", {"password": "not it"})
        assert wrong.value.code == -32010
        assert "That is not the calu user password" in wrong.value.message
        assert wrong.value.data == {"needs": "user", "unlocked": []}
        with pytest.raises(Failed) as blank:
            await editor.request("ide.unlock", {"password": ""})
        assert blank.value.code == -32010
        with pytest.raises(Failed) as missing:
            await editor.request("ide.unlock", {})
        assert missing.value.code == -32602

        assert await editor.request("ide.unlock", {"password": "the one password"}) == {
            "unlocked": ["user"],
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
        assert status["locked"] is False and status["unlocked"] == ["user"]
        assert status["database"]["reachable"] is True
        # A second unlock with any password has nothing left to open and is no error.
        assert (await editor.request("ide.unlock", {"password": "whatever"}))["changing"] is True
        written = json.dumps(editor.wire)
    # Neither the password nor the login it opened reached a log record or the editor.
    for secret in ("the one password", *secrets_of(user_url)):
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
    user_url = await prepare(db_url, tmp_path)
    store.save("user", user_url, "")
    async with bridged(tmp_path, store) as editor:
        with pytest.raises(Failed) as asked:
            await editor.request("ide.sessions")
        assert asked.value.code == -32010
        # Per request, and once for the whole process.
        assert (await editor.request("ide.sessions", {"password": ""}))["running"] == []
        assert (await editor.request("ide.unlock", {"password": ""}))["unlocked"] == ["user"]
        assert (await editor.request("ide.sessions"))["running"] == []
