"""What a browser may ask a session for, checked against what the session answers at all.

The relay forwards only the requests in `calu.web.app.LIVE_METHODS` and refuses everything
else, which is a default-deny list and therefore only as good as its being kept up to date. Two
ways it can rot: a name in it that the protocol does not answer (dead weight that reads like a
feature), and a new protocol method that nobody decided about -- it is then silently refused to
every browser, which is the safe direction but not a decision anyone made.
"""

import ast
from pathlib import Path

import pytest

import calu.protocol.server as protocol_server

pytest.importorskip("starlette", reason="the web interface needs the `web` extra")

from calu.web.app import LIVE_METHODS  # imported after the guard above

WITHHELD = {
    "initialize": "the relay authenticates for the browser; it holds the token, not the page",
    "events.subscribe": "the relay subscribes once and fans the events out itself",
    "comment.add": "the web interface has no line-selection UI; comments come from an editor",
    "shutdown": "a browser must not be able to end the calu process by accident (D34)",
}
"""Protocol methods a browser deliberately cannot reach, and why."""


def dispatched_methods() -> set[str]:
    """Return the request methods the protocol server answers, from its own `match`.

    Returns:
        Every method name in a `case` of `Client._dispatch`.

    Raises:
        AssertionError: If the parse finds almost nothing, which would make the check useless.
    """
    source = Path(protocol_server.__file__).read_text(encoding="utf-8")
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.match_case):
            continue
        patterns = (
            node.pattern.patterns if isinstance(node.pattern, ast.MatchOr) else [node.pattern]
        )
        for pattern in patterns:
            if isinstance(pattern, ast.MatchValue) and isinstance(pattern.value, ast.Constant):
                value = pattern.value.value
                if isinstance(value, str) and value:
                    found.add(value)
    assert len(found) > 10, f"the parse found only {found}; the check would prove nothing"
    return found


def test_the_browser_may_only_ask_for_methods_the_session_answers() -> None:
    """A name the protocol does not know would read like a feature and do nothing."""
    unknown = sorted(LIVE_METHODS - dispatched_methods())
    assert unknown == [], f"the relay forwards these, which no session answers: {unknown}"


def test_every_protocol_method_is_either_offered_to_a_browser_or_withheld_on_purpose() -> None:
    """A new method must be decided about, not left to the default.

    Being refused is the safe direction, so this does not protect the session; it protects the
    decision from being made by forgetting. A method added to the protocol fails here until it
    is either in `LIVE_METHODS` or in `WITHHELD` with a reason.
    """
    undecided = sorted(dispatched_methods() - LIVE_METHODS - set(WITHHELD))
    assert undecided == [], (
        "these protocol methods are neither offered to a browser nor listed as withheld: "
        f"{undecided}. Add them to LIVE_METHODS, or to WITHHELD with the reason."
    )
    # And the withheld list stays honest in the other direction.
    assert not (set(WITHHELD) & LIVE_METHODS), "a method cannot be both withheld and offered"
    stale = sorted(set(WITHHELD) - dispatched_methods())
    assert stale == [], f"WITHHELD names methods the protocol no longer has: {stale}"


def test_calu_web_refuses_a_reachable_address_before_asking_for_a_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one wiring of `calu web` worth a test: which interface it binds (D121).

    The web interface has no login, so binding a reachable address hands everything the `admin`
    credential can do to whoever finds the port. The refusal has to come **before** the
    credential is read, or a user who mistyped `--host` is first asked for a password and then
    refused -- and `calu web` was the only command no test ran at all.
    """
    from calu import cli
    from calu.credentials import CredentialStore

    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", "postgresql://admin@localhost/calu", None)
    asked: list[str] = []
    monkeypatch.setattr(cli, "read_password", lambda *a, **k: asked.append("asked") or bytearray())

    for host in ("0.0.0.0", "192.168.1.10", "::", "example.com"):
        code = cli.main(["web", "--host", host], store_factory=lambda: store)
        said = capsys.readouterr().err
        assert code == 1, f"{host} was accepted"
        assert "127.0.0.1" in said or "not an IP address" in said, said
    assert asked == [], "the address is checked before a password is asked for"

    # A loopback address passes the check; it is not served here, because that would block.
    from calu.web.app import check_host

    check_host("127.0.0.1")
    check_host("::1")
