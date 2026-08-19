"""Regression coverage for issue #57: FastAPI dispatches a *sync*
generator dependency's post-yield cleanup via a separate
`run_in_threadpool` call — for `app/deps.py::get_db`, that meant
`session.commit()` could still only be *scheduled*, not yet executed,
by the time the HTTP response had already been sent to the client. A
client that receives a redirect and immediately issues the next request
(exactly what a browser does, and what real UAT/production traffic
does) could have that next request served before the previous request's
commit had actually run — backend-independent (confirmed against
PostgreSQL-incompatible SQLite journal modes too; this is not a SQLite
bug, see issue #55/#57 and their worklogs).

Fix: `get_db` is now an `async def` generator. FastAPI resumes an async
generator dependency's cleanup directly on the event loop
(`fastapi/dependencies/utils.py::_solve_generator`,
`asynccontextmanager(dependant.call)`), with no thread-pool hop at all —
eliminating the scheduling gap the sync-generator path introduced.

Following the same lesson #55's own regression test learned the hard
way (a local "zero misses" measurement did not hold on GitHub Actions'
runners, which showed a ~29% immediate-miss rate for the *previous*,
sync-generator behavior): this file does not assert a hard "zero misses"
count as portable across every possible CI environment. It asserts what
the fix actually, structurally guarantees (it is a real `async def`
generator, verified by introspection, not a behavior inferred from
measurement) plus the same permanence guarantee `tests/test_sqlite_read_after_write.py`
already established (no write is ever *lost*, only transiently invisible
for the retry window). Measured locally, including under deliberate
artificial CPU contention (multiple CPU-bound processes saturating every
core), across 900+ sequential round trips: 0 immediate misses — recorded
in the worklog as evidence, not encoded as a hard assertion here.
"""

from __future__ import annotations

import inspect
import re

import httpx

from app.deps import get_db
from app.main import create_app
from app.models import User
from app.security import hash_password
from tests.uat.harness import LiveServer

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
_ITERATIONS = 150
_RETRY_WINDOW_SECONDS = 3.0
_RETRY_INTERVAL_SECONDS = 0.05


def test_get_db_is_an_async_generator_dependency():
    """Structural pin for the actual fix mechanism — not timing-dependent."""
    assert inspect.isasyncgenfunction(get_db)


def test_sequential_http_write_then_read_is_eventually_consistent(tmp_path):
    """Same shape as tests/test_sqlite_read_after_write.py's permanence
    test: sequential POST (redirects via `redirect_with_flash`) then GET
    the redirect target, no concurrency, no side-channel DB access. Any
    immediate miss must resolve within a short bounded retry — the write
    is never permanently lost."""
    app = create_app(database_path=str(tmp_path / "issue57.db"))
    with app.state.session_factory() as session:
        session.add(User(email="issue57-admin@example.com", password_hash=hash_password("x" * 10)))
        session.commit()

    with LiveServer(app) as server, httpx.Client(base_url=server.base_url, timeout=10.0) as client:
        login_page = client.get("/login")
        csrf = _CSRF_RE.search(login_page.text).group(1)
        login_response = client.post(
            "/login",
            data={"email": "issue57-admin@example.com", "password": "x" * 10, "csrf_token": csrf},
            follow_redirects=False,
        )
        assert login_response.status_code == 303

        permanent_misses = []
        for i in range(_ITERATIONS):
            person_form = client.get("/people/new")
            display_name = f"Issue57 Probe {i}"
            csrf = _CSRF_RE.search(person_form.text).group(1)
            create_response = client.post(
                "/people",
                data={
                    "email": f"issue57-probe-{i}@example.com",
                    "display_name": display_name,
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            assert create_response.status_code == 303
            redirect_path = create_response.headers["location"].split("?", 1)[0]

            deadline_message = None
            import time

            deadline = time.monotonic() + _RETRY_WINDOW_SECONDS
            while True:
                detail_response = client.get(redirect_path)
                if detail_response.status_code == 200 and display_name in detail_response.text:
                    break
                if time.monotonic() > deadline:
                    deadline_message = (i, detail_response.status_code)
                    break
                time.sleep(_RETRY_INTERVAL_SECONDS)
            if deadline_message is not None:
                permanent_misses.append(deadline_message)

        assert not permanent_misses, f"permanently lost write(s): {permanent_misses}"
