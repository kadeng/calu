"""The checkers screen, code honesty and the git program of the full-screen client.

What the client changes in the database (a checker's instructions, `honesty.mode`) goes
through `calu.tui.adminaccess.AdminAccess`, which `FakeAdmin` replaces here; the program
`f4` starts goes through `CaluApp.run_program`, `which` and `environ`, which the tests set.
No test reads this machine's credential store or starts one of its programs.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from calu import admin
from calu.credentials import CredentialStore, KdfParams
from calu.tui import adminaccess, gittool
from calu.tui.actions import Actions
from calu.tui.adminaccess import AdminFailed, PasswordNeeded, StoreAdmin, weakens
from calu.tui.app import describe_commit
from calu.tui.connection import Link
from calu.tui.fake import FakeAdmin, FakeSession, fake_connector
from calu.tui.model import SessionModel, honesty_from
from calu.tui.screens.checkers import CheckersScreen
from calu.tui.screens.confirm import ConfirmScreen
from calu.tui.screens.password import PasswordScreen
from calu.tui.screens.text import TextScreen
from tests.unit.test_tui_app import Running, running

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run the tests of this file on asyncio, the loop Textual uses."""
    return "asyncio"


def installed(*names: str) -> gittool.Which:
    """Build a `which` that finds exactly the named programs."""
    return lambda name: f"/usr/bin/{name}" if name in names else None


async def command(run: Running, line: str) -> None:
    """Type a slash command and press Enter."""
    await run.type(line)
    await run.press("enter")
    await run.pilot.pause()


# ------------------------------------------------------------------ the git program, pure


def test_the_git_program_is_chosen_by_environment_then_setting_then_what_is_installed() -> None:
    which = installed("lazygit", "gitui", "tig", "git")
    assert gittool.choose({"CALU_GIT_TOOL": "tig status"}, "gitui", which) == gittool.GitTool(
        ("tig", "status"), "CALU_GIT_TOOL"
    )
    assert gittool.choose({}, "gitui", which) == gittool.GitTool(("gitui",), "tui.toml")
    assert gittool.choose({}, "", which) == gittool.GitTool(("lazygit",), "installed")
    # lazygit is the default (the owner's decision): another program counts only when it is
    # configured, so without lazygit the client falls back to git diff and offers to install it.
    fallback = gittool.choose({}, "", installed("gitui", "tig", "git"))
    assert isinstance(fallback, gittool.GitTool) and fallback.source == "fallback"
    assert fallback.argv[0] == "git" and fallback.argv[-1] == "diff" and fallback.name == "git"


def test_a_configured_program_that_is_missing_is_named_and_nothing_else_is_tried() -> None:
    missing = gittool.choose({"CALU_GIT_TOOL": "gitui"}, "", installed("lazygit", "git"))
    assert missing == gittool.Missing("CALU_GIT_TOOL names gitui, which is not installed")
    broken = gittool.choose({}, "lazygit 'x", installed("lazygit"))
    assert isinstance(broken, gittool.Missing) and "tui.toml is no command line" in broken.reason
    assert gittool.choose({}, "", installed()) == gittool.Missing(
        "git is not installed on this machine"
    )


@pytest.mark.parametrize(
    ("manager", "command", "runnable"),
    [
        ("pacman", "sudo pacman -S lazygit", False),
        ("dnf", "sudo dnf install lazygit", False),
        ("zypper", "sudo zypper install lazygit", False),
        ("apt", "sudo apt install lazygit", False),
        ("go", "go install github.com/jesseduffield/lazygit@latest", True),
    ],
)
def test_the_install_command_fits_the_package_manager(
    manager: str, command: str, runnable: bool
) -> None:
    plan = gittool.install_plan(installed(manager))
    assert (plan.command, plan.manager, plan.runnable) == (command, manager, runnable)
    assert plan.runnable == ("sudo" not in plan.command), "a command with sudo is only shown"


def test_without_a_package_manager_the_release_page_is_named() -> None:
    plan = gittool.install_plan(installed())
    assert plan.manager == "release" and not plan.runnable and gittool.RELEASES in plan.command


def test_the_install_offer_prefers_a_command_the_client_may_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lazygit is offered with a command that needs no administrator rights where one exists."""
    monkeypatch.setattr(gittool.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gittool.platform, "machine", lambda: "x86_64")
    assert gittool.install_plan(installed("brew", "curl", "tar", "sh")).command == (
        "brew install lazygit"
    )
    plan = gittool.install_plan(installed("apt", "curl", "tar", "sh"))
    assert plan.runnable and "sudo" not in plan.command
    assert "lazygit_${v}_Linux_x86_64.tar.gz" in plan.command
    assert str(gittool.LOCAL_BIN) in plan.command and str(gittool.LOCAL_BIN) in plan.note
    # Another system, or an architecture lazygit has no archive for: the package manager's way.
    monkeypatch.setattr(gittool.platform, "machine", lambda: "riscv64")
    assert gittool.install_plan(installed("apt", "curl", "tar", "sh")).command == (
        "sudo apt install lazygit"
    )
    # After that install the program is found although ~/.local/bin is not on the PATH.
    local = str(gittool.LOCAL_BIN / "lazygit")
    assert gittool.choose({}, "", installed(local, "git")) == gittool.GitTool((local,), "installed")


def test_the_git_program_needs_the_project_directory_on_this_machine(tmp_path: Path) -> None:
    assert gittool.usable_directory(str(tmp_path)) is None
    said = gittool.usable_directory("/work/nowhere")
    assert said is not None and "needs a terminal on the session's machine" in said


def test_a_commit_is_described_only_where_the_repository_is(tmp_path: Path) -> None:
    assert describe_commit("/work/nowhere", "HEAD") == ""
    assert describe_commit(str(tmp_path), "HEAD") == "", "a directory without a repository"


# ------------------------------------------------------------------ honesty state, pure


def test_todays_honesty_event_carries_mode_and_marks_only() -> None:
    model = SessionModel()
    model.apply(
        "honesty.update",
        {"seq": 1, "mode": "track", "marks": [{"id": 3, "path": "a.py", "reason": "r"}]},
    )
    assert model.honesty_mode == "track" and [mark.id for mark in model.marks] == [3]
    assert not model.honesty.reported and model.honesty.baseline == ""


def test_the_coming_honesty_event_is_read_field_by_field() -> None:
    state = honesty_from(
        {
            "mode": "enforce",
            "marks": [],
            "baseline": {"commit_id": "4f2a9c1d07aa", "source": "review", "subject": "Fix"},
            "unreviewed": ["a.py", "b.py"],
            "unreviewed_count": 5,
            "review_required": True,
            "review_running": False,
        }
    )
    assert state.reported and state.baseline == "4f2a9c1d07aa" and state.baseline_source == "review"
    assert state.unreviewed == ["a.py", "b.py"] and state.unreviewed_count == 5
    assert state.review_required and not state.review_running


def test_other_spellings_and_wrong_types_do_not_break_the_honesty_state() -> None:
    nested = honesty_from(
        {"honesty": {"baseline": "abc123", "unreviewed": {"everything": True, "problem": "p"}}}
    )
    assert (
        nested.baseline == "abc123" and nested.unreviewed_all and nested.unreviewed_problem == "p"
    )
    flat = honesty_from({"honesty_baseline": None, "honesty_unreviewed": ["x"]})
    assert flat.reported and flat.baseline == "" and flat.unreviewed_count == 1
    odd = honesty_from({"baseline": 7, "unreviewed": "all", "review_required": "yes"})
    assert odd.reported and odd.baseline == "" and odd.unreviewed == [] and not odd.review_required


def test_a_mode_change_weakens_only_downwards() -> None:
    assert weakens("enforce", "track") and weakens("enforce", "off") and weakens("track", "off")
    assert not weakens("off", "track") and not weakens("track", "enforce")
    assert not weakens("enforce", "enforce") and not weakens("other", "off")


# ------------------------------------------------------------------ the credential, pure


def store_at(path: Path) -> CredentialStore:
    """Return a credential store with cheap key derivation."""
    return CredentialStore(path, KdfParams(time_cost=1, memory_cost=8, parallelism=1))


async def test_without_the_admin_credential_nothing_is_changed(tmp_path: Path) -> None:
    access = StoreAdmin(store_at(tmp_path / "c.json"))
    assert access.problem() == adminaccess.NO_CREDENTIAL and not access.needs_password()
    with pytest.raises(AdminFailed, match="no 'admin' credential"):
        await access.save_instructions("command", "text", None)
    with pytest.raises(AdminFailed, match="one of off, track, enforce"):
        await access.set_honesty_mode("strict", None, None)


async def test_a_protected_credential_needs_the_right_password(tmp_path: Path) -> None:
    store = store_at(tmp_path / "c.json")
    store.save("admin", "postgresql://owner@127.0.0.1:1/calu", "secret")
    access = StoreAdmin(store)
    assert access.problem() is None and access.needs_password()
    with pytest.raises(PasswordNeeded, match="needs its password"):
        await access.save_instructions("command", "text", None)
    with pytest.raises(PasswordNeeded, match="wrong password"):
        await access.save_instructions("command", "text", "other")


async def test_instructions_and_mode_are_stored_through_calu_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path / "c.json")
    store.save("admin", "postgresql://owner@db/calu", None)
    calls: list[tuple[Any, ...]] = []

    class Conn:
        """A connection that only closes."""

        async def close(self) -> None:
            """Record the close."""
            calls.append(("close",))

    async def connect(url: Any) -> Conn:
        """Record the connection string."""
        calls.append(("connect", bytes(url).decode()))
        return Conn()

    async def instructions(db: Any, name: str) -> str:
        """Know one checker."""
        if name != "command":
            raise admin.AdminError(f"no checker named {name!r}")
        return "old"

    async def checkers(db: Any) -> list[admin.CheckerRow]:
        """List the one checker at version 12."""
        return [admin.CheckerRow("command", "command", "m", 1, 1, 1, True, 12)]

    async def set_checker(db: Any, name: str, instructions_text: str) -> admin.CheckerRow:
        """Record the new text."""
        calls.append(("set_checker", name, instructions_text))
        return admin.CheckerRow(name, "command", "m", 1, 1, 1, True, 13)

    async def settings(db: Any, project: str | None = None) -> dict[str, Any]:
        """One project sets the mode itself."""
        return {"honesty": {"mode": "track"}} if project == "own" else {}

    async def set_setting(db: Any, key: str, value: Any, project: str | None = None) -> dict:
        """Record the setting."""
        calls.append(("set_setting", key, value, project))
        return {}

    monkeypatch.setattr(adminaccess, "connect", connect)
    monkeypatch.setattr(admin, "instructions", instructions)
    monkeypatch.setattr(admin, "set_checker", set_checker)
    monkeypatch.setattr(admin, "checkers", checkers)
    monkeypatch.setattr(admin, "settings", settings)
    monkeypatch.setattr(admin, "set_setting", set_setting)
    access = StoreAdmin(store)

    saved = await access.save_instructions("command", "new text", None)
    assert (saved.name, saved.version) == ("command", 13)
    assert calls == [
        ("connect", "postgresql://owner@db/calu"),
        ("set_checker", "command", "new text"),
        ("close",),
    ]
    with pytest.raises(AdminFailed, match="no checker named 'nope'"):
        await access.save_instructions("nope", "x", None)
    with pytest.raises(AdminFailed, match="changed meanwhile to version 12: press r"):
        await access.save_instructions("command", "newer text", None, base_version=11)
    assert ("set_checker", "command", "newer text") not in calls, "the other change is kept"
    assert (await access.save_instructions("command", "newer text", None, 12)).version == 13
    calls.clear()

    assert (await access.set_honesty_mode("enforce", "myproject", None)).scope == "global"
    assert (await access.set_honesty_mode("off", "own", None)).scope == "project own"
    assert [call for call in calls if call[0] == "set_setting"] == [
        ("set_setting", "honesty.mode", "enforce", None),
        ("set_setting", "honesty.mode", "off", "own"),
    ]


# ------------------------------------------------------------------ requests


async def test_trust_and_review_requests_and_a_session_without_them() -> None:
    session = FakeSession()
    model = SessionModel()
    link = Link(fake_connector(session), model, lambda change: None, lambda: 0.0)
    actions = Actions(link, model)

    async def ignore(method: str, params: dict[str, Any]) -> None:
        """Drop the events: this test reads the answers only."""

    async with fake_connector(session)(ignore, 0) as connection:
        link.connection = connection
        refused = await actions.trust()
        assert not refused.ok and refused.message == (
            "this session does not support it (honesty.trust)"
        )
        assert not (await actions.honesty_review()).ok
        session.honesty_requests = True
        assert (await actions.trust()).data == {"commit_id": "4f2a9c1d07"}
        await actions.trust("main")
        await actions.trust_file("notes/plan.md")
        assert (await actions.honesty_review()).ok
    assert session.sent("honesty.trust")[-3:] == [{}, {"rev": "main"}, {"path": "notes/plan.md"}]
    assert session.sent("honesty.review")[-1] == {}


# ------------------------------------------------------------------ the checkers screen


async def test_the_checkers_screen_lists_the_checkers_and_their_instructions() -> None:
    async with running("simple_turn") as run:
        await command(run, "/checkers")
        screen = run.app.screen
        assert isinstance(screen, CheckersScreen)
        assert "> command" in screen.text and "v12" in screen.text and "v3 off" in screen.text
        assert "You judge one shell command before it runs." in screen.text
        assert "e edits the instructions" in screen.message
        await run.press("j")
        assert (screen.current or {})["name"] == "edit"
        assert "You judge one edit of a project file." in screen.text
        await run.press("escape")
        assert not isinstance(run.app.screen, CheckersScreen)


async def test_edited_instructions_are_saved_with_the_credential_and_not_by_the_session() -> None:
    async with running("simple_turn") as run:
        admin_access = run.app.admin
        assert isinstance(admin_access, FakeAdmin)
        await command(run, "/checkers")
        screen = run.app.screen
        assert isinstance(screen, CheckersScreen)
        await run.press("e")
        assert screen.editing and "ctrl+s" in str(screen.query_one("#checkers-keys").render())
        editor = screen.query_one("#checkers-editor")
        editor.insert("Never allow a force push.\n\n", (0, 0))  # type: ignore[attr-defined]
        await run.press("escape")
        assert screen.editing and "esc again drops the change" in screen.message
        await run.press("ctrl+s")
        await run.pilot.pause()
        assert not screen.editing
        assert screen.message.startswith("saved: command is now version 13")
        assert admin_access.saved[0][0] == "command"
        assert admin_access.saved[0][1].startswith("Never allow a force push.")
        assert "v13" in screen.text and "Never allow a force push." in screen.text
        methods = {method for method, _ in run.session.requests}
        assert not {name for name in methods if "checker" in name and name != "checkers.get"}


async def test_unchanged_and_empty_instructions_are_not_stored() -> None:
    async with running("simple_turn") as run:
        admin_access = run.app.admin
        assert isinstance(admin_access, FakeAdmin)
        await command(run, "/checkers")
        screen = run.app.screen
        assert isinstance(screen, CheckersScreen)
        await run.press("e", "alt+s")
        assert screen.message == "unchanged; nothing was stored" and screen.editing
        screen.query_one("#checkers-editor").load_text("  ")  # type: ignore[attr-defined]
        await run.press("ctrl+s")
        assert screen.message == "the instructions must not be empty"
        await run.press("escape", "escape")
        assert not screen.editing and admin_access.saved == []


async def test_saving_asks_for_the_password_until_it_is_right() -> None:
    session = FakeSession()
    access = FakeAdmin(session, password="secret")
    async with running("simple_turn", session=session, admin=access) as run:
        await command(run, "/checkers")
        screen = run.app.screen
        assert isinstance(screen, CheckersScreen)
        await run.press("e")
        screen.query_one("#checkers-editor").load_text("New rules.")  # type: ignore[attr-defined]
        await run.press("ctrl+s")
        assert isinstance(run.app.screen, PasswordScreen)
        await run.press("x", "enter")
        await run.pilot.pause()
        again = run.app.screen
        assert isinstance(again, PasswordScreen) and "wrong password" in again.problem
        assert access.saved == []
        await run.press(*"secret", "enter")
        await run.pilot.pause()
        assert access.saved == [("command", "New rules.")]
        assert run.app.screen is screen and "version 13" in screen.message


async def test_a_refused_save_keeps_the_editor_and_its_text() -> None:
    session = FakeSession()
    access = FakeAdmin(session)
    access.fail = "the database refused: permission denied"
    async with running("simple_turn", session=session, admin=access) as run:
        await command(run, "/checkers")
        screen = run.app.screen
        assert isinstance(screen, CheckersScreen)
        await run.press("e")
        editor = screen.query_one("#checkers-editor")
        editor.load_text("New rules.")  # type: ignore[attr-defined]
        await run.press("ctrl+s")
        await run.pilot.pause()
        assert screen.editing and "not saved: the database refused" in screen.message
        assert editor.text == "New rules."  # type: ignore[attr-defined]


async def test_without_the_credential_the_checkers_screen_only_reads() -> None:
    session = FakeSession()
    async with running(
        "simple_turn", session=session, admin=FakeAdmin(session, credential=False)
    ) as run:
        await command(run, "/checkers")
        screen = run.app.screen
        assert isinstance(screen, CheckersScreen)
        assert "read-only here" in screen.message and "calu checker edit command" in screen.message
        assert "e edit" not in str(screen.query_one("#checkers-keys").render())
        await run.press("e")
        assert not screen.editing
        assert "calu checker edit command" in screen.message and "calu web" in screen.message


async def test_a_session_that_refuses_the_checkers_says_so() -> None:
    async with running("simple_turn") as run:
        run.session.fail["checkers.get"] = "the database is not reachable"
        await command(run, "/checkers")
        screen = run.app.screen
        assert isinstance(screen, CheckersScreen)
        assert screen.message == "the database is not reachable" and screen.current is None


# ------------------------------------------------------------------ honesty in the app

STATE = {
    "baseline": {"commit_id": "4f2a9c1d07aa", "source": "user", "subject": "Rename the helper"},
    "unreviewed": ["src/gate.py", "notes/plan.md"],
    "unreviewed_count": 2,
    "review_required": False,
    "review_running": False,
}
"""The honesty fields of a session that certifies commits."""

MARK = {"id": 4, "path": "src/gate.py", "symbol": "judge_step", "reason": "the docstring is old"}
"""One open mark."""


async def test_the_honesty_page_shows_mode_baseline_and_what_is_not_clean() -> None:
    async with running("simple_turn") as run:
        await run.session.emit("honesty.update", {"mode": "enforce", "marks": [MARK], **STATE})
        await run.pilot.pause()
        panel_text = run.line("panel-lines")
        assert "baseline 4f2a9c1d · by user" in panel_text
        assert "2 unreviewed files" in panel_text
        await command(run, "/honesty")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        for expected in (
            "mode      enforce · code runs and is committed only while the state is clean",
            "state     not clean",
            "baseline  4f2a9c1d07 · Rename the helper · trusted by user",
            "MARKED (1)",
            "src/gate.py:judge_step",
            "the docstring is old",
            "UNREVIEWED (2)",
            "  notes/plan.md",
            "/trust [commit or branch]",
        ):
            assert expected in page.text
        await run.session.emit(
            "honesty.update",
            {"mode": "enforce", "marks": [], **STATE, "unreviewed": [], "unreviewed_count": 0},
        )
        await run.pilot.pause()
        assert "state     clean" in page.text, "the open page follows the session"
        assert "every changed file was verified" in page.text


async def test_the_honesty_page_of_todays_session_says_what_is_not_reported() -> None:
    async with running("simple_turn") as run:
        await run.session.emit("honesty.update", {"mode": "track", "marks": [MARK]})
        await run.pilot.pause()
        await command(run, "/honesty")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        assert "state     1 open marks" in page.text
        assert "this session reports marks only" in page.text and "UNREVIEWED" not in page.text


async def test_trust_asks_names_the_commit_and_only_y_certifies() -> None:
    async with running("simple_turn") as run:
        run.session.honesty_requests = True
        await run.session.emit("honesty.update", {"mode": "track", "marks": []})
        run.app.describe_commit = lambda workspace, rev: "HEAD = 4f2a9c1 Rename the helper"
        await command(run, "/trust")
        box = run.app.screen
        assert isinstance(box, ConfirmScreen) and box.title_text == "Certify this commit as honest?"
        assert box.consequences[0] == "HEAD = 4f2a9c1 Rename the helper", "never cut"
        await run.press("enter")
        assert run.session.sent("honesty.trust") == []
        assert "nothing was certified" in run.line("composer-head")
        await command(run, "/trust")
        await run.press("y")
        await run.pilot.pause()
        assert run.session.sent("honesty.trust") == [{}]
        assert "trusted: 4f2a9c1d07 is certified as honest" in run.line("composer-head")
        assert "baseline 4f2a9c1d · by user" in run.line("panel-lines")


async def test_trust_of_a_branch_a_file_and_a_review() -> None:
    async with running("simple_turn") as run:
        run.session.honesty_requests = True
        await command(run, "/trust release")
        box = run.app.screen
        assert isinstance(box, ConfirmScreen) and box.consequences[0] == "release"
        await run.press("y")
        await run.pilot.pause()
        await command(run, "/trust-file")
        assert "usage: /trust-file <path>" in run.line("composer-head")
        await command(run, "/trust-file notes/plan.md")
        box = run.app.screen
        assert isinstance(box, ConfirmScreen) and box.consequences[0] == "notes/plan.md"
        await run.press("n")
        await command(run, "/trust-file notes/plan.md")
        await run.press("y")
        await run.pilot.pause()
        await command(run, "/honesty-review")
        assert run.session.sent("honesty.trust") == [{"rev": "release"}, {"path": "notes/plan.md"}]
        assert run.session.sent("honesty.review") == [{}]
        assert "the honesty review was asked for" in run.line("composer-head")


async def test_a_session_without_certification_says_so() -> None:
    async with running("simple_turn") as run:
        await command(run, "/trust")
        box = run.app.screen
        assert isinstance(box, ConfirmScreen) and box.consequences[0] == "HEAD"
        await run.press("y")
        await run.pilot.pause()
        assert "/trust: this session does not support it" in run.line("composer-head")
        await command(run, "/honesty-review")
        assert "this session does not support it" in run.line("composer-head")


async def test_a_stricter_mode_is_stored_at_once_and_a_weaker_one_after_a_yes() -> None:
    async with running("simple_turn") as run:
        access = run.app.admin
        assert isinstance(access, FakeAdmin)
        await command(run, "/honesty strict")
        assert "usage: /honesty [off | track | enforce]" in run.line("composer-head")
        await command(run, "/honesty enforce")
        assert access.modes == [("enforce", "myproject")]
        assert "honesty.mode is now enforce (global setting)" in run.line("composer-head")
        assert run.app.model.honesty_mode == "enforce", "announced by the session"
        await command(run, "/honesty enforce")
        assert "honesty.mode is enforce already" in run.line("composer-head")

        await command(run, "/honesty track")
        question = run.app.screen
        assert isinstance(question, ConfirmScreen)
        assert question.title_text == "Change honesty.mode from enforce to track?"
        await run.press("enter")
        assert access.modes == [("enforce", "myproject")], "Enter is no"
        assert "honesty.mode stays enforce" in run.line("composer-head")
        await command(run, "/honesty off")
        await run.press("y")
        await run.pilot.pause()
        assert access.modes[-1] == ("off", "myproject") and run.app.model.honesty_mode == "off"


async def test_the_mode_needs_the_credential_and_its_password() -> None:
    session = FakeSession()
    async with running(
        "simple_turn", session=session, admin=FakeAdmin(session, credential=False)
    ) as run:
        await command(run, "/honesty track")
        head = run.line("composer-head")
        assert "not changed here; use: calu config set honesty.mode track" in head
    session = FakeSession()
    access = FakeAdmin(session, password="secret")
    async with running("simple_turn", session=session, admin=access) as run:
        await command(run, "/honesty track")
        assert isinstance(run.app.screen, PasswordScreen)
        await run.press("escape")
        assert access.modes == [] and "no password given" in run.line("composer-head")
        await command(run, "/honesty track")
        await run.press(*"secret", "enter")
        await run.pilot.pause()
        assert access.modes == [("track", "myproject")]


# ------------------------------------------------------------------ f4


class Programs:
    """Records the programs the app starts instead of starting them."""

    def __init__(self, code: int = 0) -> None:
        """Start with no program run."""
        self.runs: list[tuple[tuple[str, ...], str]] = []
        self.code = code
        self.after: Any = None

    def __call__(self, argv: Sequence[str], cwd: str) -> int:
        """Record one start."""
        self.runs.append((tuple(argv), cwd))
        if self.after is not None:
            self.after()
        return self.code


async def test_f4_starts_the_installed_git_program_in_the_project_directory(
    tmp_path: Path,
) -> None:
    async with running("simple_turn", session=FakeSession(workspace=str(tmp_path))) as run:
        programs = Programs()
        run.app.run_program = programs
        run.app.which = installed("lazygit", "tig", "git")
        assert "f4 git" in run.line("footer")
        await run.press("f4")
        assert programs.runs == [(("lazygit",), str(tmp_path))]
        assert "back from lazygit" in run.line("composer-head")
        run.app.environ = {"CALU_GIT_TOOL": "tig status"}
        await command(run, "/git")
        assert programs.runs[-1] == (("tig", "status"), str(tmp_path))
        run.app.environ = {"CALU_GIT_TOOL": "gitui"}
        await run.press("f4")
        assert len(programs.runs) == 2
        assert "CALU_GIT_TOOL names gitui, which is not installed" in run.line("composer-head")


async def test_f4_needs_the_project_directory_on_this_machine() -> None:
    async with running("simple_turn") as run:
        programs = Programs()
        run.app.run_program = programs
        run.app.which = installed("lazygit", "git")
        await run.press("f4")
        assert programs.runs == []
        assert "needs a terminal on the session's machine" in run.line("composer-head")


async def test_without_a_git_program_the_install_command_is_shown_and_never_run_with_sudo(
    tmp_path: Path,
) -> None:
    async with running("simple_turn", session=FakeSession(workspace=str(tmp_path))) as run:
        programs = Programs()
        run.app.run_program = programs
        run.app.which = installed("git", "apt")
        await run.press("f4")
        notice = run.app.screen
        assert isinstance(notice, ConfirmScreen) and notice.notice
        assert notice.command == "sudo apt install lazygit", "on a line of its own"
        assert len(notice.consequences) == 1, "one sentence, the rest is in the help"
        assert "calu runs no command with sudo" in notice.footnote
        assert "CALU_GIT_TOOL" in notice.footnote
        await run.press("c")
        assert notice.copied and run.app.screen is notice, "c copies the command"
        await run.press("y")
        assert run.app.screen is notice and programs.runs == [], "y runs nothing here"
        await run.press("enter")
        assert [argv[0] for argv, _ in programs.runs] == ["git"], "git diff through the pager"
        assert "that was git diff" in run.line("composer-head")
        await run.press("f4")
        assert not isinstance(run.app.screen, ConfirmScreen), "the notice is shown once"
        assert len(programs.runs) == 2


async def test_an_install_without_sudo_runs_only_after_a_yes(tmp_path: Path) -> None:
    async with running("simple_turn", session=FakeSession(workspace=str(tmp_path))) as run:
        programs = Programs()
        run.app.run_program = programs
        run.app.which = installed("git", "brew")
        await run.press("f4")
        question = run.app.screen
        assert isinstance(question, ConfirmScreen) and not question.notice
        await run.press("n")
        assert [argv[0] for argv, _ in programs.runs] == ["git"], "no: git diff, no install"

    async with running("simple_turn", session=FakeSession(workspace=str(tmp_path))) as run:
        programs = Programs()
        run.app.run_program = programs
        run.app.which = installed("git", "brew")
        app = run.app

        def now_installed() -> None:
            """After the install command, lazygit is on the path."""
            app.which = installed("git", "brew", "lazygit")

        programs.after = now_installed
        await run.press("f4", "y")
        assert programs.runs == [
            (("brew", "install", "lazygit"), str(tmp_path)),
            (("lazygit",), str(tmp_path)),
        ]


async def test_a_failed_install_is_reported_and_nothing_else_starts(tmp_path: Path) -> None:
    async with running("simple_turn", session=FakeSession(workspace=str(tmp_path))) as run:
        programs = Programs(code=1)
        run.app.run_program = programs
        run.app.which = installed("git", "go")
        await run.press("f4", "y")
        assert len(programs.runs) == 1 and programs.runs[0][0][:2] == ("go", "install")
        assert "ended with exit code 1" in run.line("composer-head")


async def test_help_and_palette_name_the_new_commands() -> None:
    async with running("simple_turn") as run:
        await run.press("f1")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        text = " ".join(page.text.split())
        for expected in (
            "/trust [commit or branch]",
            "/trust-file <path>",
            "/honesty-review",
            "/honesty [off | track | enforce]",
            "/git",
            "f4",
            "lazygit by default",
            "CALU_GIT_TOOL",
        ):
            assert expected in text
