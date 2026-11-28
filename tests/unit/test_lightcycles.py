"""`calu lightcycles`: the hidden command and the local server of MetaTron for the browser.

The game itself is JavaScript and is tested in a browser (`tests/browser/test_lightcycles.py`).
The tests check these things:

- The command is hidden and still works.
- It needs nothing of calu.
- The server binds the loopback interface only.
- Every file of the page is served with the right type.
- A Content-Security-Policy allows no inline script and no other server.
"""

import argparse
import re
import socket
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from calu import cli
from calu.cli import build_parser
from calu.lightcycles import server

FILES = {
    "index.html": "text/html",
    "style.css": "text/css",
    "main.js": "text/javascript",
    "flow.js": "text/javascript",
    "sim.js": "text/javascript",
    "water.js": "text/javascript",
    "render.js": "text/javascript",
}
"""Every file of the page with the media type a browser needs for it."""


@pytest.fixture
def client() -> TestClient:
    """A client of the server application, without a socket."""
    return TestClient(server.create_app())


def test_the_command_is_not_in_the_help_and_still_parses(capsys) -> None:
    parser = build_parser()
    listed = parser.format_help()
    assert "lightcycles" not in listed and "SUPPRESS" not in listed
    # The commands a user knows are all still listed.
    for name in ("setup", "run", "attach", "web", "credential"):
        assert re.search(rf"^\s+{name}\s", listed, re.MULTILINE), name
    args = parser.parse_args(["lightcycles", "--no-browser", "--players", "1", "--bots", "3"])
    assert args.handler is cli.cmd_lightcycles
    assert (args.port, args.no_browser, args.players, args.bots, args.speed, args.seed) == (
        None,
        True,
        1,
        3,
        None,
        None,
    )
    # It is neither `calu run` nor `calu serve`: no log file is opened for it.
    assert getattr(args, "log_name", None) is None


def test_its_own_help_names_the_game_and_its_author(capsys) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["lightcycles", "--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert "MetaTron for the browser \N{EM DASH} after MetaTron by Kai Londenberg" in shown
    for option in ("--port", "--no-browser", "--players", "--bots", "--speed", "--seed"):
        assert option in shown


def test_the_handler_passes_the_options_on_and_needs_no_credential(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(server, "serve", lambda **given: calls.append(given))

    args = ["lightcycles", "--port", "8123", "--players", "2", "--bots", "5", "--speed", "3"]
    assert cli.main([*args, "--seed", "42"], store_factory=lambda: None) == 0  # type: ignore[arg-type,return-value]
    assert cli.main(["lightcycles", "--no-browser"], store_factory=lambda: None) == 0  # type: ignore[arg-type,return-value]
    assert calls == [
        {
            "port": 8123,
            "browser": True,
            "options": {"players": 2, "bots": 5, "speed": 3, "seed": 42},
        },
        {"port": None, "browser": False, "options": {}},
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--players", "11"], "--players takes a number from 0 to 10"),
        (["--bots", "-1"], "--bots takes a number from 0 to 10"),
        (["--speed", "0"], "--speed takes a number from 1 to 9"),
        (["--players", "6", "--bots", "5"], "together at most 10"),
        (["--bots", "10"], "together at most 10"),
        (["--port", "70000"], "--port takes a number from 1 to 65535"),
    ],
)
def test_an_option_out_of_range_is_one_line_and_starts_no_server(
    arguments: list[str], message: str, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(server, "serve", lambda **given: pytest.fail("the server started"))
    assert cli.main(["lightcycles", *arguments], store_factory=lambda: None) == 1  # type: ignore[arg-type,return-value]
    assert message in capsys.readouterr().err


def test_the_page_comes_with_its_security_headers(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "MetaTron" in response.text
    policy = response.headers["content-security-policy"]
    assert policy == server.CONTENT_SECURITY_POLICY
    assert "default-src 'none'" in policy and "script-src 'self'" in policy
    assert "connect-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert "unsafe" not in policy and "http" not in policy and "data:" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("name", sorted(FILES))
def test_each_file_of_the_page_ships_and_is_served_with_its_type(
    name: str, client: TestClient
) -> None:
    path = Path(str(resources.files("calu.lightcycles").joinpath("static").joinpath(name)))
    assert path.is_file() and path.stat().st_size > 0, f"{name} is missing from the package"
    response = client.get("/" if name == "index.html" else f"/static/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(FILES[name])
    assert response.headers["content-security-policy"] == server.CONTENT_SECURITY_POLICY
    assert response.content == path.read_bytes()


def test_the_package_holds_no_file_the_page_does_not_use() -> None:
    """No bitmap, sound or other asset of the original is in the package."""
    present = {entry.name for entry in server.STATIC.iterdir()}
    assert present == set(FILES)


def test_the_page_has_no_inline_script_or_style_and_loads_only_its_own_files() -> None:
    page = (server.STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", page), "an inline script"
    assert "<style" not in page and " style=" not in page and not re.search(r"\son\w+=", page)
    assert set(re.findall(r'(?:src|href)="([^"]+)"', page)) == {
        "/static/style.css",
        "/static/main.js",
    }
    for script in server.STATIC.glob("*.js"):
        text = script.read_text(encoding="utf-8")
        assert not re.search(r"https?://|fetch\(|XMLHttpRequest|WebSocket|eval\(", text), script
        for target in re.findall(r'from "([^"]+)"', text):
            assert target.startswith("./") and (server.STATIC / target[2:]).is_file(), target


def test_nothing_outside_the_static_directory_is_served(client: TestClient) -> None:
    for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/server.py", "/static/"):
        assert client.get(path).status_code == 404, path
    assert client.post("/").status_code == 405


def test_the_server_binds_the_loopback_interface_only() -> None:
    listener = server.bind(None)
    try:
        host, port = listener.getsockname()
        assert host == "127.0.0.1" and port > 0
        # The same port asked for by number is taken now: an error, not another port.
        with pytest.raises(OSError):
            server.bind(port)
        # Without a number the next start finds another, free port.
        second = server.bind(None) if port == server.USUAL_PORT else None
        if second is not None:
            assert second.getsockname()[1] not in (0, port)
            second.close()
    finally:
        listener.close()
    with pytest.raises(ValueError, match="1 to 65535"):
        server.bind(0)


def test_the_address_carries_the_options() -> None:
    assert server.address(8817, {}) == "http://127.0.0.1:8817/"
    assert server.address(9000, {"players": 1, "bots": 3}) == (
        "http://127.0.0.1:9000/?players=1&bots=3"
    )


def test_a_machine_without_a_browser_still_gets_the_address(monkeypatch) -> None:
    def broken(url: str) -> bool:
        raise RuntimeError("no display")

    monkeypatch.setattr(server.webbrowser, "open", broken)
    assert server.open_in_browser("http://127.0.0.1:1/") is False
    monkeypatch.setattr(server.webbrowser, "open", lambda url: False)
    assert server.open_in_browser("http://127.0.0.1:1/") is False


def test_serve_prints_the_address_opens_the_browser_and_serves(monkeypatch) -> None:
    import uvicorn

    said: list[str] = []
    opened: list[str] = []
    served: list[tuple[str, int]] = []

    def run(self: uvicorn.Server, sockets: list[socket.socket]) -> None:
        served.append(sockets[0].getsockname())
        raise KeyboardInterrupt  # what Ctrl-C ends the real server with

    monkeypatch.setattr(uvicorn.Server, "run", run)
    server.serve(options={"seed": 5}, say=said.append, open_url=lambda u: opened.append(u) or False)
    host, port = served[0]
    assert host == "127.0.0.1"
    assert opened == [f"http://127.0.0.1:{port}/?seed=5"]
    assert said[0].endswith(f"after MetaTron by Kai Londenberg: http://127.0.0.1:{port}/?seed=5")
    assert "open the address yourself" in said[1]
    said.clear()
    server.serve(browser=False, say=said.append, open_url=lambda u: pytest.fail("opened"))
    assert len(said) == 2 and "Ctrl-C" in said[1]


def test_no_other_command_imports_the_game_or_its_server() -> None:
    """Building the command parser imports no module of the easter egg.

    Starts a child Python process that builds the parser and prints the loaded
    `calu.lightcycles` modules. It runs no command.
    """
    code = (
        "import sys; from calu.cli import build_parser; build_parser(); "
        "print(sorted(m for m in sys.modules if m.startswith('calu.lightcycles')))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]"


def test_the_subcommand_is_registered_exactly_once() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert "lightcycles" in subparsers.choices
    assert "lightcycles" not in (subparsers.metavar or "")
