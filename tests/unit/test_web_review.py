"""Page handlers of the web interface without a database: old schemas, session requests, labels.

The database functions the handlers call (`calu.herd`, `calu.session_requests`) are replaced by
fakes, so these tests check what the handlers make of their answers. The queries themselves are
tested against a database in `tests/db/test_web.py`.
"""

from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest

# The web interface is an optional extra, so a checkout synced without it skips these tests.
pytest.importorskip("starlette", reason="the web interface's packages are missing: uv sync")
pytest.importorskip("jinja2", reason="the web interface's packages are missing: uv sync")

from starlette.responses import Response

import calu.web.app as web_app
from calu import herd, session_requests

pytestmark = pytest.mark.anyio

SESSION = "11111111-2222-3333-4444-555555555555"
NEW_SESSION = "99999999-2222-3333-4444-555555555555"


def client() -> httpx.AsyncClient:
    """A client of an application on a database that must not be touched (no login gate)."""
    app = web_app.create_app(db=object())  # type: ignore[arg-type]  # every query is faked
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


@pytest.mark.parametrize("error", [psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn])
async def test_an_old_database_schema_says_how_to_bring_it_up_to_date(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """Review finding: a database set up by an older calu was answered with a bare 500."""

    async def old_schema(*_args: Any, **_kwargs: Any) -> Any:
        """Fail as a query on a table or column the database lacks."""
        raise error('relation "sessions" has no column "last_seen"')

    monkeypatch.setattr(herd, "sessions", old_schema)
    monkeypatch.setattr(herd, "escalations", old_schema)
    async with client() as web:
        page = await web.get("/")
        assert page.status_code == 500
        assert "schema is older than this calu" in page.text
        assert "calu setup" in page.text and "calu db migrate" in page.text
        api = await web.get("/api/sessions")
        assert api.status_code == 500
        assert api.json()["error"] == "database schema too old"


async def test_a_requested_session_keeps_the_project_of_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding: a session started from the web lost the project of the session it came
    from, so it was filed under whatever the directory's configuration said."""
    filed: list[session_requests.SessionRequest] = []

    async def session(_db: Any, _session_id: str) -> Any:
        """The session of the page."""
        return SimpleNamespace(
            hostname="box", os_user="ann", directory="/w", claude_session_id="c-1"
        )

    async def project_of(_db: Any, _session_id: str) -> tuple[str, str]:
        """The session's project."""
        return ("calu", "git@example.org:calu.git")

    async def file(_db: Any, wanted: session_requests.SessionRequest) -> int:
        """Keep the request instead of writing it."""
        filed.append(wanted)
        return 7

    async def wait_for(_db: Any, _request_id: int) -> Any:
        """The server started the session."""
        return SimpleNamespace(state="started", session_id=NEW_SESSION, message="")

    monkeypatch.setattr(herd, "session", session)
    monkeypatch.setattr(herd, "project_of", project_of)
    monkeypatch.setattr(session_requests, "file", file)
    monkeypatch.setattr(session_requests, "wait_for", wait_for)
    async with client() as web:
        answer = await web.post(f"/sessions/{SESSION}/request", data={"kind": "fork"})
    assert answer.status_code == 303
    assert answer.headers["location"].endswith(f"/sessions/{NEW_SESSION}/live")
    assert len(filed) == 1
    assert filed[0].project == "calu"
    assert filed[0].kind == "fork" and filed[0].claude_session_id == "c-1"
    # The session row records neither, so the configuration decides them.
    assert filed[0].model is None and filed[0].effort is None


async def test_a_session_without_a_project_requests_one_without_a_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session of no project files its request with `project` None, as `calu start` does."""
    filed: list[session_requests.SessionRequest] = []

    async def session(_db: Any, _session_id: str) -> Any:
        """The session of the page."""
        return SimpleNamespace(hostname="box", os_user="ann", directory="/w", claude_session_id="")

    async def no_project(_db: Any, _session_id: str) -> None:
        """The session has no project row."""
        return None

    async def file(_db: Any, wanted: session_requests.SessionRequest) -> int:
        """Keep the request instead of writing it."""
        filed.append(wanted)
        return 7

    async def wait_for(_db: Any, _request_id: int) -> Any:
        """The server started the session."""
        return SimpleNamespace(state="started", session_id=NEW_SESSION, message="")

    monkeypatch.setattr(herd, "session", session)
    monkeypatch.setattr(herd, "project_of", no_project)
    monkeypatch.setattr(session_requests, "file", file)
    monkeypatch.setattr(session_requests, "wait_for", wait_for)
    async with client() as web:
        answer = await web.post(f"/sessions/{SESSION}/request", data={"kind": "new"})
    assert answer.status_code == 303
    assert filed[0].project is None


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("x" * 5000, "x" * web_app.MAX_TOKEN_LABEL),
        ("  zed on my laptop  ", "zed on my laptop"),
        ("   ", "calu web"),
        (None, "calu web"),
    ],
)
async def test_a_token_label_is_cut_to_a_bounded_length(
    monkeypatch: pytest.MonkeyPatch, given: str | None, stored: str
) -> None:
    """Review finding: `mint` stored a label of any length the form carried."""
    labels: list[str] = []

    async def mint_token(_db: Any, _session_id: str, label: str = "") -> tuple[int, str]:
        """Keep the label instead of minting."""
        labels.append(label)
        return 1, "token"

    async def session_view(*_args: Any) -> Response:
        """Stand in for the session page, which reads the database."""
        return Response("page")

    monkeypatch.setattr(herd, "mint_token", mint_token)
    monkeypatch.setattr(web_app, "_session_view", session_view)
    async with client() as web:
        answer = await web.post(
            f"/sessions/{SESSION}/token", data={} if given is None else {"label": given}
        )
    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store"
    assert labels == [stored]
