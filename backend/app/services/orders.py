import hashlib
import json
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select, or_, func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError
from ..db import utcnow
from ..models import Assessment, Brigade, Equipment, EquipmentDowntime, FaultCode, Material, MaterialUsage, Notification, Operation, OrderCounter, OrderEvent, OrderEventRevocation, Photo, ReviewJob, Section, Shift, Submission, TimeNorm, User, WorkOrder
from ..security import ensure_order_access, require_roles

DONE = {"closed", "cancelled"}
PRE_RESULT = {"issued", "accepted", "queued", "rejected", "in_progress", "paused", "rework"}


def event(db, order, actor, action, old=None, reason=None, details=None, revoked_user_ids=()):
    event_details = dict(details or {})
    if actor:
        event_details.update(actor_role=actor.role, actor_brigade_id=actor.brigade_id)
    record = OrderEvent(order_id=order.id, actor_id=actor.id if actor else None, action=action, from_status=old, to_status=order.status, reason=reason, details=event_details)
    db.add(record)
    if revoked_user_ids:
        db.flush()
        db.add_all(OrderEventRevocation(event_id=record.id, user_id=target) for target in set(revoked_user_ids))


def notify(db, user_id, order, kind, title, body, key):
    if not db.scalar(select(Notification.id).where(Notification.user_id == user_id, Notification.dedupe_key == key)):
        db.add(Notification(user_id=user_id, order_id=order.id, kind=kind, title=title, body=body, dedupe_key=key))


def recipients(db, order):
    if order.executor_id:
        return [order.executor_id]
    return list(db.scalars(select(User.id).where(User.brigade_id == order.brigade_id, User.active.is_(True), User.role == "worker")))


def serialize_order(db, order, user):
    ensure_order_access(db, user, order)
    data = {column.name: getattr(order, column.name) for column in WorkOrder.__table__.columns}
    now = utcnow()
    controlled = order.status not in DONE | {"performed", "ai_review"}
    data["overdue"] = controlled and order.due_at < now
    data["overdue_minutes"] = max(0, int((now - order.due_at).total_seconds() / 60)) if data["overdue"] else 0
    equipment = db.get(Equipment, order.equipment_id)
    data["equipment"] = {"id": equipment.id, "name": equipment.name, "inventory_number": equipment.inventory_number} if equipment else None
    data["photos"] = [{"id": p.id, "kind": p.kind, "captured_at": p.captured_at, "uploaded_at": p.uploaded_at, "source": p.source, "synthetic": p.synthetic, "result_version": p.result_version, "url": f"/api/photos/{p.id}/file"} for p in db.scalars(select(Photo).where(Photo.order_id == order.id).order_by(Photo.uploaded_at))]
    data["materials"] = [{"material_id": m.material_id, "quantity": float(m.quantity), "unit": db.get(Material, m.material_id).unit} for m in db.scalars(select(MaterialUsage).where(MaterialUsage.order_id == order.id, MaterialUsage.result_version == order.result_version))]
    assessment = db.scalar(select(Assessment).where(Assessment.order_id == order.id, Assessment.input_version == order.result_version))
    data["assessment"] = {key: getattr(assessment, key) for key in ("id", "verdict", "score", "explanation", "good", "improve", "findings", "details", "engine", "model", "needs_master_review", "created_at")} if assessment else None
    data["events"] = [{"id": e.id, "actor_id": e.actor_id, "action": e.action, "occurred_at": e.occurred_at, "from_status": e.from_status, "to_status": e.to_status, "reason": e.reason, "details": e.details} for e in db.scalars(select(OrderEvent).where(OrderEvent.order_id == order.id).order_by(OrderEvent.id))]
    data["final_score"] = order.master_score if order.master_score is not None else (assessment.score if assessment else None)
    return data


def load_order(db, user, order_id, expected_version=None, write=False):
    order = db.scalar(select(WorkOrder).where(WorkOrder.id == str(order_id)).with_for_update())
    if not order:
        raise HTTPException(404, "Наряд не найден")
    ensure_order_access(db, user, order, write=write)
    if expected_version is not None and order.version != expected_version:
        raise HTTPException(409, {"message": "Наряд изменился. Обновите карточку", "current_version": order.version})
    return order


def run_operation(db, user, body, scope, callback):
    from fastapi.encoders import jsonable_encoder
    operation_id = str(body.operation_id)
    request_hash = hashlib.sha256(json.dumps({"scope": scope, "body": body.model_dump(mode="json")}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def replay(existing):
        if existing.actor_id != user.id or existing.request_hash != request_hash:
            raise HTTPException(409, "Этот идентификатор операции уже использован")
        return existing.response

    existing = db.get(Operation, operation_id)
    if existing:
        return replay(existing)
    operation = Operation(id=operation_id, actor_id=user.id, request_hash=request_hash, response={})
    try:
        db.add(operation)
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.get(Operation, operation_id)
        if existing:
            return replay(existing)
        raise HTTPException(409, "Конфликт операции")
    try:
        order = callback()
        db.flush()
        response = jsonable_encoder(serialize_order(db, order, user))
        operation.response = response
        db.commit()
        return response
    except (IntegrityError, StaleDataError):
        db.rollback()
        raise HTTPException(409, "Конфликт назначения или версии. Обновите карточку")
    except OperationalError as exc:
        db.rollback()
        if getattr(exc.orig, "sqlstate", None) in {"40P01", "40001"}:
            raise HTTPException(409, {"message": "Конкурентное изменение. Повторите действие", "code": "transaction_retry"}) from None
        raise
    except Exception:
        db.rollback()
        raise


def check_assignment(db, executor_id, brigade_id):
    if executor_id:
        employee = db.get(User, str(executor_id))
        if not employee or not employee.active or employee.role != "worker":
            raise HTTPException(422, "Недоступный исполнитель")
        return str(executor_id), None
    brigade = db.get(Brigade, str(brigade_id))
    if not brigade or not brigade.active:
        raise HTTPException(422, "Недоступная бригада")
    if not db.scalar(select(User.id).where(User.brigade_id == brigade.id, User.role == "worker", User.active.is_(True)).limit(1)):
        raise HTTPException(422, "В бригаде нет исполнителей")
    return None, brigade.id


def create_order(db, user, body):
    require_roles(user, "master", "admin")
    def create():
        eq = db.scalar(select(Equipment).where(Equipment.id == str(body.equipment_id)).with_for_update())
        if not eq or not eq.active or not db.get(Section, eq.section_id).active:
            raise HTTPException(422, "Недоступное оборудование")
        if body.section_id and str(body.section_id) != eq.section_id:
            raise HTTPException(422, "Оборудование не относится к участку")
        executor, brigade = check_assignment(db, body.executor_id, body.brigade_id)
        if body.shift_id and not db.get(Shift, str(body.shift_id)):
            raise HTTPException(422, "Смена не найдена")
        minutes = body.norm_minutes
        if body.time_norm_id:
            norm = db.get(TimeNorm, str(body.time_norm_id))
            if not norm or norm.equipment_type != eq.equipment_type:
                raise HTTPException(422, "Норматив не соответствует оборудованию")
            minutes = norm.minutes
        now = utcnow()
        due = body.due_at or now + timedelta(minutes=minutes)
        if due <= now:
            raise HTTPException(422, "Срок должен быть в будущем")
        counter = db.scalar(select(OrderCounter).where(OrderCounter.id == 1).with_for_update())
        if counter is None:
            raise HTTPException(503, "База не инициализирована: выполните миграции")
        counter.value += 1
        # A demo account does not prove that newly entered text/images are synthetic.
        executor_brigade = db.get(User, executor).brigade_id if executor else None
        order = WorkOrder(number=counter.value, work_type=body.work_type, description=body.description, comment=body.comment, equipment_id=eq.id, section_id=eq.section_id, executor_id=executor, executor_brigade_id=executor_brigade, brigade_id=brigade, master_id=user.id, shift_id=str(body.shift_id) if body.shift_id else None, priority=body.priority, norm_minutes=minutes, due_at=due, synthetic=False)
        db.add(order)
        db.flush()
        event(db, order, user, "issued", details={"executor_id": executor, "executor_brigade_id": executor_brigade, "brigade_id": brigade, "due_at": due.isoformat(), "norm_minutes": minutes, "priority": order.priority})
        if body.equipment_stopped:
            db.add(EquipmentDowntime(equipment_id=eq.id, order_id=order.id, started_at=now, cause=body.description, actor_id=user.id))
            event(db, order, user, "downtime_started", details={"equipment_id": eq.id})
        for target in recipients(db, order):
            notify(db, target, order, "assigned", f"Наряд №{order.number}", f"{eq.name}: {order.description}. Приоритет: {order.priority}", f"issued:{order.id}")
        return order
    return run_operation(db, user, body, "create_order", create)


def action_order(db, user, order_id, body):
    def act():
        order = load_order(db, user, order_id, body.expected_version, write=True)
        # FK inserts hold KEY SHARE; NO KEY UPDATE serializes assignments without
        # deadlocking when two operation reservations reference the same employee.
        db.scalar(select(User).where(User.id == user.id).with_for_update(key_share=True).execution_options(populate_existing=True))
        ensure_order_access(db, user, order, write=True)
        old = order.status
        allowed = {
            "accept": {"issued", "queued"}, "queue": {"issued", "accepted"}, "reject": {"issued", "accepted", "queued"},
            "start": {"accepted", "queued", "rework"}, "pause": {"in_progress"}, "resume": {"paused"},
        }
        if old not in allowed[body.action]:
            raise HTTPException(409, "Недопустимый переход статуса")
        if body.action in {"accept", "queue", "start", "resume"} and not user.on_shift:
            raise HTTPException(409, "Исполнитель не на смене")
        targets = {"accept": "accepted", "queue": "queued", "reject": "rejected", "start": "in_progress", "pause": "paused", "resume": "in_progress"}
        order.status = targets[body.action]
        now = utcnow()
        if body.action in {"accept", "queue"}:
            order.accepted_at = order.accepted_at or now
            order.working_executor_id = user.id
        if body.action == "queue":
            last = db.scalar(select(func.max(WorkOrder.queue_position)).where(or_(WorkOrder.executor_id == user.id, WorkOrder.working_executor_id == user.id))) or 0
            order.queue_position = last + 1
        if body.action in {"start", "resume"}:
            # Lock the employee row as well as the work order; the unique column is a second guard.
            db.scalar(select(User).where(User.id == user.id).with_for_update(key_share=True))
            active = db.scalar(select(WorkOrder.id).where(WorkOrder.active_executor_id == user.id, WorkOrder.id != order.id))
            if active:
                raise HTTPException(409, "Сначала приостановите текущий наряд")
            order.working_executor_id = user.id
            order.active_executor_id = user.id
            order.started_at = order.started_at or now
            order.queue_position = None
        if body.action in {"pause", "reject"}:
            order.active_executor_id = None
        if body.action == "reject":
            order.rejection_justified = None
            notify(db, order.master_id, order, "rejected", f"Отказ от наряда №{order.number}", body.reason, f"reject:{order.id}:{order.version}")
        if body.action == "pause":
            notify(db, order.master_id, order, "paused", f"Приостановлен наряд №{order.number}", body.reason, f"pause:{order.id}:{order.version}")
        event(db, order, user, {"accept": "accepted", "queue": "queued", "reject": "rejected", "start": "started", "pause": "paused", "resume": "resumed"}[body.action], old, body.reason)
        return order
    return run_operation(db, user, body, f"action:{order_id}", act)


def submit_result(db, user, order_id, body):
    def submit():
        order = load_order(db, user, order_id, body.expected_version, write=True)
        if order.status not in {"in_progress", "paused", "rework"}:
            raise HTTPException(409, "Результат нельзя отправить в текущем статусе")
        if body.fault_code_id:
            fault = db.scalar(select(FaultCode).where(FaultCode.id == str(body.fault_code_id)).with_for_update())
            if not fault or not fault.active:
                raise HTTPException(422, "Шифр неисправности не найден")
        for usage in body.materials:
            material = db.scalar(select(Material).where(Material.id == str(usage.material_id)).with_for_update())
            if not material or not material.active:
                raise HTTPException(422, "Материал не найден")
        photos = []
        for photo_id in body.photo_ids:
            photo = db.scalar(select(Photo).where(Photo.id == str(photo_id)).with_for_update())
            if not photo or photo.order_id != order.id or photo.kind != "after" or photo.author_id != user.id or photo.result_version is not None:
                raise HTTPException(422, "Фото результата недоступно или уже использовано")
            photos.append(photo)
        old = order.status
        order.result_version += 1
        order.work_description = body.work_description
        order.fault_code_id = str(body.fault_code_id) if body.fault_code_id else None
        order.materials_none = body.materials_none
        order.submitted_at = utcnow()
        order.active_executor_id = None
        order.status = "performed"
        order.master_score = order.master_verdict = order.master_comment = None
        order.working_executor_id = user.id
        db.add(Submission(order_id=order.id, result_version=order.result_version, actor_id=user.id, actor_brigade_id=user.brigade_id, work_description=body.work_description, fault_code_id=order.fault_code_id, materials_none=body.materials_none, comment=body.comment, submitted_at=order.submitted_at))
        for usage in body.materials:
            db.add(MaterialUsage(order_id=order.id, result_version=order.result_version, material_id=str(usage.material_id), quantity=usage.quantity))
        for photo in photos:
            photo.result_version = order.result_version
        event(db, order, user, "performed", old, body.comment, {"result_version": order.result_version})
        order.status = "ai_review"
        event(db, order, user, "review_requested", "performed", details={"result_version": order.result_version})
        db.add(ReviewJob(order_id=order.id, input_version=order.result_version))
        return order
    return run_operation(db, user, body, f"submit:{order_id}", submit)


def master_decision(db, user, order_id, body):
    require_roles(user, "master", "admin")
    def decide():
        order = load_order(db, user, order_id, body.expected_version)
        if order.status not in {"ai_review", "rework"}:
            raise HTTPException(409, "Наряд не ожидает приёмки")
        assessment = db.scalar(select(Assessment).where(Assessment.order_id == order.id, Assessment.input_version == order.result_version))
        if not assessment:
            raise HTTPException(409, "Проверка ещё не завершена")
        if body.verdict != "requires_rework":
            has_photo = db.scalar(select(Photo.id).where(Photo.order_id == order.id, Photo.result_version == order.result_version, Photo.kind == "after"))
            has_material = db.scalar(select(MaterialUsage.id).where(MaterialUsage.order_id == order.id, MaterialUsage.result_version == order.result_version))
            if not order.work_description or not order.fault_code_id or (not has_material and not order.materials_none) or (order.work_type == "unplanned" and not has_photo):
                raise HTTPException(422, "Сначала заполните обязательные поля результата")
            if body.score is None and assessment.score is None:
                raise HTTPException(422, "Укажите оценку мастера")
        changed = body.verdict != assessment.verdict or (body.score is not None and body.score != assessment.score)
        if (changed or body.verdict != "accepted") and not body.reason:
            raise HTTPException(422, "Для замечания или изменения оценки требуется причина")
        old = order.status
        order.master_verdict = body.verdict
        order.master_score = body.score if body.score is not None else assessment.score
        order.master_comment = body.reason
        if body.verdict == "requires_rework":
            if order.status != "rework":
                order.rework_count += 1
            order.status = "rework"
            action = "rework"
        else:
            order.status = "closed"
            order.closed_at = utcnow()
            action = "closed"
        event(db, order, user, action, old, body.reason, {"verdict": body.verdict, "score": order.master_score, "assessment_id": assessment.id})
        for target in recipients(db, order):
            notify(db, target, order, action, f"Результат наряда №{order.number}", body.reason or "Работа принята мастером", f"decision:{order.id}:{order.result_version}:{body.operation_id}")
        return order
    return run_operation(db, user, body, f"decision:{order_id}", decide)


def update_order(db, user, order_id, body):
    require_roles(user, "master", "admin")
    def update():
        order = load_order(db, user, order_id, body.expected_version)
        if order.status in DONE:
            raise HTTPException(409, "Завершённый наряд нельзя изменять")
        old = order.status
        changes = {}
        revoked_user_ids = []
        if body.cancel:
            order.status = "cancelled"
            order.cancelled_at = utcnow()
            order.active_executor_id = None
            order.queue_position = None
        else:
            if order.status not in PRE_RESULT:
                raise HTTPException(409, "Сначала завершите проверку результата")
            if body.executor_id and body.brigade_id:
                raise HTTPException(422, "Укажите одного исполнителя или бригаду")
            if body.executor_id or body.brigade_id:
                revoked_user_ids = recipients(db, order)
                if order.working_executor_id:
                    revoked_user_ids.append(order.working_executor_id)
                executor, brigade = check_assignment(db, body.executor_id, body.brigade_id)
                executor_brigade = db.get(User, executor).brigade_id if executor else None
                changes["assignment"] = {"old_executor_id": order.executor_id, "old_executor_brigade_id": order.executor_brigade_id, "old_brigade_id": order.brigade_id, "executor_id": executor, "executor_brigade_id": executor_brigade, "brigade_id": brigade}
                order.executor_id, order.brigade_id = executor, brigade
                order.executor_brigade_id = executor_brigade
                order.working_executor_id = order.active_executor_id = None
                order.accepted_at = order.started_at = None
                order.queue_position = None
                order.status = "issued"
                for target in recipients(db, order):
                    notify(db, target, order, "assigned", f"Переназначен наряд №{order.number}", body.reason, f"reassigned:{order.id}:{order.version}")
            if body.priority:
                if body.priority == "emergency" and order.work_type != "unplanned":
                    raise HTTPException(422, "Аварийный приоритет допустим для внеплановой работы")
                changes["priority"] = {"old": order.priority, "new": body.priority}
                order.priority = body.priority
            if body.due_at:
                if body.due_at <= utcnow():
                    raise HTTPException(422, "Новый срок должен быть в будущем")
                changes["due_at"] = {"old": order.due_at.isoformat(), "new": body.due_at.isoformat()}
                order.due_at = body.due_at
            if not changes:
                raise HTTPException(422, "Изменения не указаны")
        event(db, order, user, "cancelled" if body.cancel else "reassigned" if "assignment" in changes else "updated", old, body.reason, changes, revoked_user_ids=revoked_user_ids)
        return order
    return run_operation(db, user, body, f"update:{order_id}", update)


def downtime(db, user, order_id, body):
    def change():
        order = load_order(db, user, order_id, body.expected_version)
        if user.role == "leader":
            raise HTTPException(403, "Руководитель имеет доступ к просмотру")
        if user.role == "worker":
            ensure_order_access(db, user, order, write=True)
        if body.action == "start" and order.status in DONE:
            raise HTTPException(409, "Нельзя начать простой по завершённому наряду")
        current = db.scalar(select(EquipmentDowntime).where(EquipmentDowntime.order_id == order.id, EquipmentDowntime.ended_at.is_(None)).with_for_update())
        if body.action == "start":
            if current:
                raise HTTPException(409, "Простой уже начат")
            db.add(EquipmentDowntime(equipment_id=order.equipment_id, order_id=order.id, started_at=utcnow(), cause=body.reason, actor_id=user.id))
        else:
            if not current:
                raise HTTPException(409, "Открытый простой не найден")
            current.ended_at = utcnow()
        # Force a version increment even though only a related table changed.
        order.version += 1
        event(db, order, user, f"downtime_{'started' if body.action == 'start' else 'ended'}", order.status, body.reason)
        return order
    return run_operation(db, user, body, f"downtime:{order_id}", change)
