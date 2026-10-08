from uuid import uuid4
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, UTCDateTime, utcnow


def uid():
    return str(uuid4())


ROLES = {"master", "worker", "leader", "admin"}
STATUSES = {"issued", "accepted", "queued", "rejected", "in_progress", "paused", "performed", "ai_review", "rework", "closed", "cancelled"}
PRIORITIES = {"emergency", "high", "normal", "planned"}


class Section(Base):
    __tablename__ = "sections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(default=True)


class Brigade(Base):
    __tablename__ = "brigades"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    active: Mapped[bool] = mapped_column(default=True)


class Equipment(Base):
    __tablename__ = "equipment"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    inventory_number: Mapped[str] = mapped_column(String(100), unique=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    equipment_type: Mapped[str] = mapped_column(String(100))
    criticality: Mapped[int] = mapped_column(default=1)
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (CheckConstraint("criticality BETWEEN 1 AND 5"),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    login: Mapped[str] = mapped_column(String(100), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(Text)
    pin_hash: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="worker")
    specialty: Mapped[str] = mapped_column(String(100), default="слесарь")
    grade: Mapped[int] = mapped_column(default=3)
    brigade_id: Mapped[str | None] = mapped_column(ForeignKey("brigades.id"), index=True)
    on_shift: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)
    synthetic: Mapped[bool] = mapped_column(default=False)
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[object | None] = mapped_column(UTCDateTime)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (CheckConstraint("role IN ('master','worker','leader','admin')"), CheckConstraint("grade BETWEEN 1 AND 8"))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[object] = mapped_column(UTCDateTime)
    revoked: Mapped[bool] = mapped_column(default=False)


class Shift(Base):
    __tablename__ = "shifts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[object] = mapped_column(UTCDateTime, index=True)
    ends_at: Mapped[object] = mapped_column(UTCDateTime)
    __table_args__ = (CheckConstraint("ends_at > starts_at"),)


class ShiftMember(Base):
    __tablename__ = "shift_members"
    shift_id: Mapped[str] = mapped_column(ForeignKey("shifts.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)


class FaultCode(Base):
    __tablename__ = "fault_codes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    specialty: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(default=True)


class Material(Base):
    __tablename__ = "materials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(30))
    allowed_fault_prefixes: Mapped[list] = mapped_column(JSON, default=list)
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(default=True)


class TimeNorm(Base):
    __tablename__ = "time_norms"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    equipment_type: Mapped[str] = mapped_column(String(100))
    fault_code_id: Mapped[str] = mapped_column(ForeignKey("fault_codes.id"))
    minutes: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("minutes > 0"),)


class MaterialNorm(Base):
    __tablename__ = "material_norms"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    fault_code_id: Mapped[str] = mapped_column(ForeignKey("fault_codes.id"))
    equipment_type: Mapped[str] = mapped_column(String(100))
    material_id: Mapped[str] = mapped_column(ForeignKey("materials.id"))
    quantity: Mapped[object] = mapped_column(Numeric(12, 3))
    __table_args__ = (CheckConstraint("quantity > 0"), UniqueConstraint("fault_code_id", "equipment_type", "material_id"))


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    number: Mapped[int] = mapped_column(Integer, unique=True)
    work_type: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(ForeignKey("equipment.id"), index=True)
    executor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    executor_brigade_id: Mapped[str | None] = mapped_column(ForeignKey("brigades.id"))
    brigade_id: Mapped[str | None] = mapped_column(ForeignKey("brigades.id"), index=True)
    master_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shift_id: Mapped[str | None] = mapped_column(ForeignKey("shifts.id"), index=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    due_at: Mapped[object] = mapped_column(UTCDateTime, index=True)
    norm_minutes: Mapped[int] = mapped_column(default=60)
    status: Mapped[str] = mapped_column(String(20), default="issued", index=True)
    created_at: Mapped[object] = mapped_column(UTCDateTime, default=utcnow, index=True)
    accepted_at: Mapped[object | None] = mapped_column(UTCDateTime)
    started_at: Mapped[object | None] = mapped_column(UTCDateTime)
    submitted_at: Mapped[object | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[object | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[object | None] = mapped_column(UTCDateTime)
    working_executor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    active_executor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), unique=True)
    queue_position: Mapped[int | None] = mapped_column(Integer)
    work_description: Mapped[str | None] = mapped_column(Text)
    fault_code_id: Mapped[str | None] = mapped_column(ForeignKey("fault_codes.id"), index=True)
    materials_none: Mapped[bool] = mapped_column(default=False)
    result_version: Mapped[int] = mapped_column(default=0)
    rework_count: Mapped[int] = mapped_column(default=0)
    master_score: Mapped[int | None] = mapped_column(Integer)
    master_verdict: Mapped[str | None] = mapped_column(String(30))
    master_comment: Mapped[str | None] = mapped_column(Text)
    rejection_justified: Mapped[bool | None] = mapped_column(Boolean)
    synthetic: Mapped[bool] = mapped_column(default=False)
    version: Mapped[int] = mapped_column(default=1)
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (CheckConstraint("work_type IN ('planned','unplanned')"), CheckConstraint("priority IN ('emergency','high','normal','planned')"), CheckConstraint("status IN ('issued','accepted','queued','rejected','in_progress','paused','performed','ai_review','rework','closed','cancelled')"), CheckConstraint("norm_minutes > 0"), CheckConstraint("result_version >= 0 AND rework_count >= 0 AND version >= 1"), CheckConstraint("(executor_id IS NOT NULL AND brigade_id IS NULL) OR (executor_id IS NULL AND brigade_id IS NOT NULL)"), CheckConstraint("master_score IS NULL OR master_score BETWEEN 1 AND 5"))


class OrderCounter(Base):
    __tablename__ = "order_counter"
    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[int] = mapped_column(default=0)


class OrderEvent(Base):
    __tablename__ = "order_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100))
    occurred_at: Mapped[object] = mapped_column(UTCDateTime, default=utcnow, index=True)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class OrderEventRevocation(Base):
    """Address a removal signal to former assignees without retaining access."""
    __tablename__ = "order_event_revocations"
    event_id: Mapped[int] = mapped_column(ForeignKey("order_events.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[object] = mapped_column(UTCDateTime, default=utcnow)


class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    result_version: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    actor_brigade_id: Mapped[str | None] = mapped_column(ForeignKey("brigades.id"))
    work_description: Mapped[str] = mapped_column(Text)
    fault_code_id: Mapped[str | None] = mapped_column(ForeignKey("fault_codes.id"))
    materials_none: Mapped[bool] = mapped_column(default=False)
    comment: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[object] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("order_id", "result_version"),)


class MaterialUsage(Base):
    __tablename__ = "material_usage"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    result_version: Mapped[int] = mapped_column(Integer)
    material_id: Mapped[str] = mapped_column(ForeignKey("materials.id"))
    quantity: Mapped[object] = mapped_column(Numeric(12, 3))
    __table_args__ = (CheckConstraint("quantity > 0"), UniqueConstraint("order_id", "result_version", "material_id"))


class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(300))
    mime_type: Mapped[str] = mapped_column(String(40), default="image/jpeg")
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    phash: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[object] = mapped_column(UTCDateTime, default=utcnow)
    captured_at: Mapped[object | None] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(30), default="upload")
    synthetic: Mapped[bool] = mapped_column(default=False)
    result_version: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("kind IN ('before','after')"),)


class Assessment(Base):
    __tablename__ = "assessments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    input_version: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(30))
    score: Mapped[int | None] = mapped_column(Integer)
    explanation: Mapped[str] = mapped_column(Text)
    good: Mapped[list] = mapped_column(JSON, default=list)
    improve: Mapped[list] = mapped_column(JSON, default=list)
    findings: Mapped[list] = mapped_column(JSON, default=list)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    engine: Mapped[str] = mapped_column(String(30), default="rules")
    model: Mapped[str | None] = mapped_column(String(200))
    needs_master_review: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[object] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("order_id", "input_version"), CheckConstraint("score IS NULL OR score BETWEEN 1 AND 5"))


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[object] = mapped_column(UTCDateTime, default=utcnow)
    locked_at: Mapped[object | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("order_id", "input_version"),)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("work_orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[object] = mapped_column(UTCDateTime, default=utcnow)
    read_at: Mapped[object | None] = mapped_column(UTCDateTime)
    telegram_status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[object] = mapped_column(UTCDateTime, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("user_id", "dedupe_key"),)


class EquipmentDowntime(Base):
    __tablename__ = "equipment_downtimes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    equipment_id: Mapped[str] = mapped_column(ForeignKey("equipment.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    started_at: Mapped[object] = mapped_column(UTCDateTime)
    ended_at: Mapped[object | None] = mapped_column(UTCDateTime)
    cause: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (CheckConstraint("ended_at IS NULL OR ended_at >= started_at"),)


class TelegramLink(Base):
    __tablename__ = "telegram_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[object] = mapped_column(UTCDateTime)
    consumed_at: Mapped[object | None] = mapped_column(UTCDateTime)


class TelegramBotState(Base):
    __tablename__ = "telegram_bot_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    update_offset: Mapped[int] = mapped_column(default=0)
