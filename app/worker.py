"""Worker process entrypoint, run as `python -m app.worker`.

Separate process from the web app (see compose.yaml's `worker` service and
the Kubernetes Deployment planned in Feature 10) — polls the jobs table
and processes one job at a time via app/jobs.py::process_next_job. Job
handlers are registered by importing the modules that define them (see
_register_handlers below) before the poll loop starts.
"""

from __future__ import annotations

import logging
import os
import signal
import time
import uuid
from pathlib import Path

from app.config import get_settings
from app.db import build_engine, init_db, make_session_factory
from app.import_directory import run_directory_once
from app.jobs import process_next_job
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2
_shutdown_requested = False


def _register_handlers() -> None:
    import app.connections  # noqa: F401 - import registers the connection_test handler
    import app.imports  # noqa: F401 - import registers the run_import handler


def _handle_shutdown_signal(signum, frame) -> None:  # noqa: ANN001 - signal handler signature
    global _shutdown_requested
    logger.info("worker received signal %s, finishing current job then exiting", signum)
    _shutdown_requested = True


def run_forever(
    session_factory,
    *,
    worker_id: str,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    watch_dir: Path | None = None,
    watch_importer: str | None = None,
) -> None:
    logger.info("worker %s starting", worker_id)
    while not _shutdown_requested:
        try:
            processed = process_next_job(session_factory, worker_id=worker_id)
        except Exception:
            logger.exception("worker %s: unexpected error processing job", worker_id)
            processed = False

        if not processed and watch_dir is not None and watch_importer:
            try:
                processed = run_directory_once(
                    session_factory, root=watch_dir, importer_name=watch_importer, actor=worker_id
                )
            except Exception:
                logger.exception("worker %s: unexpected error processing watched directory", worker_id)
                processed = False

        if not processed:
            time.sleep(poll_interval)
    logger.info("worker %s shut down cleanly", worker_id)


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    _register_handlers()

    engine = build_engine(settings.resolved_engine_target)
    init_db(engine)
    session_factory = make_session_factory(engine)

    worker_id = f"{os.uname().nodename}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    watch_dir = Path(settings.import_watch_dir) if settings.import_watch_dir else None
    if watch_dir is not None:
        logger.info(
            "watching import directory %s with importer '%s'", watch_dir, settings.import_watch_importer
        )

    run_forever(
        session_factory,
        worker_id=worker_id,
        watch_dir=watch_dir,
        watch_importer=settings.import_watch_importer or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
