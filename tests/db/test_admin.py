"""Changing projects, configuration, checker definitions and rules in the database."""

from pathlib import Path

import psycopg
import pytest

from calu import admin
from calu.db import Db, connect
from calu.setup import setup
from calu.store import ConfigStore

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def prepared(db_url: str) -> tuple[Db, psycopg.AsyncConnection]:
    """Return an owner connection on a database with the defaults loaded."""
    conn = await connect(db_url)
    await setup(conn, db_url, "test:model")
    return Db(conn), conn


async def test_projects_are_registered_and_listed(db_url: str) -> None:
    db, conn = await prepared(db_url)
    try:
        row = await admin.add_project(db, "demo", "git@example.com:demo.git")
        # A new project gets the careful policy and a starter allow list (D45).
        assert row.name == "demo" and row.network_policy == "proxy"
        seeded = (await admin.settings(db, "demo"))["claude"]["sandbox"]["allowed_domains"]
        assert "pypi.org" in seeded and "github.com" in seeded
        assert "*" not in seeded, "a new project must not reach every domain"
        # The old behaviour is one flag away, and then nothing is seeded.
        wide = await admin.add_project(db, "wide", None, "full")
        assert wide.network_policy == "full"
        assert "allowed_domains" not in (await admin.settings(db, "wide")).get("claude", {}).get(
            "sandbox", {}
        )
        assert "demo" in row.line() and "example.com" in row.line()
        with pytest.raises(admin.AdminError, match="exists already"):
            await admin.add_project(db, "demo")
        with pytest.raises(admin.AdminError, match="network policy"):
            await admin.add_project(db, "other", network_policy="sometimes")
        with pytest.raises(admin.AdminError, match="needs a name"):
            await admin.add_project(db, "  ")
        assert [p.name for p in await admin.projects(db)] == ["demo", "wide"]
        with pytest.raises(admin.AdminError, match="no project named"):
            await admin.project_id(db, "nope")
    finally:
        await conn.close()


async def test_configuration_values_are_merged_validated_and_reloaded(db_url: str, tmp_path):
    db, conn = await prepared(db_url)
    try:
        await admin.add_project(db, "demo")
        await admin.set_setting(db, "claude.model", "sonnet")
        await admin.set_setting(db, "context.narrate", False)
        stored = await admin.set_setting(db, "claude.effort", "low")
        assert stored == {
            "claude": {"model": "sonnet", "effort": "low"},
            "context": {"narrate": False},
        }
        await admin.set_setting(db, "claude.model", "haiku", project="demo")

        # A session sees the merged result.
        settings = await ConfigStore(db, tmp_path, "demo").load()
        assert settings.config.claude.model == "haiku"
        assert settings.config.claude.effort == "low"
        assert settings.config.context.narrate is False

        with pytest.raises(admin.AdminError, match="would be invalid"):
            await admin.set_setting(db, "context.fold_every", 0)
        with pytest.raises(admin.AdminError, match="not a configuration key"):
            await admin.set_setting(db, "claude..model", "x")
        with pytest.raises(admin.AdminError, match="no project named"):
            await admin.set_setting(db, "claude.model", "x", project="nope")

        left = await admin.unset_setting(db, "claude.effort")
        assert left == {"claude": {"model": "sonnet"}, "context": {"narrate": False}}
        assert await admin.unset_setting(db, "nothing.here") == left
    finally:
        await conn.close()


def test_values_are_parsed_as_json_with_a_text_fallback() -> None:
    assert admin.parse_value("3") == 3
    assert admin.parse_value("true") is True
    assert admin.parse_value("null") is None
    assert admin.parse_value('["a", "b"]') == ["a", "b"]
    assert admin.parse_value("opus") == "opus"
    assert admin.parse_value("mistral:mistral-small-2603") == "mistral:mistral-small-2603"


async def test_checker_definitions_are_changed_and_versioned(db_url: str) -> None:
    db, conn = await prepared(db_url)
    try:
        names = [c.name for c in await admin.checkers(db)]
        assert names == ["changeset", "command", "edit", "narrator", "subagent-result"]
        assert (await admin.instructions(db, "command")).startswith("#")
        with pytest.raises(admin.AdminError, match="no checker named"):
            await admin.instructions(db, "nope")

        row = await admin.set_checker(db, "edit", instructions_text="# My rules\n", max_tokens=99)
        assert row.version == 2 and row.max_tokens == 99
        assert await admin.instructions(db, "edit") == "# My rules\n"
        disabled = await admin.set_checker(db, "edit", enabled=False)
        assert not disabled.enabled and disabled.version == 3
        assert "(disabled)" in disabled.line()

        with pytest.raises(admin.AdminError, match="must not be empty"):
            await admin.set_checker(db, "edit", instructions_text="  ")
        with pytest.raises(admin.AdminError, match="route must be one of"):
            await admin.set_checker(db, "edit", route="sideways")
        with pytest.raises(admin.AdminError, match="does not exist"):
            await admin.set_checker(db, "extra", model="test:model")

        new = await admin.set_checker(
            db, "extra", instructions_text="# Extra\n", route="command", model="test:other"
        )
        assert new.name == "extra" and new.route == "command" and new.model == "test:other"
        assert new.version == 1
    finally:
        await conn.close()


async def test_rules_are_added_validated_and_removed(db_url: str, tmp_path) -> None:
    db, conn = await prepared(db_url)
    try:
        await admin.add_project(db, "demo")
        rule = await admin.add_rule(
            db, "command-deny", "^ship-it( |$)", comment="never", project="demo"
        )
        assert rule.scope == "demo" and rule.position == 0
        assert "command-deny" in rule.line() and "# never" in rule.line()
        second = await admin.add_rule(db, "command-deny", "^ship-it-2$", project="demo")
        assert second.position == 1

        with pytest.raises(admin.AdminError, match="rule list must be one of"):
            await admin.add_rule(db, "command-maybe", "^x$")
        with pytest.raises(admin.AdminError, match="not a valid regular expression"):
            await admin.add_rule(db, "command-deny", "^(")
        with pytest.raises(admin.AdminError, match="needs a pattern"):
            await admin.add_rule(db, "command-deny", "  ")

        # The project's rules are in force for that project only.
        settings = await ConfigStore(db, tmp_path, "demo").load()
        patterns = [r.pattern.pattern for r in settings.rules.lists("command-deny")[0]]
        assert "^ship-it( |$)" in patterns
        global_only = await ConfigStore(db, tmp_path).load()
        assert "^ship-it( |$)" not in [
            r.pattern.pattern for r in global_only.rules.lists("command-deny")[0]
        ]

        listed = await admin.rules(db, "demo", "command-deny")
        assert [r.id for r in listed][-2:] == [rule.id, second.id]
        assert all(r.list == "command-deny" for r in listed)
        with pytest.raises(admin.AdminError, match="rule list must be one of"):
            await admin.rules(db, rule_list="nonsense")

        assert await admin.set_rule(db, rule.id, False) is True
        assert not next(r for r in await admin.rules(db, "demo") if r.id == rule.id).enabled
        assert await admin.set_rule(db, 999_999, False) is False
        assert await admin.remove_rule(db, rule.id) is True
        assert await admin.remove_rule(db, rule.id) is False
    finally:
        await conn.close()


async def test_a_change_announces_itself_to_running_sessions(db_url: str, tmp_path) -> None:
    db, conn = await prepared(db_url)
    listener = await connect(db_url)
    try:
        from calu.db import CONFIG_CHANGED, listen

        await listen(listener, [CONFIG_CHANGED])
        await admin.set_setting(db, "claude.model", "sonnet")
        import asyncio

        notify = await asyncio.wait_for(anext(listener.notifies()), timeout=5)
        assert notify.channel == CONFIG_CHANGED
    finally:
        await conn.close()
        await listener.close()


async def test_a_whole_rule_list_is_replaced_from_its_file_form(db_url: str, tmp_path) -> None:
    db, conn = await prepared(db_url)
    try:
        await admin.add_project(db, "demo")
        first = await admin.add_rule(db, "command-deny", "^old-one$", "keep me", "demo")
        disabled = await admin.add_rule(db, "command-deny", "^switched-off$", project="demo")
        await admin.set_rule(db, disabled.id, False)

        rows = [r for r in await admin.rules(db, "demo", "command-deny") if r.scope == "demo"]
        text = admin.render_rule_list([r for r in rows if r.enabled], "command-deny", "demo")
        assert "^old-one$" in text and "^switched-off$" not in text

        edited = text.replace("^old-one$", "^new-one$") + "\n# also this\n^second$\n"
        removed, stored = await admin.replace_rule_list(db, "command-deny", edited, "demo")
        assert (removed, stored) == (1, 2)

        after = [r for r in await admin.rules(db, "demo", "command-deny") if r.scope == "demo"]
        assert [(r.pattern, r.comment) for r in after if r.enabled] == [
            ("^new-one$", "keep me"),
            ("^second$", "also this"),
        ]
        # A disabled rule is left untouched (its position may now interleave; it is inactive).
        assert [r.pattern for r in after if not r.enabled] == ["^switched-off$"]
        assert first.id not in [r.id for r in after]

        # A bad pattern changes nothing.
        with pytest.raises(admin.AdminError, match="not a valid regular expression"):
            await admin.replace_rule_list(db, "command-deny", "^(\n", "demo")
        assert len([r for r in await admin.rules(db, "demo", "command-deny") if r.enabled]) >= 2
        with pytest.raises(admin.AdminError, match="rule list must be one of"):
            await admin.replace_rule_list(db, "command-perhaps", "^x$")
        with pytest.raises(admin.AdminError, match="no project named"):
            await admin.replace_rule_list(db, "command-deny", "^x$", "nope")

        # An empty file removes the enabled rules of that scope and leaves the global ones.
        removed, stored = await admin.replace_rule_list(db, "command-deny", "", "demo")
        assert (removed, stored) == (2, 0)
        left = await admin.rules(db, "demo", "command-deny")
        assert [r.scope for r in left if r.enabled] == ["global"] * len(
            [r for r in left if r.enabled]
        )

        # The session sees the replacement.
        settings = await ConfigStore(db, tmp_path, "demo").load()
        patterns = [r.pattern.pattern for r in settings.rules.lists("command-deny")[0]]
        assert "^new-one$" not in patterns and "^old-one$" not in patterns
    finally:
        await conn.close()


async def test_a_projects_network_policy_shapes_the_sandbox(db_url: str) -> None:
    """D39: the column was inert until now — a session must actually get the policy."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        await admin.add_project(db, "narrow", network_policy="proxy")
        await admin.set_setting(
            db, "claude.sandbox.allowed_domains", ["pypi.org"], project="narrow"
        )

        narrow = await ConfigStore(db, Path("/w"), "narrow").load()
        assert narrow.config.claude.sandbox.allowed_domains == ["pypi.org"]

        # "none" clears the allow list, whatever the settings say, so nothing is reachable.
        await admin.set_network_policy(db, "narrow", "none")
        closed = await ConfigStore(db, Path("/w"), "narrow").load()
        assert closed.config.claude.sandbox.allowed_domains == []
        assert "allowedDomains" not in closed.config.claude.sandbox.settings()["sandbox"].get(
            "network", {}
        )

        # "full" is the wildcard the spike verified against the real CLI.
        await admin.set_network_policy(db, "narrow", "full")
        open_network = await ConfigStore(db, Path("/w"), "narrow").load()
        assert open_network.config.claude.sandbox.allowed_domains == ["*"]

        # A session without a project keeps the configured settings untouched.
        await admin.set_setting(db, "claude.sandbox.allowed_domains", ["global.example"])
        plain = await ConfigStore(db, Path("/w"), None).load()
        assert plain.config.claude.sandbox.allowed_domains == ["global.example"]

        with pytest.raises(admin.AdminError):
            await admin.set_network_policy(db, "narrow", "wide-open")
        with pytest.raises(admin.AdminError):
            await admin.set_network_policy(db, "no-such-project", "none")
    finally:
        await conn.close()


async def test_one_entry_of_a_list_setting_is_added_and_removed(db_url: str, tmp_path) -> None:
    """`config add` and `config remove` keep the other entries, defaults included."""
    db, conn = await prepared(db_url)
    try:
        key = "claude.sandbox.deny_read"
        defaults = admin.effective_value(await admin.settings(db), key)
        assert len(defaults) > 5, "the default deny list should not be nearly empty"

        # Adding one path keeps every default path.
        merged, added = await admin.add_to_setting(db, key, "~/secrets")
        assert added is True
        stored = merged["claude"]["sandbox"]["deny_read"]
        assert stored == [*defaults, "~/secrets"]

        # Adding it twice changes nothing and says so.
        merged, added = await admin.add_to_setting(db, key, "~/secrets")
        assert added is False
        assert merged["claude"]["sandbox"]["deny_read"] == stored

        # Removing an entry that is only in the defaults is allowed.
        merged = await admin.remove_from_setting(db, key, defaults[0])
        after = merged["claude"]["sandbox"]["deny_read"]
        assert defaults[0] not in after
        assert "~/secrets" in after and len(after) == len(defaults)

        # A session really uses the result.
        settings = await ConfigStore(db, tmp_path).load()
        assert settings.config.claude.sandbox.deny_read == after

        # What cannot work is refused, and nothing is written.
        with pytest.raises(admin.AdminError, match="is not in"):
            await admin.remove_from_setting(db, key, "never-there")
        with pytest.raises(admin.AdminError, match="not a list but a str"):
            await admin.add_to_setting(db, "claude.model", "opus")
        with pytest.raises(admin.AdminError, match="has no"):
            await admin.add_to_setting(db, "claude.nonsense", "x")
        assert (await admin.settings(db))["claude"]["sandbox"]["deny_read"] == after
    finally:
        await conn.close()


async def test_a_list_entry_can_be_added_for_one_project_only(db_url: str) -> None:
    """A project scope starts from the list a session there would use."""
    db, conn = await prepared(db_url)
    try:
        # "none" seeds nothing, so this starts from an empty list.
        await admin.add_project(db, "demo", "git@example.com:demo.git", "none")
        key = "claude.sandbox.allowed_domains"
        merged, added = await admin.add_to_setting(db, key, "docs.example.com", project="demo")
        assert added is True
        assert merged["claude"]["sandbox"]["allowed_domains"] == ["docs.example.com"]
        # The global scope is untouched.
        assert "allowed_domains" not in (await admin.settings(db)).get("claude", {}).get(
            "sandbox", {}
        )
        # A domain the starter list of a "proxy" project already has is not added twice.
        await admin.add_project(db, "narrow", None, "proxy")
        _, again = await admin.add_to_setting(db, key, "pypi.org", project="narrow")
        assert again is False
    finally:
        await conn.close()
