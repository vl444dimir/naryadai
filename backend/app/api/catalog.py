from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import AuthSession, Brigade, Equipment, FaultCode, Material, MaterialNorm, MaterialUsage, Section, Shift, ShiftMember, Submission, TimeNorm, User, WorkOrder
from ..schemas import StrictModel
from ..security import get_current_user, hash_password, require_roles
from .auth import public_user

router = APIRouter(prefix="/catalog", tags=["Справочники"])


class SectionData(StrictModel):
    name: str = Field(min_length=2, max_length=200)
    external_id: str | None = Field(default=None, max_length=100)
    active: bool = True


class BrigadeData(StrictModel):
    name: str = Field(min_length=2, max_length=200)
    active: bool = True


class EquipmentData(SectionData):
    inventory_number: str = Field(min_length=1, max_length=100)
    section_id: UUID
    equipment_type: str = Field(min_length=1, max_length=100)
    criticality: int = Field(default=1, ge=1, le=5)


class FaultData(StrictModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=2, max_length=200)
    specialty: str | None = Field(default=None, max_length=100)
    active: bool = True


class MaterialData(SectionData):
    unit: str = Field(min_length=1, max_length=30)
    allowed_fault_prefixes: list[str] = Field(default_factory=list, max_length=20)


class NormData(StrictModel):
    name: str = Field(min_length=2, max_length=200)
    equipment_type: str = Field(min_length=1, max_length=100)
    fault_code_id: UUID
    minutes: int = Field(ge=1, le=43200)


class MaterialNormData(StrictModel):
    equipment_type: str = Field(min_length=1, max_length=100)
    fault_code_id: UUID
    material_id: UUID
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)


def serialize(instance):
    return {c.name: getattr(instance, c.name) for c in instance.__table__.columns}


def commit(db):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Значение уже существует или связано с другими данными")


def validate_references(db, data):
    for key, model in [("section_id", Section), ("fault_code_id", FaultCode), ("material_id", Material), ("brigade_id", Brigade)]:
        if data.get(key) and not db.get(model, str(data[key])):
            raise HTTPException(422, f"Связанный справочник {key} не найден")


def register_resource(name, model, schema):
    def list_items(section_id: UUID | None = None, active_only: bool = False, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        query = select(model)
        if section_id and hasattr(model, "section_id"):
            query = query.where(model.section_id == str(section_id))
        if active_only and hasattr(model, "active"):
            query = query.where(model.active.is_(True))
        return {"items": [serialize(item) for item in db.scalars(query.order_by(model.id).offset(offset).limit(limit))]}

    def create(body, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        require_roles(user, "admin")
        data = body.model_dump(mode="json")
        validate_references(db, data)
        item = model(**data)
        db.add(item)
        commit(db)
        return serialize(item)

    def update(item_id: UUID, body, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        require_roles(user, "admin")
        item = db.scalar(select(model).where(model.id == str(item_id)).with_for_update())
        if not item:
            raise HTTPException(404, "Запись не найдена")
        data = body.model_dump(mode="json")
        validate_references(db, data)
        if model is Equipment and item.section_id != data["section_id"]:
            # Historical orders must retain a consistent equipment/section link.
            if db.scalar(select(WorkOrder.id).where(WorkOrder.equipment_id == item.id).limit(1)):
                raise HTTPException(409, "Оборудование с историей нельзя перемещать между участками")
        if model is Equipment and item.equipment_type != data["equipment_type"] and db.scalar(select(WorkOrder.id).where(WorkOrder.equipment_id == item.id).limit(1)):
            raise HTTPException(409, "Тип оборудования с историей нельзя изменять")
        if model is Material and item.unit != data["unit"] and db.scalar(select(MaterialUsage.id).where(MaterialUsage.material_id == item.id).limit(1)):
            raise HTTPException(409, "Единицу материала с историей списаний нельзя изменять; создайте новую позицию")
        if model is FaultCode and item.code != data["code"] and db.scalar(select(Submission.id).where(Submission.fault_code_id == item.id).limit(1)):
            raise HTTPException(409, "Использованный шифр нельзя изменять; создайте новый шифр")
        for key, value in data.items():
            setattr(item, key, value)
        commit(db)
        return serialize(item)

    create.__annotations__["body"] = schema
    update.__annotations__["body"] = schema
    router.add_api_route(f"/{name}", list_items, methods=["GET"], name=f"list_{name}")
    router.add_api_route(f"/{name}", create, methods=["POST"], status_code=201, name=f"create_{name}")
    router.add_api_route(f"/{name}/{{item_id}}", update, methods=["PUT"], name=f"update_{name}")


for args in [("sections", Section, SectionData), ("brigades", Brigade, BrigadeData), ("equipment", Equipment, EquipmentData), ("fault-codes", FaultCode, FaultData), ("materials", Material, MaterialData), ("time-norms", TimeNorm, NormData), ("material-norms", MaterialNorm, MaterialNormData)]:
    register_resource(*args)


class UserCreate(StrictModel):
    login: str = Field(min_length=2, max_length=100)
    full_name: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    pin: str | None = Field(default=None, pattern=r"^\d{4,8}$")
    role: Literal["worker", "master", "leader", "admin"] = "worker"
    specialty: str = Field(default="слесарь", min_length=1, max_length=100)
    grade: int = Field(default=3, ge=1, le=8)
    brigade_id: UUID | None = None
    active: bool = True


class UserUpdate(StrictModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    pin: str | None = Field(default=None, pattern=r"^\d{4,8}$")
    role: Literal["worker", "master", "leader", "admin"] | None = None
    specialty: str | None = Field(default=None, min_length=1, max_length=100)
    grade: int | None = Field(default=None, ge=1, le=8)
    brigade_id: UUID | None = None
    active: bool | None = None


def availability(db, employee):
    orders = list(db.scalars(select(WorkOrder).where((WorkOrder.executor_id == employee.id) | (WorkOrder.working_executor_id == employee.id), WorkOrder.status.in_(["issued", "accepted", "queued", "in_progress", "paused", "rework"]))))
    current = next((o for o in orders if o.status == "in_progress"), None) or next((o for o in orders if o.status in {"paused", "accepted", "rework"}), None)
    queued = sum(o.status in {"queued", "issued"} for o in orders)
    status = "off_shift" if not employee.on_shift else "busy" if current else "queued" if queued else "free"
    return {**public_user(employee), "availability": status, "color": {"off_shift": "gray", "busy": "yellow", "queued": "blue", "free": "green"}[status], "current_order_id": current.id if current else None, "current_order_number": current.number if current else None, "queue_count": queued}


@router.get("/users")
def users(role: str | None = None, brigade_id: UUID | None = None, on_shift: bool | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = select(User)
    if user.role == "worker":
        query = query.where(User.id == user.id)
    if role:
        query = query.where(User.role == role)
    if brigade_id:
        query = query.where(User.brigade_id == str(brigade_id))
    if on_shift is not None:
        query = query.where(User.on_shift == on_shift)
    return {"items": [availability(db, item) for item in db.scalars(query.order_by(User.full_name))]}


@router.post("/users", status_code=201)
def create_user(body: UserCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    require_roles(user, "admin")
    data = body.model_dump(mode="json", exclude={"password", "pin"})
    validate_references(db, data)
    employee = User(**data, password_hash=hash_password(body.password), pin_hash=hash_password(body.pin) if body.pin else None)
    db.add(employee)
    commit(db)
    return public_user(employee)


@router.patch("/users/{user_id}")
def update_user(user_id: UUID, body: UserUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    require_roles(user, "admin")
    employee = db.get(User, str(user_id))
    if not employee:
        raise HTTPException(404, "Сотрудник не найден")
    data = body.model_dump(mode="json", exclude_unset=True, exclude={"password", "pin"})
    validate_references(db, data)
    if employee.id == user.id and (data.get("active") is False or ("role" in data and data["role"] != "admin")):
        raise HTTPException(409, "Нельзя отключить собственный административный доступ")
    if any(key in data and data[key] is None for key in {"full_name", "role", "specialty", "grade", "active"}):
        raise HTTPException(422, "Обязательное поле нельзя очистить")
    for key, value in data.items():
        setattr(employee, key, value)
    if body.password:
        employee.password_hash = hash_password(body.password)
    if "pin" in body.model_fields_set:
        employee.pin_hash = hash_password(body.pin) if body.pin else None
    if body.password or "pin" in body.model_fields_set or "role" in data or data.get("active") is False:
        for auth in db.scalars(select(AuthSession).where(AuthSession.user_id == employee.id)):
            auth.revoked = True
    commit(db)
    return public_user(employee)


class Attendance(StrictModel):
    on_shift: bool


@router.put("/users/{user_id}/attendance")
def attendance(user_id: UUID, body: Attendance, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    require_roles(user, "master", "admin")
    employee = db.get(User, str(user_id))
    if not employee:
        raise HTTPException(404, "Сотрудник не найден")
    employee.on_shift = body.on_shift
    commit(db)
    return availability(db, employee)


class ShiftData(StrictModel):
    name: str = Field(min_length=2, max_length=200)
    starts_at: datetime
    ends_at: datetime
    user_ids: list[UUID] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def dates(self):
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None or self.ends_at <= self.starts_at:
            raise ValueError("Укажите корректный период смены с часовым поясом")
        if len(set(self.user_ids)) != len(self.user_ids):
            raise ValueError("Сотрудник указан дважды")
        return self


@router.get("/shifts")
def shifts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = select(Shift)
    if user.role == "worker":
        query = query.join(ShiftMember).where(ShiftMember.user_id == user.id)
    return {"items": [serialize(s) for s in db.scalars(query.order_by(Shift.starts_at.desc()).limit(200))]}


@router.post("/shifts", status_code=201)
def create_shift(body: ShiftData, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    require_roles(user, "master", "admin")
    for user_id in body.user_ids:
        employee = db.get(User, str(user_id))
        if not employee or not employee.active:
            raise HTTPException(422, "Сотрудник не найден")
    shift = Shift(name=body.name, starts_at=body.starts_at, ends_at=body.ends_at)
    db.add(shift)
    db.flush()
    for user_id in body.user_ids:
        db.add(ShiftMember(shift_id=shift.id, user_id=str(user_id)))
    commit(db)
    return serialize(shift)
