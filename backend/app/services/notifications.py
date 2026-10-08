"""Persistent per-user inbox, deadline checks and a retryable Telegram outbox."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..db import utcnow
from ..models import Equipment, Notification, OrderEvent, Section, TelegramBotState, TelegramLink, User, WorkOrder
from ..security import can_access_order

ACTIVE_STATUSES = {"issued", "accepted", "queued", "rejected", "in_progress", "paused", "rework"}
STATUS_LABELS = {"issued": "выдан", "accepted": "принят", "queued": "в очереди", "rejected": "отклонён", "in_progress": "в работе", "paused": "приостановлен", "rework": "на доработке"}


def _protect_transport_logs() -> None:
    # HTTPX emits full request URLs at INFO. Telegram embeds the bot token in
    # that URL, so ordinary transport logs must not be enabled for this client.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def executor_ids(session: Any, order: WorkOrder) -> list[str]:
    if order.executor_id:
        return [order.executor_id]
    if order.brigade_id:
        return list(session.scalars(select(User.id).where(User.brigade_id == order.brigade_id, User.active.is_(True), User.role == "worker", User.on_shift.is_(True))))
    return []


def create_notification(session: Any, user_id: str, kind: str, title: str, body: str, order_id: str | None = None, dedupe_key: str | None = None) -> Notification:
    """Enqueue atomically in the caller's transaction; never sends externally."""
    key = dedupe_key or f"event:{uuid4()}"
    existing = session.scalar(select(Notification).where(Notification.user_id == user_id, Notification.dedupe_key == key))
    if existing:
        return existing
    notification = Notification(user_id=user_id, kind=kind, title=title[:200], body=body, order_id=order_id, dedupe_key=key[:200])
    try:
        with session.begin_nested():
            session.add(notification)
            session.flush()
        return notification
    except IntegrityError:
        existing = session.scalar(select(Notification).where(Notification.user_id == user_id, Notification.dedupe_key == key[:200]))
        if existing is None:
            raise
        return existing


def _last_assignment_time(session: Any, order: WorkOrder) -> datetime:
    assignment = session.scalar(select(OrderEvent).where(OrderEvent.order_id == order.id, OrderEvent.action.in_(["created", "issued", "reassigned", "reassign"])).order_by(OrderEvent.occurred_at.desc(), OrderEvent.id.desc()).limit(1))
    return assignment.occurred_at if assignment else order.created_at


def _context(session: Any, order: WorkOrder, now: datetime) -> str:
    equipment = session.get(Equipment, order.equipment_id)
    section = session.get(Section, order.section_id)
    executors = [session.get(User, user_id) for user_id in executor_ids(session, order)]
    names = ", ".join(user.full_name for user in executors if user) or "бригада"
    latest = session.scalar(select(OrderEvent).where(OrderEvent.order_id == order.id, OrderEvent.reason.is_not(None)).order_by(OrderEvent.occurred_at.desc(), OrderEvent.id.desc()).limit(1))
    comment = latest.reason if latest else order.comment
    overdue = max(0, int((now - order.due_at).total_seconds() // 60))
    return f"Наряд №{order.number}. Оборудование: {equipment.name if equipment else order.equipment_id}. Участок: {section.name if section else order.section_id}. Исполнитель: {names}. Статус: {STATUS_LABELS.get(order.status, order.status)}. Просрочка: {overdue} мин. Последний комментарий: {comment or 'нет'}."


def _alternative(session: Any, order: WorkOrder) -> str:
    current = session.get(User, order.executor_id) if order.executor_id else None
    busy = select(WorkOrder.active_executor_id).where(WorkOrder.active_executor_id.is_not(None))
    query = select(User).where(User.role == "worker", User.active.is_(True), User.on_shift.is_(True), User.id.not_in(busy))
    if current:
        query = query.where(User.specialty == current.specialty, User.id != current.id)
    candidate = session.scalar(query.order_by(User.full_name).limit(1))
    return f" Свободный кандидат той же специальности: {candidate.full_name}; назначение подтверждает мастер." if candidate and current else " Подходящий свободный кандидат не найден; проверьте загрузку и специальность."


def scan_deadlines(session: Any, now: datetime | None = None, *, settings: Any | None = None) -> int:
    """Compute deadlines from server time; overdue is never a lifecycle state."""
    now = now or utcnow()
    settings = settings or get_settings()
    created = 0
    orders = list(session.scalars(select(WorkOrder).where(WorkOrder.status.in_(ACTIVE_STATUSES))))
    for order in orders:
        recipients = executor_ids(session, order)
        if not recipients:
            recipients = [order.master_id]
        fingerprint = f"{order.id}:{order.due_at.isoformat()}:{order.result_version}"
        seconds_left = (order.due_at - now).total_seconds()
        if 0 < seconds_left <= max(0, settings.reminder_minutes) * 60:
            for user_id in recipients:
                key = f"deadline_reminder:{fingerprint}"
                before = session.scalar(select(Notification.id).where(Notification.user_id == user_id, Notification.dedupe_key == key))
                create_notification(session, user_id, "deadline_reminder", f"Приближается срок: наряд №{order.number}", f"До срока осталось {max(1, int(seconds_left // 60))} мин. " + _context(session, order, now), order_id=order.id, dedupe_key=key)
                created += before is None
        if seconds_left <= 0:
            bucket = int((-seconds_left) // (max(1, settings.repeat_minutes) * 60))
            for user_id in set(recipients) | {order.master_id}:
                key = f"overdue:{fingerprint}:{bucket}"
                before = session.scalar(select(Notification.id).where(Notification.user_id == user_id, Notification.dedupe_key == key))
                create_notification(session, user_id, "overdue", f"Просрочен наряд №{order.number}", _context(session, order, now), order_id=order.id, dedupe_key=key)
                created += before is None
            prolonged = getattr(settings, "leader_escalation_minutes", 120)
            if prolonged > 0 and -seconds_left >= prolonged * 60:
                leader_bucket = int((-seconds_left - prolonged * 60) // (max(1, settings.repeat_minutes) * 60))
                leaders = session.scalars(select(User.id).where(User.role == "leader", User.active.is_(True)))
                for leader_id in leaders:
                    key = f"prolonged_overdue:{fingerprint}:{leader_bucket}"
                    before = session.scalar(select(Notification.id).where(Notification.user_id == leader_id, Notification.dedupe_key == key))
                    create_notification(session, leader_id, "prolonged_overdue", f"Длительная просрочка наряда №{order.number}", _context(session, order, now), order_id=order.id, dedupe_key=key)
                    created += before is None
        if order.status == "issued" and order.accepted_at is None:
            assigned = _last_assignment_time(session, order)
            threshold = settings.emergency_accept_minutes if order.priority == "emergency" else settings.accept_minutes
            seconds = (now - assigned).total_seconds()
            if seconds >= max(0, threshold) * 60:
                bucket = int((seconds - max(0, threshold) * 60) // (max(1, settings.repeat_minutes) * 60))
                key = f"not_accepted:{order.id}:{assigned.isoformat()}:{bucket}"
                before = session.scalar(select(Notification.id).where(Notification.user_id == order.master_id, Notification.dedupe_key == key))
                create_notification(session, order.master_id, "not_accepted", f"Не принят наряд №{order.number}", _context(session, order, now) + _alternative(session, order), order_id=order.id, dedupe_key=key)
                created += before is None
    session.commit()
    return int(created)


def _telegram_text(session: Any, notification: Notification, settings: Any) -> str:
    order = session.get(WorkOrder, notification.order_id) if notification.order_id else None
    # Real workforce identities and comments stay in the authorized local inbox.
    # The external channel gets only a minimal alert and an authenticated link.
    text = f"{notification.title}\n{notification.body}" if order is None or order.synthetic else f"{notification.title}\nОткройте приложение для подробностей."
    if order:
        text += "\n" + settings.public_app_url.rstrip("/") + f"/orders/{order.id}"
    return text[:4000]


async def _send_telegram(token: str, chat_id: str, text: str, *, client: httpx.AsyncClient | None = None) -> tuple[bool, str | None, int | None]:
    _protect_transport_logs()
    owned = client is None
    client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
    try:
        # Do not log this URL, exception URL, response body, or token.
        response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=10)
        try:
            data = response.json()
        except ValueError:
            return False, "telegram_invalid_response", None
        if not isinstance(data, dict):
            return False, "telegram_invalid_response", None
        if response.status_code == 200 and data.get("ok") is True:
            return True, None, None
        parameters = data.get("parameters")
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        return False, f"telegram_http_{response.status_code}", max(1, min(3600, retry_after)) if isinstance(retry_after, int) and not isinstance(retry_after, bool) else None
    except httpx.HTTPError:
        return False, "telegram_transport_failure", None
    finally:
        if owned:
            await client.aclose()


def _delivery_invalid_reason(session: Any, notification: Notification, user: User | None, order: WorkOrder | None, now: datetime, settings: Any) -> str | None:
    if user is None or not user.active:
        return "recipient_inactive"
    if notification.order_id and (order is None or not can_access_order(user, order)):
        return "recipient_order_access_revoked"
    deadlines = {"deadline_reminder", "overdue", "not_accepted", "prolonged_overdue", "assigned"}
    if notification.kind not in deadlines:
        return None
    if order is None or order.status not in ACTIVE_STATUSES:
        return "notification_order_state_obsolete"
    recipients = set(executor_ids(session, order)) or {order.master_id}
    assigned_at = _last_assignment_time(session, order)
    if assigned_at > notification.created_at:
        return "notification_assignment_obsolete"
    seconds_left = (order.due_at - now).total_seconds()
    fingerprint = f"{order.id}:{order.due_at.isoformat()}:{order.result_version}"
    if notification.kind == "deadline_reminder":
        if notification.dedupe_key != f"deadline_reminder:{fingerprint}" or user.id not in recipients or not 0 < seconds_left <= max(0, settings.reminder_minutes) * 60:
            return "notification_deadline_obsolete"
    elif notification.kind == "overdue":
        if not notification.dedupe_key.startswith(f"overdue:{fingerprint}:") or user.id not in recipients | {order.master_id} or seconds_left > 0:
            return "notification_deadline_obsolete"
    elif notification.kind == "prolonged_overdue":
        threshold = getattr(settings, "leader_escalation_minutes", 120)
        if not notification.dedupe_key.startswith(f"prolonged_overdue:{fingerprint}:") or user.role != "leader" or threshold <= 0 or -seconds_left < threshold * 60:
            return "notification_escalation_obsolete"
    elif notification.kind == "not_accepted":
        threshold = settings.emergency_accept_minutes if order.priority == "emergency" else settings.accept_minutes
        if order.status != "issued" or order.accepted_at is not None or user.id != order.master_id or (now - assigned_at).total_seconds() < max(0, threshold) * 60 or not notification.dedupe_key.startswith(f"not_accepted:{order.id}:{assigned_at.isoformat()}:"):
            return "notification_acceptance_obsolete"
    elif notification.kind == "assigned" and (order.status != "issued" or user.id not in recipients):
        return "notification_assignment_obsolete"
    return None


def deliver_pending(session: Any, limit: int = 20, now: datetime | None = None, *, settings: Any | None = None, client: httpx.AsyncClient | None = None) -> int:
    """Revalidate immediately before dispatch, without a DB lock over HTTP.

    A state/access change after validation can race an already-started external
    request. Telegram remains at-least-once after uncertain network timeouts.
    CAS completion prevents a lost lease from overwriting a newer worker state.
    """
    fixed_now = now
    now = now or utcnow()
    settings = settings or get_settings()
    if not settings.telegram_bot_token:
        session.execute(update(Notification).where(Notification.telegram_status == "pending").values(telegram_status="unconfigured", last_error="telegram_not_configured"))
        session.commit()
        return 0
    # Recover expired delivery claims after a worker restart.
    session.execute(update(Notification).where(Notification.telegram_status == "sending", Notification.next_attempt_at <= now).values(telegram_status="pending"))
    session.commit()
    candidates = list(session.scalars(select(Notification.id).where(Notification.telegram_status.in_(["pending", "retry", "unconfigured"]), Notification.next_attempt_at <= now).order_by(Notification.created_at, Notification.id).limit(limit)))
    delivered = 0
    for notification_id in candidates:
        checked_at = fixed_now or utcnow()
        lease_until = checked_at + timedelta(seconds=60)
        claimed = session.execute(update(Notification).where(Notification.id == notification_id, Notification.telegram_status.in_(["pending", "retry", "unconfigured"]), Notification.next_attempt_at <= checked_at).values(telegram_status="sending", next_attempt_at=lease_until))
        session.commit()
        if not claimed.rowcount:
            continue
        session.expire_all()
        notification = session.get(Notification, notification_id)
        user = session.get(User, notification.user_id)
        order = session.get(WorkOrder, notification.order_id) if notification.order_id else None
        invalid = _delivery_invalid_reason(session, notification, user, order, checked_at, settings)
        if invalid:
            notification.telegram_status = "cancelled"
            notification.last_error = invalid
            session.commit()
            continue
        if not user.telegram_chat_id:
            notification.telegram_status = "unconfigured"
            notification.last_error = "telegram_chat_not_linked"
            notification.next_attempt_at = checked_at + timedelta(minutes=5)
            session.commit()
            continue
        # A valid delayed alert describes the current state, not a stale copy.
        if order is not None and notification.kind in {"deadline_reminder", "overdue", "not_accepted", "prolonged_overdue"}:
            notification.body = _context(session, order, checked_at)
            if notification.kind == "deadline_reminder":
                notification.body = f"До срока осталось {max(1, int((order.due_at - checked_at).total_seconds() // 60))} мин. " + notification.body
            if notification.kind == "not_accepted":
                notification.body += _alternative(session, order)
        text = _telegram_text(session, notification, settings)
        # Commit before the external call to avoid holding a DB transaction.
        chat_id = user.telegram_chat_id
        session.commit()
        ok, error, retry_after = asyncio.run(_send_telegram(settings.telegram_bot_token, chat_id, text, client=client))
        session.expire_all()
        notification = session.get(Notification, notification_id)
        attempts = notification.attempts + 1
        session.execute(update(Notification).where(Notification.id == notification_id, Notification.telegram_status == "sending", Notification.next_attempt_at == lease_until).values(attempts=attempts, telegram_status="sent" if ok else "failed" if attempts >= 8 else "retry", last_error=error, next_attempt_at=utcnow() + timedelta(seconds=retry_after or min(3600, 5 * 2 ** min(attempts, 9)))))
        session.commit()
        delivered += int(ok)
    return delivered


def create_telegram_link(session: Any, user: User, *, settings: Any | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    username = getattr(settings, "telegram_bot_username", "").lstrip("@")
    if not settings.telegram_bot_token or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("telegram_link_not_configured")
    now = utcnow()
    # Only the most recent, short-lived token remains usable.
    session.execute(update(TelegramLink).where(TelegramLink.user_id == user.id, TelegramLink.consumed_at.is_(None)).values(consumed_at=now))
    token = secrets.token_urlsafe(24)
    expires = now + timedelta(minutes=10)
    session.add(TelegramLink(user_id=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=expires))
    session.commit()
    return {"url": f"https://t.me/{username}?start={token}", "expires_at": expires, "instructions": "Откройте ссылку и нажмите Start в личном чате с ботом. Привязка подтверждается сообщением Telegram."}


def apply_telegram_updates(session: Any, updates: list[dict[str, Any]], *, now: datetime | None = None) -> int:
    now = now or utcnow()
    state = session.scalar(select(TelegramBotState).where(TelegramBotState.id == 1).with_for_update())
    if state is None:
        try:
            with session.begin_nested():
                session.add(TelegramBotState(id=1, update_offset=0))
                session.flush()
        except IntegrityError:
            pass
        state = session.scalar(select(TelegramBotState).where(TelegramBotState.id == 1).with_for_update())
    linked = 0
    for incoming in sorted((item for item in updates if isinstance(item, dict) and isinstance(item.get("update_id"), int)), key=lambda item: item["update_id"]):
        update_id = incoming["update_id"]
        if update_id < state.update_offset:
            continue
        state.update_offset = update_id + 1
        message = incoming.get("message") or {}
        if not isinstance(message, dict):
            continue
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            continue
        chat_id = chat.get("id")
        if chat.get("type") != "private" or not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0 or sender.get("id") != chat_id:
            continue
        match = re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]{20,64})", str(message.get("text") or "").strip())
        if not match:
            continue
        link = session.scalar(select(TelegramLink).where(TelegramLink.token_hash == hashlib.sha256(match.group(1).encode()).hexdigest(), TelegramLink.consumed_at.is_(None), TelegramLink.expires_at > now).with_for_update())
        if link is None:
            continue
        user = session.get(User, link.user_id)
        other = session.scalar(select(User.id).where(User.telegram_chat_id == str(chat_id), User.id != link.user_id))
        if user is None or not user.active or other:
            continue
        user.telegram_chat_id = str(chat_id)
        link.consumed_at = now
        session.execute(update(Notification).where(Notification.user_id == user.id, Notification.telegram_status == "unconfigured").values(telegram_status="pending", next_attempt_at=now, last_error=None))
        create_notification(session, user.id, "telegram_linked", "Telegram подключён", "Ваш личный чат подтверждён. Теперь уведомления могут приходить при свёрнутом приложении.", dedupe_key=f"telegram_linked:{link.id}")
        linked += 1
    session.commit()
    return linked


async def _fetch_telegram_updates(token: str, offset: int, *, client: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    _protect_transport_logs()
    owned = client is None
    client = client or httpx.AsyncClient(timeout=5, follow_redirects=False)
    try:
        response = await client.post(f"https://api.telegram.org/bot{token}/getUpdates", json={"offset": offset, "timeout": 0, "limit": 100, "allowed_updates": ["message"]}, timeout=5)
        if response.status_code != 200:
            return []
        data = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("result"), list):
            return []
        return data["result"]
    except (httpx.HTTPError, ValueError):
        return []
    finally:
        if owned:
            await client.aclose()


def poll_telegram_updates(session: Any, *, settings: Any | None = None, client: httpx.AsyncClient | None = None) -> int:
    settings = settings or get_settings()
    if not settings.telegram_bot_token:
        return 0
    state = session.get(TelegramBotState, 1)
    offset = state.update_offset if state else 0
    session.commit()  # Never hold a DB lock while contacting Telegram.
    updates = asyncio.run(_fetch_telegram_updates(settings.telegram_bot_token, offset, client=client))
    return apply_telegram_updates(session, updates)
