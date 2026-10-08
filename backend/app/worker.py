"""Standalone durable worker: python -m app.worker.

Review calls run in a separate loop so a slow model never stops deadline scans.
Jobs and Telegram delivery claims survive worker restarts.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal, utcnow
from .models import Assessment, OrderEvent, ReviewJob, WorkOrder
from .services.notifications import create_notification, deliver_pending, executor_ids, poll_telegram_updates, scan_deadlines
from .services.review import ProviderUnavailable, StaleReview, check_snapshot, run_review, snapshot_from_order

logger = logging.getLogger("naryadai.worker")
MAX_REVIEW_ATTEMPTS = 3


def _manual_failure(session: Session, job: ReviewJob, error: str) -> None:
    order = session.scalar(select(WorkOrder).where(WorkOrder.id == job.order_id).with_for_update())
    if order is None or order.result_version != job.input_version or order.status not in {"performed", "ai_review"}:
        job.state = "obsolete"
        return
    existing = session.scalar(select(Assessment).where(Assessment.order_id == order.id, Assessment.input_version == job.input_version))
    if existing is None:
        message = "ИИ-проверка недоступна после повторных попыток. Результат сохранён; требуется ручная проверка мастера."
        rules = check_snapshot(snapshot_from_order(session, order, job.input_version))
        previous_status = order.status
        verdict = "requires_rework" if rules["verdict"] == "requires_rework" else "accepted_with_remarks"
        if verdict == "requires_rework":
            order.status = "rework"
            order.rework_count += 1
            order.active_executor_id = None
            order.queue_position = None
        else:
            order.status = "ai_review"
        session.add(Assessment(order_id=order.id, input_version=job.input_version, verdict=verdict, score=None, explanation=message, engine="rules", model=None, needs_master_review=True, good=rules["good"], improve=rules["improve"] + ["Мастеру проверить смысл работ, материалы и фотографии вручную."], findings=rules["findings"] + [{"code": "model_unavailable", "severity": "warning", "message": message, "evidence": ["field:work_description"]}], details={"model_status": "failed", "model_error": error, "time": rules["time"], "completeness_score100": rules["score"], "input_version": job.input_version, "final_decision": "master"}))
        session.add(OrderEvent(order_id=order.id, action="review_failed", actor_id=None, from_status=previous_status, to_status=order.status, reason=message, details={"input_version": job.input_version, "model_status": "failed"}))
        for user_id in set(executor_ids(session, order)) | {order.master_id}:
            create_notification(session, user_id, "review_failed", f"Ручная проверка наряда №{order.number}", message, order_id=order.id, dedupe_key=f"review_failed:{order.id}:{job.input_version}")
    job.state = "failed"


def process_review_jobs(session_factory: Callable[[], Session] = SessionLocal, limit: int = 1, *, now: datetime | None = None) -> int:
    now = now or utcnow()
    # A lease exceeds the configured request timeout and allows crash recovery.
    lease = max(120, get_settings().ai_timeout_seconds + 60)
    with session_factory() as session:
        session.execute(update(ReviewJob).where(ReviewJob.state == "running", ReviewJob.locked_at < now - timedelta(seconds=lease)).values(state="pending", locked_at=None, next_attempt_at=now))
        session.commit()
        candidates = list(session.scalars(select(ReviewJob.id).where(ReviewJob.state.in_(["pending", "retry"]), ReviewJob.next_attempt_at <= now).order_by(ReviewJob.next_attempt_at, ReviewJob.id).limit(limit)))
    completed = 0
    for job_id in candidates:
        with session_factory() as session:
            claimed = session.execute(update(ReviewJob).where(ReviewJob.id == job_id, ReviewJob.state.in_(["pending", "retry"]), ReviewJob.next_attempt_at <= now).values(state="running", locked_at=now, attempts=ReviewJob.attempts + 1))
            session.commit()
            if not claimed.rowcount:
                continue
            job = session.get(ReviewJob, job_id)
            try:
                run_review(session, job.order_id, job.input_version)
                session.expire_all()
                job = session.get(ReviewJob, job_id)
                job.state, job.last_error, job.locked_at = "completed", None, None
                completed += 1
            except StaleReview as exc:
                session.rollback()
                job = session.get(ReviewJob, job_id)
                job.state, job.last_error, job.locked_at = "obsolete", str(exc), None
            except ProviderUnavailable as exc:
                session.rollback()
                job = session.get(ReviewJob, job_id)
                job.last_error, job.locked_at = str(exc), None
                if job.attempts >= MAX_REVIEW_ATTEMPTS:
                    _manual_failure(session, job, str(exc))
                else:
                    job.state = "retry"
                    job.next_attempt_at = utcnow() + timedelta(seconds=5 * 2 ** job.attempts)
            except Exception:
                # Unexpected errors are stored as a fixed code, never as a
                # provider response or exception URL containing secrets.
                session.rollback()
                job = session.get(ReviewJob, job_id)
                job.last_error, job.locked_at = "review_internal_error", None
                if job.attempts >= MAX_REVIEW_ATTEMPTS:
                    _manual_failure(session, job, "review_internal_error")
                else:
                    job.state = "retry"
                    job.next_attempt_at = utcnow() + timedelta(seconds=10 * job.attempts)
            session.commit()
    return completed


def deadline_tick(session_factory: Callable[[], Session] = SessionLocal) -> dict[str, int]:
    with session_factory() as session:
        queued = scan_deadlines(session)
        return {"queued": queued}


def delivery_tick(session_factory: Callable[[], Session] = SessionLocal) -> dict[str, int]:
    with session_factory() as session:
        linked = poll_telegram_updates(session)
        sent = deliver_pending(session)
        return {"linked": linked, "sent": sent}


def _delivery_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            delivery_tick()
        except Exception:
            logger.error("Delivery worker tick failed")
        stop.wait(max(0.2, get_settings().worker_interval_seconds))


def _review_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            process_review_jobs()
        except Exception:
            logger.error("Review worker tick failed")
        stop.wait(max(0.2, get_settings().worker_interval_seconds))


def run_worker(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    thread = threading.Thread(target=_review_loop, args=(stop,), name="naryadai-review", daemon=True)
    delivery_thread = threading.Thread(target=_delivery_loop, args=(stop,), name="naryadai-delivery", daemon=True)
    thread.start()
    delivery_thread.start()
    try:
        while not stop.is_set():
            try:
                deadline_tick()
            except Exception:
                logger.error("Deadline worker tick failed")
            stop.wait(max(0.2, get_settings().worker_interval_seconds))
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        thread.join(timeout=get_settings().ai_timeout_seconds + 2)
        delivery_thread.join(timeout=12)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_worker()
