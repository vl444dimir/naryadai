import asyncio
import json
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy import select, or_, and_
from sqlalchemy.orm import Session
from ..db import SessionLocal, get_db, utcnow
from ..models import AuthSession, OrderEvent, OrderEventRevocation, User, WorkOrder
from ..security import bearer, get_current_user, token_hash

router = APIRouter(prefix="/events", tags=["Обновление статусов"])


def visible_events(db, user, after, limit):
    if user.role == "worker":
        current_access = or_(WorkOrder.executor_id == user.id, WorkOrder.working_executor_id == user.id, WorkOrder.brigade_id == user.brigade_id if user.brigade_id else False)
        query = select(OrderEvent, current_access.label("current_access")).join(WorkOrder).outerjoin(OrderEventRevocation, and_(OrderEventRevocation.event_id == OrderEvent.id, OrderEventRevocation.user_id == user.id)).where(OrderEvent.id > after, or_(current_access, OrderEventRevocation.user_id.is_not(None)))
        rows = db.execute(query.order_by(OrderEvent.id).limit(limit))
    else:
        query = select(OrderEvent).join(WorkOrder).where(OrderEvent.id > after)
        rows = ((record, True) for record in db.scalars(query.order_by(OrderEvent.id).limit(limit)))
    return [{"id": e.id, "order_id": e.order_id, "action": e.action if access else "assignment_revoked", "occurred_at": e.occurred_at, "to_status": e.to_status if access else None, "reason": e.reason if access else None} for e, access in rows]


@router.get("")
def poll(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    items = visible_events(db, user, after, limit)
    return {"items": items, "cursor": items[-1]["id"] if items else after, "poll_interval_seconds": 2}


@router.get("/stream")
def stream(after: int = Query(0, ge=0), user: User = Depends(get_current_user), credentials=Depends(bearer)):
    user_id, digest = user.id, token_hash(credentials.credentials)
    async def generate():
        cursor = after
        while True:
            with SessionLocal() as db:
                auth = db.scalar(select(AuthSession.id).where(AuthSession.token_hash == digest, AuthSession.revoked.is_(False), AuthSession.expires_at > utcnow()))
                current = db.get(User, user_id)
                if not auth or not current or not current.active:
                    yield 'event: session_expired\ndata: {}\n\n'
                    return
                items = visible_events(db, current, cursor, 100)
            for item in items:
                cursor = item["id"]
                yield f'id: {cursor}\nevent: order_changed\ndata: {json.dumps(jsonable_encoder(item), ensure_ascii=False)}\n\n'
            if not items:
                yield ': heartbeat\n\n'
            await asyncio.sleep(1)
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
