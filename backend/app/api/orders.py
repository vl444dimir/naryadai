from uuid import UUID
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, or_, case, func
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User, WorkOrder, OrderEvent, FaultCode
from ..schemas import OrderCreate, OrderAction, SubmitResult, MasterDecision, OrderUpdate, DowntimeInput, VersionInput, StrictModel
from ..security import get_current_user, require_roles
from ..services import orders

router = APIRouter(prefix="/orders", tags=["Наряды"])


@router.post("", status_code=201)
def create(body: OrderCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return orders.create_order(db, user, body)


@router.get("")
def listing(status: str | None = None, section_id: UUID | None = None, equipment_id: UUID | None = None, executor_id: UUID | None = None, brigade_id: UUID | None = None, priority: str | None = None, overdue: bool | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..db import utcnow
    from ..models import STATUSES, PRIORITIES
    now = utcnow()
    query = select(WorkOrder)
    if user.role == "worker":
        query = query.where(or_(WorkOrder.executor_id == user.id, WorkOrder.working_executor_id == user.id, WorkOrder.brigade_id == user.brigade_id if user.brigade_id else False))
    if status:
        if status not in STATUSES:
            raise HTTPException(422, "Неизвестный статус")
        query = query.where(WorkOrder.status == status)
    if priority:
        if priority not in PRIORITIES:
            raise HTTPException(422, "Неизвестный приоритет")
        query = query.where(WorkOrder.priority == priority)
    for name, value in [("section_id", section_id), ("equipment_id", equipment_id), ("executor_id", executor_id), ("brigade_id", brigade_id)]:
        if value:
            column = getattr(WorkOrder, name)
            query = query.where(or_(column == str(value), WorkOrder.working_executor_id == str(value)) if name == "executor_id" else column == str(value))
    condition = (WorkOrder.due_at < now) & WorkOrder.status.not_in(["closed", "cancelled", "performed", "ai_review"])
    if overdue is not None:
        query = query.where(condition if overdue else ~condition)
    counts = {stage: 0 for stage in sorted(STATUSES)}
    overdue_count = 0
    aggregate = query.with_only_columns(WorkOrder.status, func.count(WorkOrder.id), func.sum(case((condition, 1), else_=0))).group_by(WorkOrder.status)
    for stage, count, late in db.execute(aggregate):
        counts[stage] = count
        overdue_count += late or 0
    data = list(db.scalars(query.order_by(case((WorkOrder.priority == "emergency", 0), (WorkOrder.priority == "high", 1), else_=2), WorkOrder.queue_position.asc().nulls_last(), WorkOrder.due_at, WorkOrder.number).offset(offset).limit(limit)))
    return {"items": [orders.serialize_order(db, order, user) for order in data], "offset": offset, "limit": limit, "total": sum(counts.values()), "counts_by_status": counts, "overdue_count": overdue_count}


@router.get("/{order_id}")
def get(order_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return orders.serialize_order(db, orders.load_order(db, user, order_id), user)


@router.post("/{order_id}/actions")
def action(order_id: UUID, body: OrderAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return orders.action_order(db, user, order_id, body)


@router.post("/{order_id}/result")
def result(order_id: UUID, body: SubmitResult, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return orders.submit_result(db, user, order_id, body)


@router.post("/{order_id}/decision")
def decision(order_id: UUID, body: MasterDecision, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return orders.master_decision(db, user, order_id, body)


@router.patch("/{order_id}")
def update(order_id: UUID, body: OrderUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return orders.update_order(db, user, order_id, body)


@router.post("/{order_id}/downtime")
def downtime(order_id: UUID, body: DowntimeInput, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return orders.downtime(db, user, order_id, body)


class Comment(VersionInput):
    text: str = Query(min_length=2, max_length=3000)


@router.post("/{order_id}/comments")
def comment(order_id: UUID, body: Comment, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    def add():
        order = orders.load_order(db, user, order_id, body.expected_version)
        if user.role == "leader":
            raise HTTPException(403, "Руководитель имеет доступ к просмотру")
        if user.role == "worker":
            from ..security import ensure_order_access
            ensure_order_access(db, user, order, write=True)
        order.version += 1
        orders.event(db, order, user, "commented", order.status, body.text)
        return order
    return orders.run_operation(db, user, body, f"comment:{order_id}", add)


class RejectionDecision(VersionInput):
    justified: bool
    reason: str = Query(min_length=2, max_length=2000)


@router.post("/{order_id}/rejection-decision")
def rejection(order_id: UUID, body: RejectionDecision, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    require_roles(user, "master", "admin")
    def decide():
        order = orders.load_order(db, user, order_id, body.expected_version)
        rejection_event = db.scalar(select(OrderEvent).where(OrderEvent.order_id == order.id, OrderEvent.action == "rejected").order_by(OrderEvent.id.desc()))
        if not rejection_event:
            raise HTTPException(409, "Отказ от наряда не зарегистрирован")
        order.rejection_justified = body.justified
        order.version += 1
        orders.event(db, order, user, "rejection_reviewed", order.status, body.reason, {"justified": body.justified, "rejection_event_id": rejection_event.id, "executor_id": rejection_event.actor_id})
        return order
    return orders.run_operation(db, user, body, f"rejection:{order_id}", decide)


class RepeatConfirmation(VersionInput):
    previous_order_id: UUID
    reason: str = Query(min_length=2, max_length=2000)


@router.post("/{order_id}/confirm-repeat")
def confirm_repeat(order_id: UUID, body: RepeatConfirmation, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    require_roles(user, "master", "admin")
    def confirm():
        order = orders.load_order(db, user, order_id, body.expected_version)
        previous = orders.load_order(db, user, body.previous_order_id)
        if order.id == previous.id or previous.equipment_id != order.equipment_id or not order.fault_code_id or order.fault_code_id != previous.fault_code_id or previous.status != "closed" or order.work_type != "unplanned" or not previous.closed_at or not (previous.closed_at <= order.created_at <= previous.closed_at + timedelta(days=7)):
            raise HTTPException(422, "Связь повторного отказа не соответствует оборудованию, шифру или семидневному периоду")
        order.version += 1
        orders.event(db, order, user, "repeat_confirmed", order.status, body.reason, {"confirmed_repeat_of": previous.id})
        return order
    return orders.run_operation(db, user, body, f"repeat:{order_id}", confirm)
