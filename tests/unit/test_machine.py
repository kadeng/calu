"""The identity of a process table, and what it says about a recorded session (D202)."""

from pathlib import Path

from calu.machine import Machine, here


def test_the_identity_is_read_from_the_three_places_and_survives_storage(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/machine-id").write_text("4c1f\n", encoding="ascii")
    (tmp_path / "proc/sys/kernel/random").mkdir(parents=True)
    (tmp_path / "proc/sys/kernel/random/boot_id").write_text("9b0e\n", encoding="ascii")
    (tmp_path / "proc/self/ns").mkdir(parents=True)
    (tmp_path / "proc/self/ns/pid").symlink_to("pid:[4026531836]")
    found = here(tmp_path)
    assert found == Machine("4c1f", "9b0e", "pid:[4026531836]")
    assert found.text() == "4c1f|9b0e|pid:[4026531836]"
    assert Machine.parse(found.text()) == found
    # A system that provides none of them (no /proc, a container without a machine id).
    assert here(tmp_path / "missing") == Machine()
    assert Machine.parse(Machine().text()) is None
    assert Machine.parse(None) is None and Machine.parse("") is None
    assert Machine.parse("not an identity") is None


def test_this_machine_has_a_boot_id_and_a_pid_namespace() -> None:
    real = here()
    assert real.boot_id and real.pid_namespace.startswith("pid:")
    assert real.relation(real) == "same"


def test_only_the_same_process_table_or_a_reboot_says_something_about_a_process() -> None:
    this = Machine("m1", "boot2", "pid:[1]")
    assert this.relation(Machine("m1", "boot2", "pid:[1]")) == "same"
    # Another container of the same host: the same kernel, another process table.
    assert this.relation(Machine("m1", "boot2", "pid:[2]")) == "other"
    # This machine before its last reboot: whatever ran then is gone.
    assert this.relation(Machine("m1", "boot1", "pid:[1]")) == "rebooted"
    # Another machine, whatever its host name is.
    assert this.relation(Machine("m2", "boot9", "pid:[1]")) == "other"
    # Without a machine id a different boot id may be a reboot or another machine.
    assert Machine("", "boot2", "pid:[1]").relation(Machine("", "boot1", "pid:[1]")) == "other"
    assert this.relation(Machine("m1", "", "pid:[1]")) == "other"
    assert Machine("m1", "", "").relation(Machine("m1", "", "")) == "other"
