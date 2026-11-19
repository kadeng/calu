"""What the live view says when a session's own address is on another machine."""

import dataclasses
import socket

import pytest

pytest.importorskip("starlette")

from calu.web.app import _unreachable_message
from tests.unit.test_endpoint import row


def test_a_session_of_another_host_names_the_port_to_forward() -> None:
    elsewhere = row("/srv/p", host="buildbox", address="ws://127.0.0.1:41235")
    message = _unreachable_message(elsewhere, ConnectionRefusedError(111, "refused"))
    assert "runs on buildbox, not on this machine" in message
    assert "ssh -N -L 41235:127.0.0.1:41235 buildbox" in message
    assert "Escalations page" in message


def test_a_session_of_this_host_that_does_not_answer_keeps_both_explanations() -> None:
    here = row("/srv/p", host=socket.gethostname(), address="ws://127.0.0.1:41235")
    message = _unreachable_message(here, ConnectionRefusedError(111, "refused"))
    assert "the session's process is gone" in message
    gone = dataclasses.replace(here, silent_for_s=10_000)
    assert "nothing live to reach" in _unreachable_message(gone, OSError("x"))
