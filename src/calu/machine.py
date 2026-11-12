"""Which machine a process runs on, as far as its process table is concerned (PLAN.md D202).

`calu sessions` closes the open escalations of a silent session whose process is gone
(`calu.herd.close_gone`). "Gone" is read from this machine's process table, so it only means
something for a session that ran in the same process table. The host name does not say that:
two machines, or two containers, may have the same one. A session therefore records this
identity when it starts (`sessions.machine`), and the listing compares it with its own:

    recorded identity vs. this process          the session's process ...
    same boot, same pid namespace               is in this process table: its pid is looked up
    same machine id, another boot               ended with the reboot: it is gone
    anything else, or a part is missing         cannot be judged from here

The three parts, each "" where the system does not provide it:

- machine id: `/etc/machine-id` (written once at installation; containers often have none);
- boot id: `/proc/sys/kernel/random/boot_id` (new at every boot of the kernel; containers of
  one host share it);
- pid namespace: the link `/proc/self/ns/pid` (differs between containers of one host).

Limit: two machines cloned from one image share the machine id. Together with the same host
name, a session of the one then looks like a session from before a reboot on the other. A
session that still beats is never judged at all, so this needs a silent session as well.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Relation = Literal["same", "rebooted", "other"]
"""Where a recorded session ran, seen from this process (the table in the module docstring)."""


@dataclass(frozen=True)
class Machine:
    """The identity of a process table: machine id, boot id and pid namespace."""

    machine_id: str = ""
    boot_id: str = ""
    pid_namespace: str = ""

    def text(self) -> str:
        """Return the identity as stored in `sessions.machine`.

        Returns:
            The three parts joined with "|", for example
            `4c1f...|9b0e...|pid:[4026531836]`. None of the parts contains that character.
        """
        return "|".join((self.machine_id, self.boot_id, self.pid_namespace))

    @classmethod
    def parse(cls, text: str | None) -> Machine | None:
        """Read an identity back from `sessions.machine`.

        Args:
            text: The stored value; None or "" for a session of a calu that recorded none.

        Returns:
            The identity, or None if nothing was recorded or the value does not have three
            parts.
        """
        parts = (text or "").split("|")
        if len(parts) != 3 or not any(parts):
            return None
        return cls(*parts)

    def relation(self, recorded: Machine) -> Relation:
        """Say where a session with a recorded identity ran, seen from this identity.

        Args:
            recorded: The identity the session recorded when it started.

        Returns:
            "same" if both have the same boot id and pid namespace, both known; "rebooted" if
            both have the same known machine id and different known boot ids; "other" in
            every other case, which includes every case with a missing part that would be
            needed to decide.
        """
        if (
            self.boot_id
            and self.pid_namespace
            and self.boot_id == recorded.boot_id
            and self.pid_namespace == recorded.pid_namespace
        ):
            return "same"
        if (
            self.machine_id
            and self.machine_id == recorded.machine_id
            and self.boot_id
            and recorded.boot_id
            and self.boot_id != recorded.boot_id
        ):
            return "rebooted"
        return "other"


def here(root: Path = Path("/")) -> Machine:
    """Read the identity of the process table this process runs in.

    Args:
        root: The directory that holds `etc` and `proc`; tests pass a directory of their own.

    Returns:
        The identity; a part the system does not provide is "".
    """

    def read(relative: str) -> str:
        """Read one small file; "" if it cannot be read.

        Args:
            relative: The path below `root`.

        Returns:
            The content without surrounding whitespace and without "|".
        """
        try:
            return (root / relative).read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            return ""

    try:
        namespace = os.readlink(root / "proc/self/ns/pid")
    except OSError:
        namespace = ""
    return Machine(
        machine_id=read("etc/machine-id").replace("|", ""),
        boot_id=read("proc/sys/kernel/random/boot_id").replace("|", ""),
        pid_namespace=namespace.replace("|", ""),
    )
