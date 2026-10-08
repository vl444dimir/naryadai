from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db, utcnow
from ..models import Notification, User
from ..security import get_current_user
from ..services.notifications import create_notification, create_telegram_link

router = APIRouter(prefix="/notifications", tags=["notifications"])


def serialize_notification(item: Notification) -> dict:
    return {"id": item.id, "order_id": item.order_id, "kind": item.kind, "title": item.title, "body": item.body, "created_at": item.created_at, "read_at": item.read_at, "telegram_status": item.telegram_status, "delivery_attempts": item.attempts, "delivery_error": item.last_error}


@router.get("")
def inbox(unread_only: bool = False, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    items = db.scalars(query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit).offset(offset))
    unread = db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None)))
    return {"items": [serialize_notification(item) for item in items], "unread_count": unread}


@router.get("/status")
def delivery_status(user: User = Depends(get_current_user)):
    settings = get_settings()
    return {"inbox": "enabled", "telegram": {"configured": bool(settings.telegram_bot_token), "link_available": bool(settings.telegram_bot_token and settings.telegram_bot_username), "linked": bool(user.telegram_chat_id), "delivery_mode": "durable_outbox", "sound": "depends_on_device_and_telegram_settings"}, "worker_mode": "embedded" if settings.worker_enabled else "standalone_required"}


class TelegramPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str | None = Field(default=None, pattern=r"^-?\d{1,20}$")


@router.post("/telegram/link", status_code=201)
def link_telegram(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        return create_telegram_link(db, user)
    except ValueError:
        raise HTTPException(409, "Настройте токен и имя Telegram-бота на сервере.") from None


@router.put("/telegram")
def set_telegram_preferences(body: TelegramPreferences, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # A user's explicit preference; no global token or another user's linkage
    # can be changed through this route. Clearing disables future deliveries.
    if body.chat_id is not None:
        raise HTTPException(403, "Привязка требует подтверждения владельца чата через бота; произвольный chat ID не принимается.")
    user.telegram_chat_id = None
    db.commit()
    return {"linked": bool(user.telegram_chat_id)}


@router.post("/test", status_code=202)
def test_notification(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    settings = get_settings()
    if not settings.telegram_bot_token or not user.telegram_chat_id:
        raise HTTPException(409, "Для тестового push настройте Telegram-бота и свой chat ID; входящий список работает отдельно.")
    now = utcnow()
    item = create_notification(db, user.id, "test", "Тест уведомления НарядAI", "Канал настроен. Звук зависит от настроек телефона и Telegram.", dedupe_key=f"test:{int(now.timestamp() // 60)}")
    db.commit()
    return {"notification": serialize_notification(item), "delivery": "queued"}


@router.post("/read-all")
def read_all(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    result = db.execute(update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None)).values(read_at=utcnow()))
    db.commit()
    return {"updated": result.rowcount}


@router.post("/{notification_id}/read")
def mark_read(notification_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = db.scalar(select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id))
    if item is None:
        raise HTTPException(404, "Уведомление не найдено")
    if item.read_at is None:
        item.read_at = utcnow()
    db.commit()
    return serialize_notification(item)
