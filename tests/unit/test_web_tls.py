"""HTTPS for `calu web`, optional (`--tls-cert`, `--tls-key`; `calu serve --web-tls-cert`).

Certificates and keys are made in `tmp_path` with `cryptography`. One test runs uvicorn with TLS
on a free port of 127.0.0.1 in a thread and talks to it over a real TLS connection that checks
the certificate against the name `calu.localhost`; it stops the server at its end. No output.
"""

import datetime
import re
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("starlette", reason="the web interface's packages are missing: uv sync")

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from calu import cli
from calu.cli import CliError
from calu.web.app import WEB_HOST_NAME, LocalOnly, TokenGate


def make_pair(directory: Path, names: tuple[str, ...] = (WEB_HOST_NAME,)) -> tuple[Path, Path]:
    """Make a self-signed certificate for `names` and its key (mode 600).

    Args:
        directory: Where the two files go.
        names: The DNS names of the certificate.

    Returns:
        (certificate, key).
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else "x")])
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
    )
    if names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in names]), critical=False
        )
    cert = builder.sign(key, hashes.SHA256())
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path


def test_tls_is_off_unless_both_files_are_given_and_usable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli._tls_files(None, None) is None
    cert, key = make_pair(tmp_path / "good")
    assert cli._tls_files(str(cert), str(key)) == (str(cert), str(key))
    assert capsys.readouterr().out == "", "a certificate for calu.localhost needs no note"
    with pytest.raises(CliError, match="go together"):
        cli._tls_files(str(cert), None)
    key.chmod(0o644)
    with pytest.raises(CliError, match="can be read by others"):
        cli._tls_files(str(cert), str(key))
    key.chmod(0o600)
    other_cert, _other_key = make_pair(tmp_path / "other")
    with pytest.raises(CliError, match="cannot be used together"):
        cli._tls_files(str(other_cert), str(key))  # a key of another certificate
    with pytest.raises(CliError, match="No such file"):
        cli._tls_files(str(cert), str(tmp_path / "missing.pem"))
    named, named_key = make_pair(tmp_path / "named", ("example.org",))
    assert cli._tls_files(str(named), str(named_key)) is not None
    assert "does not name calu.localhost" in capsys.readouterr().out


def test_with_tls_a_changing_request_must_come_from_the_https_origin() -> None:
    guard = LocalOnly(app=None, host_name=WEB_HOST_NAME, scheme="https")

    def scope(origin: str) -> dict[str, Any]:
        """A POST with this Origin to the interface's own host and port.

        Args:
            origin: The Origin header.

        Returns:
            The ASGI scope.
        """
        return {
            "type": "http",
            "method": "POST",
            "path": "/escalations/1/resolve",
            "headers": [(b"host", b"calu.localhost:8800"), (b"origin", origin.encode())],
        }

    assert guard.refusal(scope("https://calu.localhost:8800")) is None
    assert guard.refusal(scope("http://calu.localhost:8800")), "same host and port, other scheme"
    plain = LocalOnly(app=None, host_name=WEB_HOST_NAME, scheme="http")
    assert plain.refusal(scope("https://calu.localhost:8800"))
    assert plain.refusal(scope("http://calu.localhost:8800")) is None


def test_with_tls_the_login_cookie_is_secure() -> None:
    def cookie(secure: bool) -> str:
        """Log in once and return the Set-Cookie header.

        Args:
            secure: Whether the gate serves HTTPS.

        Returns:
            The header.
        """
        sent: list[dict[str, Any]] = []

        async def run() -> None:
            """Send one login request through the gate."""
            gate = TokenGate(app=None, token="tok", secure=secure)
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/",
                "query_string": b"token=tok",
                "headers": [(b"host", b"calu.localhost:8800")],
            }

            async def receive() -> dict[str, Any]:
                """No body.

                Returns:
                    The end of the body.
                """
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message: dict[str, Any]) -> None:
                """Keep what the gate sends.

                Args:
                    message: An ASGI message.
                """
                sent.append(message)

            await gate(scope, receive, send)

        import anyio

        anyio.run(run)
        headers = dict(sent[0]["headers"])
        return headers[b"set-cookie"].decode()

    assert cookie(True).endswith("; Secure")
    assert "Secure" not in cookie(False)


def test_the_printed_addresses_follow_the_scheme() -> None:
    assert cli._web_token_line("calu.localhost", 8800, "t", False, "https").startswith(
        "calu web on https://calu.localhost:8800/?token=t "
    )
    assert cli._pane_line("calu.localhost", 8800, "t", "https").startswith(
        "chat pane: https://calu.localhost:8800/pane#token=t "
    )
    assert "http://" in cli._pane_line("calu.localhost", 8800, None)


def test_uvicorn_serves_https_that_checks_out_for_calu_localhost(tmp_path: Path) -> None:
    import uvicorn

    cert, key = make_pair(tmp_path)

    async def hello(scope: Any, receive: Any, send: Any) -> None:
        """Answer every request with "ok".

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = LocalOnly(hello, host_name=WEB_HOST_NAME, scheme="https")
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        context = ssl.create_default_context(cafile=str(cert))
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=WEB_HOST_NAME) as tls:
                request = (
                    f"GET / HTTP/1.1\r\nHost: {WEB_HOST_NAME}:{port}\r\nConnection: close\r\n\r\n"
                )
                tls.sendall(request.encode())
                answer = b""
                while chunk := tls.recv(4096):
                    answer += chunk
        assert answer.startswith(b"HTTP/1.1 200") and b"\r\nok\r\n" in answer
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            raw.sendall(f"GET / HTTP/1.1\r\nHost: {WEB_HOST_NAME}:{port}\r\n\r\n".encode())
            raw.settimeout(5)
            try:
                plain = raw.recv(4096)
            except ConnectionResetError, TimeoutError:
                plain = b""
        assert not plain.startswith(b"HTTP/1.1 200"), "no answer over plain HTTP"
    finally:
        server.should_exit = True
        thread.join(10)


def _free_port() -> int:
    """Return a port that is free on both loopback addresses at the moment.

    Returns:
        The port.
    """
    for _ in range(50):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        try:
            with socket.socket(socket.AF_INET6) as six:
                six.bind(("::1", port))
        except OSError:
            continue
        return port
    raise AssertionError("no free port on both addresses")


def test_calu_web_takes_its_port_on_both_loopback_addresses_or_refuses() -> None:
    """D279: a program on the other loopback address would be served under the same origin."""
    port = _free_port()
    sockets = cli._web_sockets(port)
    try:
        assert sorted(sock.getsockname()[0] for sock in sockets) == ["127.0.0.1", "::1"]
    finally:
        for sock in sockets:
            sock.close()
    for family, address in ((socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")):
        with socket.socket(family) as squatter:
            squatter.bind((address, port))
            squatter.listen()
            with pytest.raises(CliError, match=f"port {port} on {re.escape(address)}"):
                cli._web_sockets(port)
