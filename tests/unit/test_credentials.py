import base64
import json
import os
import stat
from pathlib import Path

import pytest

from calu import preflight
from calu.claude.sdk_runner import SdkRunner
from calu.config import CheckerConfig, ClaudeConfig, Config, load_secrets
from calu.credentials import (
    CorruptStore,
    CredentialStore,
    UnknownCredential,
    WrongPassword,
    default_path,
    wipe,
)


def test_save_and_load_round_trip(store: CredentialStore) -> None:
    store.save("admin", "postgresql://u:p@h/db", bytearray(b"secret"))
    assert store.load("admin", "secret") == bytearray(b"postgresql://u:p@h/db")
    assert store.names() == ["admin"]
    assert "admin" in store
    assert "session" not in store


def test_wrong_password_and_unknown_name(store: CredentialStore) -> None:
    store.save("admin", "x", "right")
    with pytest.raises(WrongPassword):
        store.load("admin", "wrong")
    with pytest.raises(UnknownCredential):
        store.load("session", "right")


def test_file_permissions_and_no_plaintext(store: CredentialStore) -> None:
    store.save("admin", "postgresql://owner:TOPSECRET@h/db", "pw")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert "TOPSECRET" not in store.path.read_text()


def test_entry_bound_to_its_name(store: CredentialStore) -> None:
    store.save("admin", "x", "pw")
    data = json.loads(store.path.read_text())
    data["entries"]["session"] = data["entries"]["admin"]
    store.path.write_text(json.dumps(data))
    with pytest.raises(WrongPassword):
        store.load("session", "pw")


def test_replace_and_remove_keep_other_entries(store: CredentialStore) -> None:
    store.save("admin", "a1", "pw")
    store.save("session", "s", "pw2")
    store.save("admin", "a2", "pw3")
    assert store.load("admin", "pw3") == bytearray(b"a2")
    assert store.load("session", "pw2") == bytearray(b"s")
    assert store.remove("admin")
    assert not store.remove("admin")
    assert store.names() == ["session"]


def test_corrupt_file(store: CredentialStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json")
    with pytest.raises(CorruptStore):
        store.names()
    store.path.write_text('{"version": 99, "entries": {}}')
    with pytest.raises(CorruptStore):
        store.names()


def test_empty_name_or_password_rejected(store: CredentialStore) -> None:
    with pytest.raises(ValueError):
        store.save("", "x", "pw")
    with pytest.raises(ValueError):
        store.save("admin", "x", "")


def test_wipe_zeroes_in_place() -> None:
    buf = bytearray(b"secret")
    wipe(buf)
    assert buf == bytearray(6)


def test_default_path_uses_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert str(default_path()) == "/tmp/xdg/calu/credentials.json"


def test_an_anthropic_key_serves_the_checkers_and_is_hidden_from_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D37: the checkers may use the Anthropic API while the main chat keeps the subscription."""
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-for-the-checkers\n", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    loaded = load_secrets(env_file)
    # The key is in calu's own environment, so a checker model can use it ...
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-for-the-checkers"
    assert "ANTHROPIC_API_KEY" in loaded

    # ... and blanked for Claude Code, which therefore stays on whatever it is set up with.
    config = ClaudeConfig(model="opus")
    runner = SdkRunner(tmp_path, config, loaded)
    assert runner._claude_env()["ANTHROPIC_API_KEY"] == ""
    assert "CLAUDE_CODE_USE_BEDROCK" not in runner._claude_env()

    # Every provider key calu knows is blanked, not only the ones that are set.
    assert runner._claude_env()["MISTRAL_API_KEY"] == ""

    # Bedrock for Claude itself is the separate switch, and does not touch the checker key.
    bedrock = SdkRunner(tmp_path, ClaudeConfig(use_bedrock=True), loaded)._claude_env()
    assert bedrock["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert bedrock["ANTHROPIC_API_KEY"] == ""


def test_a_missing_anthropic_key_for_a_checker_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checker on the Anthropic API without the key must not fail silently at the first step."""
    config = Config(command_checker=CheckerConfig(model="anthropic:claude-haiku-4-5"))
    about_anthropic = [w for w in preflight.key_warnings(config, {}) if "ANTHROPIC" in w]
    assert len(about_anthropic) == 1
    assert "command checker" in about_anthropic[0]
    # With the key in the environment there is nothing left to say about this provider.
    with_key = preflight.key_warnings(config, {"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert [w for w in with_key if "ANTHROPIC" in w] == []


def test_an_entry_can_be_protected_by_the_file_mode_alone(store: CredentialStore) -> None:
    """D104: a password typed for every `calu sessions` is a password people take off.

    The entry is readable without a password, says so through `needs_password`, and the file
    still has no plaintext of any *encrypted* entry -- but this one is plainly in it, which is
    the cost and is reported rather than hidden.
    """
    store.save("operator", "postgresql://op:p@h/db", None)
    assert store.needs_password("operator") is False
    assert store.load("operator") == bytearray(b"postgresql://op:p@h/db")
    # A password offered for such an entry is ignored rather than refused: a caller that asks
    # `needs_password` first never passes one, and one that does should not get an error it
    # cannot act on.
    assert store.load("operator", "anything") == bytearray(b"postgresql://op:p@h/db")

    store.save("admin", "postgresql://a:p@h/db", "pw")
    assert store.needs_password("admin") is True
    assert sorted(store.names()) == ["admin", "operator"]
    text = store.path.read_text(encoding="utf-8")
    assert "postgresql://a:p@h/db" not in text, "the encrypted entry must stay encrypted"
    assert (
        "postgresql://op:p@h/db"
        in base64.b64decode(json.loads(text)["entries"]["operator"]["secret"]).decode()
    )

    with pytest.raises(UnknownCredential):
        store.needs_password("nothing")


def test_an_encrypted_entry_read_without_a_password_is_an_error(store: CredentialStore) -> None:
    """Silently returning nothing, or treating None as the password, would both be worse."""
    store.save("admin", "postgresql://a:p@h/db", "pw")
    with pytest.raises(CorruptStore) as raised:
        store.load("admin")
    assert "without a password" in str(raised.value)
