"""A client as real root asks the admin password once and keeps the login in memory (D249).

`calu attach --sudo` holds its database login before it switches to the user's file
identity; afterwards root's credential store is closed to it, and every database feature of
the full-screen client runs with the held login, without a dialog. The test runs that part
of the client in a process of its own (the switch lasts for the rest of a process), against
the box's test server, whose owner is not a superuser (the case of a managed database).
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from calu.credentials import CredentialStore
from calu.db import connect
from calu.setup import enable_admin_login, setup
from tests.root.box import AS_ROOT, OTHER_UID, WHY_NOT

pytestmark = [pytest.mark.root, pytest.mark.skipif(not AS_ROOT, reason=WHY_NOT)]

PASSWORD = "typed once"

CLIENT = """
    import json, os, subprocess, sys
    from pathlib import Path
    import anyio
    from calu import asuser, cli
    from calu.credentials import CredentialStore
    from calu.tui.adminaccess import StoreAdmin

    store = CredentialStore(Path({store!r}))
    asked = []

    def answer(prompt, confirm=False):
        asked.append(prompt)
        return bytearray({password!r}.encode())

    cli.read_password = answer
    problem = cli._hold_for_client(store, True)
    held = cli._held[0]
    run_as = asuser.resolve("{uid}:{uid}", Path({state!r}))
    asuser.prepare(run_as, Path({state!r}))
    asuser.enter(run_as, Path({state!r}))
    try:
        store.path.read_bytes()
        store_after = "read"
    except PermissionError:
        store_after = "refused"
    access = StoreAdmin(store, database=held)

    async def use():
        rows = await access.sessions(None)
        saved = await access.save_instructions("command", "Saved by the root client.", None)
        mode = await access.set_honesty_mode("track", None, None)
        return len(rows), saved.version, mode.mode

    count, version, mode = anyio.run(use)
    child = subprocess.run(["env"], capture_output=True, text=True, **asuser.child())
    print(json.dumps({{
        "problem": problem,
        "asked": len(asked),
        "role": held.user,
        "needs_password": access.needs_password(),
        "store_after": store_after,
        "sessions": count,
        "version": version,
        "mode": mode,
        "in_environment": {password!r} in "".join(os.environ.values()),
        "in_child": {password!r} in child.stdout,
    }}))
"""
"""The client's part, filled in with `str.format`."""


@pytest.fixture
def state_root() -> Iterator[Path]:
    """A state root every user can enter, for `asuser.prepare`."""
    with tempfile.TemporaryDirectory(prefix="calu-root-client-") as name:
        path = Path(name)
        path.chmod(0o755)
        yield path


@pytest.mark.db
@pytest.mark.anyio
@pytest.mark.parametrize("role_accepts", [True, False], ids=["calu_admin", "owner-fallback"])
async def test_one_admin_password_serves_every_database_feature_after_the_switch(
    db_url: str, tmp_path: Path, state_root: Path, role_accepts: bool
) -> None:
    """D249: asked once; `calu_admin` held where the role accepts it, else the owner login.

    Without the role's login (a blank admin password: PostgreSQL accepts none) the owner
    login that the password opened is kept, and the client still asks nothing again.
    """
    owner = await connect(db_url)
    try:
        await setup(owner, db_url, "test:model")
        if role_accepts:
            await enable_admin_login(owner, PASSWORD)
    finally:
        await owner.close()
    store = CredentialStore(tmp_path / "etc" / "calu" / "credentials.json")
    store.save("admin", db_url, PASSWORD)
    code = CLIENT.format(
        store=str(store.path), password=PASSWORD, uid=OTHER_UID, state=str(state_root)
    )
    environment = dict(os.environ) | {"CALU_PLACE_FILE": "/nonexistent"}
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen == {
        "problem": None,
        "asked": 1,
        "role": "calu_admin" if role_accepts else "calu_owner",
        "needs_password": False,
        "store_after": "refused",
        "sessions": 0,
        "version": seen["version"],
        "mode": "track",
        "in_environment": False,
        "in_child": False,
    }
