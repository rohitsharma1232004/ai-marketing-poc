"""Optional in-process scheduler for the single-instance POC deployment.

For production scale, run ``publishing_worker.py`` as a dedicated worker against
a shared durable database. For the free/single-instance Railway POC, this daemon
thread can process due jobs from the same persistent SQLite database while the
Streamlit process remains alive.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from meta_publisher import DEFAULT_META_GRAPH_API_VERSION
from publishing_worker import run_due_jobs

_MIN_INTERVAL_SECONDS = 30
_MAX_INTERVAL_SECONDS = 3600
_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STOP_EVENT: threading.Event | None = None
_SIGNATURE: tuple[str, int, str] | None = None


@dataclass(frozen=True)
class PublishingRuntimeStatus:
    running: bool
    interval_seconds: int
    api_version: str


def _safe_interval(value: int | str) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Publishing worker interval must be a whole number.") from error
    if not _MIN_INTERVAL_SECONDS <= interval <= _MAX_INTERVAL_SECONDS:
        raise ValueError(
            f"Publishing worker interval must be between {_MIN_INTERVAL_SECONDS} and "
            f"{_MAX_INTERVAL_SECONDS} seconds."
        )
    return interval


def _loop(db_path: str, interval_seconds: int, api_version: str, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            # Safe operational result is intentionally ignored here. Individual
            # job outcomes are persisted in publication_jobs for the UI/history.
            run_due_jobs(
                db_path,
                limit=20,
                api_version=api_version,
            )
        except Exception:
            # A background helper must never crash the Streamlit web process.
            # The next interval can try again; job-level errors remain persisted.
            pass
        stop.wait(interval_seconds)


def start_background_publishing_worker(
    db_path: str,
    *,
    interval_seconds: int | str = 60,
    api_version: str = DEFAULT_META_GRAPH_API_VERSION,
) -> PublishingRuntimeStatus:
    """Start at most one daemon worker for this Python process.

    Meta tokens are resolved from environment variables by ``publishing_worker``.
    This is appropriate for Railway/server deployment where secrets are injected
    as environment variables. It deliberately does not read Streamlit session
    state from the background thread.
    """

    global _THREAD, _STOP_EVENT, _SIGNATURE
    clean_db = str(db_path or "").strip()
    if not clean_db:
        raise ValueError("Publishing worker database path must not be empty.")
    interval = _safe_interval(interval_seconds)
    version = str(api_version or DEFAULT_META_GRAPH_API_VERSION).strip()
    signature = (clean_db, interval, version)

    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            if _SIGNATURE != signature:
                raise RuntimeError(
                    "Publishing background worker is already running with different settings."
                )
            return PublishingRuntimeStatus(True, interval, version)

        stop = threading.Event()
        thread = threading.Thread(
            target=_loop,
            args=(clean_db, interval, version, stop),
            name="meta-publishing-worker",
            daemon=True,
        )
        _STOP_EVENT = stop
        _THREAD = thread
        _SIGNATURE = signature
        thread.start()
    return PublishingRuntimeStatus(True, interval, version)


def configured_auto_worker_enabled(value: str | None = None) -> bool:
    raw = str(value if value is not None else os.getenv("AUTO_PUBLISH_WORKER", "false"))
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("AUTO_PUBLISH_WORKER must be true or false.")
