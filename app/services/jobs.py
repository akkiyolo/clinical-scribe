"""Background job runner and stuck-job sweeper.

Jobs run in-process on a small thread pool (the spec's accepted trade-off for a portfolio app;
a real queue is listed under "Production path" in the README). Because a restart kills in-flight
jobs, anything left in a "working" state past STALE_AFTER is marked failed so the doctor can
click Retry instead of waiting forever.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.consult import AgentRun, Consult
from app.models.enums import AgentRunStatus, ConsultStatus, TranscriptionStatus
from app.services.timeutil import as_utc

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clinical-jobs")
_inline = False


def set_inline_mode(enabled: bool) -> None:
    """Run jobs synchronously in the caller's thread (used by tests for determinism)."""
    global _inline
    _inline = enabled


def _run_logged(func: Callable, args: tuple, task_id: str | None) -> None:
    try:
        func(*args)
    except Exception:
        logger.exception("Background task %s failed", task_id or func.__name__)


def run_in_background(func: Callable, *args, task_id: str | None = None) -> Future | None:
    """Run func(*args) on the job pool; exceptions are logged, never raised to the caller."""
    if _inline:
        _run_logged(func, args, task_id)
        return None
    return _executor.submit(_run_logged, func, args, task_id)


def _is_stale(moment: datetime | None) -> bool:
    moment = as_utc(moment)
    return moment is not None and datetime.now(timezone.utc) - moment > STALE_AFTER


def expire_stale_consult(db: Session, consult: Consult) -> bool:
    """Fail a consult stuck mid-job past STALE_AFTER. Returns True when it changed anything.

    Called on every consult read, so a job killed by a restart surfaces as "failed, click Retry"
    as soon as it is old enough rather than only at the next process start.
    """
    changed = False
    if consult.transcription_status == TranscriptionStatus.transcribing and _is_stale(
        consult.updated_at
    ):
        consult.transcription_status = TranscriptionStatus.failed
        consult.error_message = "Transcription timed out. Click Retry to try again."
        changed = True
    if consult.status in (
        ConsultStatus.soap_generating,
        ConsultStatus.prescription_generating,
    ) and _is_stale(consult.updated_at):
        consult.status = ConsultStatus.failed
        consult.error_message = "Processing timed out. Click Retry to try again."
        changed = True
    if changed:
        stuck_runs = (
            db.query(AgentRun)
            .filter(
                AgentRun.consult_id == consult.id,
                AgentRun.status.in_([AgentRunStatus.queued, AgentRunStatus.running]),
            )
            .all()
        )
        for run in stuck_runs:
            if _is_stale(run.started_at or run.created_at):
                run.status = AgentRunStatus.failed
                run.error = "Job timed out (process may have restarted)"
                run.finished_at = datetime.now(timezone.utc)
        db.commit()
    return changed


def sweep_stuck_jobs() -> int:
    """Mark stale queued/running agent runs, transcriptions and SOAP jobs as failed.

    Run on startup to recover from process restarts. Returns the number of jobs swept.
    """
    db = SessionLocal()
    try:
        count = 0
        for run in db.query(AgentRun).filter(
            AgentRun.status.in_([AgentRunStatus.queued, AgentRunStatus.running])
        ):
            if not _is_stale(run.started_at or run.created_at):
                continue
            run.status = AgentRunStatus.failed
            run.error = "Job timed out (process may have restarted)"
            run.finished_at = datetime.now(timezone.utc)
            consult = db.get(Consult, run.consult_id)
            if consult and consult.status == ConsultStatus.prescription_generating:
                consult.status = ConsultStatus.failed
                consult.error_message = "Processing timed out. Click Retry to try again."
            count += 1

        for consult in db.query(Consult).filter(
            (Consult.transcription_status == TranscriptionStatus.transcribing)
            | (Consult.status == ConsultStatus.soap_generating)
        ):
            if expire_stale_consult(db, consult):
                count += 1

        db.commit()
        if count:
            logger.info("Swept %d stuck jobs", count)
        return count
    except Exception:
        db.rollback()
        logger.exception("Error sweeping stuck jobs")
        return 0
    finally:
        db.close()
