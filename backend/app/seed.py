"""Explicit, deterministic demo-data loading; never runs on application startup.

The fixture namespace belongs to this loader. Existing unrelated rows must never
be deleted or overwritten. Illustrations and historical reviews are synthetic,
and must not be presented as real repair photographs or live AI results.
"""

from __future__ import annotations

import hashlib
import argparse
import json
import struct
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Assessment, Brigade, Equipment, EquipmentDowntime, FaultCode, Material,
    MaterialNorm, OrderCounter, OrderEvent, Photo, ReviewJob, Section, Shift, ShiftMember,
    Submission, TimeNorm, User, WorkOrder,
)


DEMO_NAMESPACE = uuid.UUID("b7529b88-bc69-4c39-b312-1ac8c03a9404")
DEMO_MARKER = "naradai.synthetic.v1"

SECTIONS = ("Дробление", "Обогащение", "Ремонтно-механический цех", "Энергоуправление")
EQUIPMENT = (
    (0, "Конвейер К-1", "conveyor"), (0, "Конвейер К-2", "conveyor"),
    (0, "Конвейер К-3", "conveyor"), (0, "Дробилка КМД-1750", "crusher"),
    (0, "Дробилка ДЩ-2", "crusher"), (0, "Питатель ПТ-1", "feeder"),
    (1, "Насос Н-1", "pump"), (1, "Насос Н-2", "pump"),
    (1, "Насос Н-3", "pump"), (1, "Насос Н-4", "pump"),
    (1, "Сепаратор С-1", "separator"), (1, "Сепаратор С-2", "separator"),
    (1, "Насос Н-7", "pump"),
    (2, "Токарный станок Т-1", "machine"), (2, "Токарный станок Т-2", "machine"),
    (2, "Фрезерный станок Ф-1", "machine"), (2, "Сварочный аппарат СВ-1", "welder"),
    (2, "Пресс ПР-1", "press"), (2, "Кран КР-1", "crane"),
    (3, "Подстанция ПС-1", "substation"), (3, "Подстанция ПС-2", "substation"),
    (3, "Трансформатор ТР-1", "transformer"), (3, "Компрессор КМ-1", "compressor"),
    (3, "Компрессор КМ-2", "compressor"), (3, "Электродвигатель ЭД-1", "motor"),
)
FAULTS = (
    ("М-01", "Износ уплотнения", "слесарь"), ("М-02", "Повреждение подшипника", "слесарь"),
    ("М-03", "Нарушение крепления", "слесарь"), ("М-04", "Износ ремня", "слесарь"),
    ("Э-01", "Обрыв цепи", "электрик"), ("Э-02", "Неисправность контактора", "электрик"),
    ("Э-03", "Неисправность датчика", "электрик"), ("Э-04", "Нагрев соединения", "электрик"),
    ("Г-01", "Утечка гидравлической жидкости", "слесарь"), ("Г-02", "Снижение давления", "слесарь"),
    ("Г-03", "Износ гидрорукава", "слесарь"), ("Г-04", "Неисправность гидроклапана", "слесарь"),
    ("П-01", "Утечка воздуха", "слесарь"), ("П-02", "Засорение пневмофильтра", "слесарь"),
    ("П-03", "Неисправность пневмоклапана", "слесарь"), ("П-04", "Износ пневморукава", "слесарь"),
    ("С-01", "Недостаток смазки", "слесарь"), ("С-02", "Загрязнение масла", "слесарь"),
    ("С-03", "Засорение фильтра смазки", "слесарь"), ("С-04", "Неисправность маслопровода", "слесарь"),
)
MATERIALS = (
    ("Подшипник 6205", "шт", ["М"]), ("Уплотнение насоса", "шт", ["М", "Г"]),
    ("Болт М12", "шт", ["М"]), ("Гайка М12", "шт", ["М"]),
    ("Ремень привода", "шт", ["М"]), ("Муфта привода", "шт", ["М"]),
    ("Шпонка", "шт", ["М"]), ("Шайба", "шт", ["М"]),
    ("Кабель силовой", "м", ["Э"]), ("Кабель сигнальный", "м", ["Э"]),
    ("Контактор", "шт", ["Э"]), ("Датчик температуры", "шт", ["Э"]),
    ("Предохранитель", "шт", ["Э"]), ("Клемма", "шт", ["Э"]),
    ("Изоляционная лента", "рулон", ["Э"]), ("Кабельный наконечник", "шт", ["Э"]),
    ("Гидравлическое масло", "л", ["Г", "С"]), ("Гидрорукав", "м", ["Г"]),
    ("Гидроклапан", "шт", ["Г"]), ("Гидрофильтр", "шт", ["Г"]),
    ("Манометр гидравлический", "шт", ["Г"]), ("Кольцо уплотнительное", "шт", ["М", "Г"]),
    ("Штуцер", "шт", ["Г", "П"]), ("Прокладка", "шт", ["М", "Г", "П"]),
    ("Пневморукав", "м", ["П"]), ("Пневмофильтр", "шт", ["П"]),
    ("Пневмоклапан", "шт", ["П"]), ("Фитинг", "шт", ["П"]),
    ("Манометр пневматический", "шт", ["П"]), ("Хомут", "шт", ["М", "Г", "П"]),
    ("Смазка универсальная", "кг", ["С", "М"]), ("Масло редукторное", "л", ["С"]),
    ("Фильтр масляный", "шт", ["С"]), ("Маслопровод", "м", ["С"]),
    ("Ветошь", "кг", ["М", "Э", "Г", "П", "С"]),
    ("Очиститель", "л", ["М", "Э", "Г", "П", "С"]),
    ("Электроды", "кг", ["М"]), ("Проволока сварочная", "кг", ["М"]),
    ("Абразивный круг", "шт", ["М"]), ("Защитный кожух", "шт", ["М"]),
)


class SeedConflict(ValueError):
    """Stop the whole load when an ID or unique value belongs to another row."""


def _ensure(session: Session, model, fields: dict, identity: tuple[str, ...]):
    existing = session.get(model, fields["id"])
    if existing is not None:
        if any(getattr(existing, key) != fields[key] for key in identity):
            raise SeedConflict(f"ID демонстрационной записи конфликтует: {model.__name__}")
        return existing, False
    for name in identity:
        if name == "synthetic":
            continue
        value = fields.get(name)
        if value is not None and session.scalar(select(model).where(getattr(model, name) == value)) is not None:
            raise SeedConflict(f"Занято уникальное значение: {model.__name__}.{name}")
    row = model(**fields)
    session.add(row)
    session.flush()
    return row, True


def _photo(session: Session, order: WorkOrder, upload_dir: Path, kind: str, at: datetime, created_files: list[Path]):
    from PIL import Image
    from io import BytesIO
    from .services.photos import dhash

    photo_id = demo_id("photo", f"{order.id}:{kind}:1")
    content = synthetic_png(photo_id)
    path = upload_dir.resolve() / f"{photo_id}.png"
    if path.exists():
        if path.read_bytes() != content:
            raise SeedConflict("Файл демонстрационного фото уже существует с другим содержимым")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
        created_files.append(path)
    with Image.open(BytesIO(content)) as image:
        phash = dhash(image)
    session.add(Photo(
        id=photo_id, order_id=order.id, author_id=order.master_id if kind == "before" else order.executor_id,
        kind=kind, path=path.name, mime_type="image/png", sha256=hashlib.sha256(content).hexdigest(),
        phash=phash, uploaded_at=at, captured_at=None, source="seed", synthetic=True,
        result_version=1 if kind == "after" else None,
    ))


def _event(session: Session, order: WorkOrder, action: str, at: datetime, old: str | None, new: str,
           actor: str | None, reason: str | None = None, pattern: str = "baseline"):
    details = {"synthetic": True, "seed": DEMO_MARKER, "pattern": pattern,
               **_original_actor_snapshot(actor), "seed_snapshot_source": "original_roster_v1"}
    if action == "issued":
        details.update(executor_id=order.executor_id, executor_brigade_id=order.executor_brigade_id,
                       brigade_id=order.brigade_id, due_at=order.due_at.isoformat(),
                       norm_minutes=order.norm_minutes, priority=order.priority)
    session.add(OrderEvent(order_id=order.id, actor_id=actor, action=action, occurred_at=at,
                           from_status=old, to_status=new, reason=reason,
                           details=details))


def _add_order(session: Session, *, key: str, number: int, equipment: Equipment, worker: User,
               master: User, shift: Shift, fault: FaultCode, at: datetime, upload_dir: Path,
               created_files: list[Path], work_type: str = "planned", pattern: str = "baseline",
               material: Material | None = None, quantity: Decimal = Decimal("1"),
               wait: bool = False, target: str = "closed", score: int = 4):
    from .models import MaterialUsage

    order_id = demo_id("order", key)
    existing = session.get(WorkOrder, order_id)
    if existing is not None:
        if not existing.synthetic or existing.comment is None or DEMO_MARKER not in existing.comment:
            raise SeedConflict("ID демонстрационного наряда занят посторонними данными")
        expected_result = target in {"closed", "rework", "ai_review", "performed"}
        expected_description = f"[Демо] {fault.name}: {equipment.name}"
        expected_work = f"[Демо] Выполнена работа по шифру {fault.code}; проверена видимая часть узла." if expected_result else None
        # Version 1 and the known fixture signature identify untouched originals.
        # Never derive past affiliation from today's mutable employee record.
        if (existing.version == 1 and existing.status == target
                and existing.description == expected_description and existing.work_description == expected_work
                and existing.comment == f"{DEMO_MARKER}; pattern={pattern}; синтетическая история, не производственные данные"
                and existing.equipment_id == equipment.id and existing.section_id == equipment.section_id
                and existing.executor_id == worker.id and existing.brigade_id is None
                and existing.executor_brigade_id in {None, _original_actor_snapshot(worker.id)["actor_brigade_id"]}
                and existing.master_id == master.id and existing.fault_code_id == fault.id
                and existing.work_type == work_type and existing.norm_minutes == 60
                and existing.due_at == existing.created_at + timedelta(minutes=90)
                and existing.priority == ("planned" if work_type == "planned" else "emergency" if target == "issued" else "high")
                and existing.result_version == (1 if expected_result else 0)
                and existing.rework_count == (1 if target == "rework" else 0)
                and existing.master_score == (score if target == "closed" else 2 if target == "rework" else None)):
            _backfill_original_snapshots(session, existing, worker.id, pattern)
        return existing, False
    accepted = at + timedelta(minutes=2)
    started = accepted + timedelta(minutes=3)
    submitted = started + timedelta(minutes=45)
    if wait:
        submitted += timedelta(hours=10)
    review_at = submitted + timedelta(seconds=5)
    closed = review_at + timedelta(minutes=5)
    done = target in {"closed", "rework", "ai_review", "performed"}
    started_state = target in {"in_progress", "paused"} or done
    accepted_state = target in {"accepted", "in_progress", "paused"} or done
    priority = "planned" if work_type == "planned" else ("emergency" if target == "issued" else "high")
    norm = 60
    order = WorkOrder(
        id=order_id, number=number, work_type=work_type,
        description=f"[Демо] {fault.name}: {equipment.name}",
        comment=f"{DEMO_MARKER}; pattern={pattern}; синтетическая история, не производственные данные",
        section_id=equipment.section_id, equipment_id=equipment.id, executor_id=worker.id,
        executor_brigade_id=_original_actor_snapshot(worker.id)["actor_brigade_id"],
        brigade_id=None, master_id=master.id, shift_id=shift.id, priority=priority,
        due_at=at + timedelta(minutes=90), norm_minutes=norm, status=target, created_at=at,
        accepted_at=accepted if accepted_state else None, started_at=started if started_state else None,
        submitted_at=submitted if done else None, closed_at=closed if target == "closed" else None,
        working_executor_id=worker.id if started_state else None,
        active_executor_id=worker.id if target == "in_progress" else None,
        queue_position=1 if target == "queued" else None,
        work_description=f"[Демо] Выполнена работа по шифру {fault.code}; проверена видимая часть узла." if done else None,
        fault_code_id=fault.id, materials_none=material is None if done else False,
        result_version=1 if done else 0, rework_count=1 if target == "rework" else 0,
        master_score=score if target == "closed" else (2 if target == "rework" else None),
        master_verdict="accepted" if target == "closed" else ("requires_rework" if target == "rework" else None),
        master_comment="Синтетическая приёмка для демонстрации отчётов" if target == "closed" else None,
        rejection_justified=True if target == "rejected" else None, synthetic=True,
    )
    session.add(order)
    session.flush()
    _event(session, order, "issued", at, None, "issued", master.id, pattern=pattern)
    if target in {"queued", "rejected"}:
        reason = "Нет материалов; демонстрационный обоснованный отказ" if target == "rejected" else None
        _event(session, order, "rejected" if target == "rejected" else "queued", accepted,
               "issued", target, worker.id, reason, pattern)
    if accepted_state:
        _event(session, order, "accepted", accepted, "issued", "accepted", worker.id, pattern=pattern)
    if started_state:
        _event(session, order, "started", started, "accepted", "in_progress", worker.id, pattern=pattern)
    if wait or target == "paused":
        paused_at = started + timedelta(minutes=20)
        _event(session, order, "paused", paused_at, "in_progress", "paused", worker.id,
               "Ожидание подшипника со склада", pattern)
        if wait:
            _event(session, order, "resumed", paused_at + timedelta(hours=10), "paused", "in_progress", worker.id, pattern=pattern)
    if work_type == "unplanned":
        _photo(session, order, upload_dir, "before", at, created_files)
    if done:
        session.add(Submission(
            id=demo_id("submission", order.id), order_id=order.id, result_version=1,
            actor_id=worker.id, work_description=order.work_description, fault_code_id=fault.id,
            actor_brigade_id=_original_actor_snapshot(worker.id)["actor_brigade_id"],
            materials_none=material is None, comment="Синтетический результат", submitted_at=submitted,
        ))
        if material is not None:
            session.add(MaterialUsage(id=demo_id("usage", order.id), order_id=order.id,
                                      result_version=1, material_id=material.id, quantity=quantity))
        _photo(session, order, upload_dir, "after", submitted, created_files)
        _event(session, order, "performed", submitted, "in_progress", "performed", worker.id, pattern=pattern)
        if target != "performed":
            _event(session, order, "review_requested", review_at, "performed", "ai_review", None, pattern=pattern)
        if target in {"closed", "rework"}:
            verdict = "requires_rework" if target == "rework" else "accepted"
            session.add(Assessment(
                id=demo_id("assessment", order.id), order_id=order.id, input_version=1,
                verdict=verdict, score=2 if target == "rework" else score,
                explanation="Синтетическая историческая оценка; модель не вызывалась.",
                good=["Демонстрационный результат сохранён"],
                improve=["Нужна доработка демонстрационного результата"] if target == "rework" else [],
                findings=[], details={"synthetic": True, "seed": DEMO_MARKER, "pattern": pattern},
                engine="seed", model=None, needs_master_review=True,
                created_at=review_at + timedelta(seconds=1),
            ))
            _event(session, order, "rework" if target == "rework" else "closed", closed, "ai_review", target, master.id,
                   "Историческая синтетическая приёмка" if target == "closed" else "Уточнить описание результата",
                   pattern)
        if target == "ai_review":
            session.add(ReviewJob(id=demo_id("review_job", order.id), order_id=order.id, input_version=1,
                                  state="pending", attempts=0, next_attempt_at=review_at, last_error=None))
    if work_type == "unplanned":
        session.add(EquipmentDowntime(
            id=demo_id("downtime", order.id), equipment_id=equipment.id, order_id=order.id,
            started_at=at, ended_at=submitted if done else None,
            cause="[Демо] Ожидание запчастей" if wait else f"[Демо] {fault.name}", actor_id=master.id,
        ))
    session.flush()
    return order, True


def seed_demo(session: Session, *, password: str, upload_dir: str | Path,
              anchor: datetime | None = None) -> dict:
    """Load one namespace atomically, preserving every existing row.

    Existing demo records are not refreshed, including passwords and changes
    made through the app. A persisted current shift fixes the first run's anchor.
    Schema creation/migration is intentionally outside this function.
    """
    from .security import hash_password

    if not password or len(password) < 8:
        raise ValueError("DEMO_PASSWORD должен содержать минимум 8 символов")
    if session.in_transaction():
        raise ValueError("Загрузчик требует новую сессию без активной транзакции")
    anchor = anchor or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        raise ValueError("Дата демонстрации должна содержать часовой пояс")
    anchor = anchor.astimezone(timezone.utc)
    upload_dir = Path(upload_dir)
    created_files: list[Path] = []
    added_orders = 0
    try:
        with session.begin():
            current = session.get(Shift, demo_id("shift", "current"))
            if current is not None:
                if current.name != f"[Демо] Текущая смена {DEMO_MARKER}":
                    raise SeedConflict("ID текущей демонстрационной смены занят")
                anchor = current.starts_at + timedelta(hours=12)
            day = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
            sections = []
            for index, name in enumerate(SECTIONS):
                row, _ = _ensure(session, Section, dict(id=demo_id("section", index), name=f"[Демо] {name}",
                                                       external_id=f"demo:section:{index}", active=True), ("external_id", "name"))
                sections.append(row)
            brigades = []
            for index in range(3):
                row, _ = _ensure(session, Brigade, dict(id=demo_id("brigade", index), name=f"[Демо] Бригада {index + 1}", active=True), ("name",))
                brigades.append(row)
            equipment = []
            for index, (section, name, kind) in enumerate(EQUIPMENT):
                row, _ = _ensure(session, Equipment, dict(
                    id=demo_id("equipment", index), name=f"[Демо] {name}", inventory_number=f"DEMO-{index + 1:03d}",
                    section_id=sections[section].id, equipment_type=kind, criticality=5 if index in {2, 12} else 2,
                    external_id=f"demo:equipment:{index}", active=True), ("external_id", "inventory_number"))
                equipment.append(row)
            users = []
            specs = [(f"demo.master{i + 1}", f"Мастер {i + 1}", "master", "мастер", None, True) for i in range(2)]
            specs += [(f"demo.worker{i + 1:02d}", f"Исполнитель {i + 1:02d}", "worker", "электрик" if i >= 10 else "слесарь",
                       brigades[i // 5].id, i < 12) for i in range(15)]
            specs += [("demo.leader", "Руководитель", "leader", "руководитель", None, True),
                      ("demo.admin", "Администратор", "admin", "администратор", None, False)]
            for index, (login, name, role, specialty, brigade_id, on_shift) in enumerate(specs):
                existing = session.get(User, demo_id("user", index))
                fields = dict(id=demo_id("user", index), login=login, full_name=f"[Демо] {name}",
                              password_hash=existing.password_hash if existing is not None else hash_password(password),
                              role=role, specialty=specialty, grade=3 + index % 3, brigade_id=brigade_id,
                              on_shift=on_shift, active=True, synthetic=True)
                row, _ = _ensure(session, User, fields, ("login", "synthetic"))
                users.append(row)
            masters, workers = users[:2], users[2:17]
            faults = []
            for index, (code, name, specialty) in enumerate(FAULTS):
                row, _ = _ensure(session, FaultCode, dict(id=demo_id("fault", index), code=code,
                                                         name=f"[Демо] {name}", specialty=specialty, active=True), ("code", "name"))
                faults.append(row)
            materials = []
            for index, (name, unit, allowed) in enumerate(MATERIALS):
                row, _ = _ensure(session, Material, dict(id=demo_id("material", index), name=f"[Демо] {name}",
                                                       unit=unit, allowed_fault_prefixes=allowed,
                                                       external_id=f"demo:material:{index}", active=True), ("external_id",))
                materials.append(row)
            # Namespaced identifiers coexist with production norms and never alter them.
            for kind in sorted({item.equipment_type for item in equipment}):
                for fault_index in (0, 1, 17):
                    fields = dict(id=demo_id("time_norm", f"{kind}:{fault_index}"), name=f"[Демо] {faults[fault_index].name} / {kind}",
                                  equipment_type=kind, fault_code_id=faults[fault_index].id, minutes=60)
                    existing = session.get(TimeNorm, fields["id"])
                    if existing is None:
                        session.add(TimeNorm(**fields))
                    elif existing.name != fields["name"]:
                        raise SeedConflict("ID норматива времени занят")
            session.flush()
            for kind in sorted({item.equipment_type for item in equipment}):
                fields = dict(id=demo_id("material_norm", kind), fault_code_id=faults[1].id,
                              equipment_type=kind, material_id=materials[0].id, quantity=Decimal("2"))
                existing = session.get(MaterialNorm, fields["id"])
                if existing is None:
                    conflict = session.scalar(select(MaterialNorm).where(
                        MaterialNorm.fault_code_id == fields["fault_code_id"], MaterialNorm.equipment_type == kind,
                        MaterialNorm.material_id == materials[0].id))
                    if conflict is not None:
                        raise SeedConflict("Норматив материала уже существует вне demo namespace")
                    session.add(MaterialNorm(**fields))
                elif (existing.fault_code_id, existing.equipment_type, existing.material_id) != (fields["fault_code_id"], kind, materials[0].id):
                    raise SeedConflict("ID норматива материала занят")
            session.flush()
            shifts = []
            for offset in range(91):
                starts = day - timedelta(days=90 - offset)
                key = "current" if offset == 90 else offset
                name = f"[Демо] Текущая смена {DEMO_MARKER}" if offset == 90 else f"[Демо] Смена {offset + 1} {DEMO_MARKER}"
                shift_id = demo_id("shift", key)
                shift = session.get(Shift, shift_id)
                if shift is None:
                    shift = Shift(id=shift_id, name=name, starts_at=starts, ends_at=starts + timedelta(days=1))
                    session.add(shift)
                    session.flush()
                elif shift.name != name:
                    raise SeedConflict("ID демонстрационной смены занят")
                shifts.append(shift)
                for worker in workers:
                    if offset == 90 and not worker.on_shift:
                        continue
                    if session.get(ShiftMember, (shift.id, worker.id)) is None:
                        session.add(ShiftMember(shift_id=shift.id, user_id=worker.id))
            session.flush()
            counter = session.scalar(select(OrderCounter).where(OrderCounter.id == 1).with_for_update())
            highest = session.scalar(select(func.max(WorkOrder.number))) or 0
            if counter is None:
                counter = OrderCounter(id=1, value=highest)
                session.add(counter)
                session.flush()
            else:
                counter.value = max(counter.value, highest)

            def add(key: str, eq_index: int, worker_index: int, fault_index: int, at: datetime,
                    shift_index: int, **options):
                nonlocal added_orders
                _, created = _add_order(session, key=key, number=counter.value + 1, equipment=equipment[eq_index],
                                        worker=workers[worker_index], master=masters[worker_index % 2], shift=shifts[shift_index],
                                        fault=faults[fault_index], at=at, upload_dir=upload_dir,
                                        created_files=created_files, **options)
                if created:
                    counter.value += 1
                    added_orders += 1

            # Six orders per day; unique workers at each daily baseline block.
            for index in range(540):
                shift_index, slot = divmod(index, 6)
                worker_index = index % 15
                electrical = worker_index >= 10
                fault_index = 4 + index % 4 if electrical else (index % 4 if index % 3 == 0 else 16 + index % 4)
                eq_index = 19 + index % 6 if electrical else index % 19
                material_index = 12 if electrical else (0 if fault_index == 1 else 34)
                add(f"baseline:{index}", eq_index, worker_index, fault_index,
                    shifts[shift_index].starts_at + timedelta(hours=8 + slot), shift_index,
                    work_type="unplanned" if index % 3 == 0 else "planned", material=materials[material_index],
                    quantity=Decimal("2") if fault_index == 1 else Decimal("1"), score=3 + index % 3)
            # Four explicit patterns with stable IDs and evidence tags.
            for index in range(10):
                shift_index = 48 + index * 3
                add(f"repeat_bearing:{index}", 2, 0, 1, shifts[shift_index].starts_at + timedelta(hours=18), shift_index,
                    work_type="unplanned", pattern="repeat_bearing", material=materials[0], quantity=Decimal("2"))
            for index in range(6):
                planned_day = 10 + index * 10
                add(f"after_pm:planned:{index}", 12, 1, 0, shifts[planned_day].starts_at + timedelta(hours=18), planned_day,
                    pattern="after_pm_planned", material=materials[1])
                add(f"after_pm:failure:{index}", 12, 1, 0, shifts[planned_day + 1].starts_at + timedelta(hours=18), planned_day + 1,
                    work_type="unplanned", pattern="after_pm_failure", material=materials[1])
            for index in range(6):
                shift_index = 17 + index * 10
                add(f"material_excess:{index}", 1, 2, 1, shifts[shift_index].starts_at + timedelta(hours=18), shift_index,
                    work_type="unplanned", pattern="material_excess", material=materials[0], quantity=Decimal("8"), score=3)
            for index in range(6):
                shift_index = 6 + index * 10
                add(f"parts_wait:{index}", 6, 3, 1, shifts[shift_index].starts_at + timedelta(hours=18), shift_index,
                    work_type="unplanned", pattern="parts_wait", material=materials[0], quantity=Decimal("2"), wait=True)
            # Current work shows the ordinary lifecycle without pretending a provider ran.
            current_at = anchor - timedelta(hours=2)
            live = (("issued", 4), ("accepted", 5), ("in_progress", 6), ("queued", 6),
                    ("paused", 7), ("rejected", 8), ("rework", 9), ("ai_review", 10))
            for index, (target, worker_index) in enumerate(live):
                add(f"current:{target}", index, worker_index, 4 if worker_index >= 10 else 0,
                    current_at + timedelta(minutes=index), 90, work_type="unplanned", pattern="current",
                    material=materials[12] if worker_index >= 10 else materials[1], target=target)
            session.flush()
            report = {
                "namespace": DEMO_MARKER, "anchor_date": day.date().isoformat(), "orders_added": added_orders,
                "demo_orders": session.scalar(select(func.count()).select_from(WorkOrder).where(WorkOrder.synthetic.is_(True), WorkOrder.comment.contains(DEMO_MARKER))),
                "sections": 4, "equipment": 25, "masters": 2, "workers": 15, "brigades": 3,
                "fault_codes": 20, "materials": 40, "history_days": 90,
                "patterns": {"repeat_bearing": 10, "after_pm_pairs": 6, "material_excess": 6, "parts_wait": 6},
                "historical_assessment_engine": "seed", "images": "synthetic abstract illustrations",
            }
        return report
    except Exception:
        # Remove only files exclusively created by this failed invocation.
        for path in created_files:
            path.unlink(missing_ok=True)
        raise


def main() -> None:
    from .config import get_settings
    from .db import SessionLocal

    parser = argparse.ArgumentParser(description="Явная загрузка синтетики; существующие записи не меняются")
    parser.add_argument("--anchor", help="Дата/время ISO 8601 с часовым поясом; сохраняется первый anchor")
    args = parser.parse_args()
    anchor = datetime.fromisoformat(args.anchor.replace("Z", "+00:00")) if args.anchor else None
    settings = get_settings()
    with SessionLocal() as session:
        result = seed_demo(session, password=settings.demo_password, upload_dir=settings.upload_dir, anchor=anchor)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Только демонстрационные аккаунты: demo.master1/2, demo.worker01..15, demo.leader, demo.admin.")
    print("Пароль взят из DEMO_PASSWORD. Существующие demo-пароли повторным запуском не меняются.")
    print("Оценки engine=seed и изображения synthetic не являются живым ИИ или реальными фото.")


def demo_id(kind: str, key: str | int) -> str:
    """Stable IDs avoid duplicate entities on every explicit seed invocation."""
    return str(uuid.uuid5(DEMO_NAMESPACE, f"{kind}:{key}"))


def _original_actor_snapshot(actor_id: str | None) -> dict:
    """Original fixture roster, independent of later User changes."""
    if actor_id is None:
        return {"actor_role": None, "actor_brigade_id": None}
    for index in range(19):
        if actor_id != demo_id("user", index):
            continue
        if index < 2:
            return {"actor_role": "master", "actor_brigade_id": None}
        if index < 17:
            return {"actor_role": "worker", "actor_brigade_id": demo_id("brigade", (index - 2) // 5)}
        return {"actor_role": "leader" if index == 17 else "admin", "actor_brigade_id": None}
    raise SeedConflict("Неизвестный участник первоначального демонстрационного набора")


def _backfill_original_snapshots(session: Session, order: WorkOrder, worker_id: str, pattern: str) -> None:
    """Fill missing historical metadata only on verified untouched demo rows.

    Existing non-null snapshots, edited orders, foreign records and legacy real
    records are not changed. No current user field is a historical data source.
    """
    original_brigade = _original_actor_snapshot(worker_id)["actor_brigade_id"]
    submission = session.get(Submission, demo_id("submission", order.id))
    events = list(session.scalars(select(OrderEvent).where(OrderEvent.order_id == order.id)))
    if order.executor_brigade_id is None:
        order.executor_brigade_id = original_brigade
    if (submission is not None and submission.actor_brigade_id is None
            and submission.actor_id == worker_id and submission.result_version == 1
            and submission.work_description == order.work_description
            and submission.fault_code_id == order.fault_code_id
            and submission.materials_none == order.materials_none
            and submission.submitted_at == order.submitted_at
            and submission.comment == "Синтетический результат"):
        submission.actor_brigade_id = original_brigade
    transitions = {
        "issued": (None, "issued", order.master_id, None),
        "accepted": ("issued", "accepted", worker_id, None),
        "started": ("accepted", "in_progress", worker_id, None),
        "queued": ("issued", "queued", worker_id, None),
        "rejected": ("issued", "rejected", worker_id, "Нет материалов; демонстрационный обоснованный отказ"),
        "paused": ("in_progress", "paused", worker_id, "Ожидание подшипника со склада"),
        "resumed": ("paused", "in_progress", worker_id, None),
        "performed": ("in_progress", "performed", worker_id, None),
        "review_requested": ("performed", "ai_review", None, None),
        "closed": ("ai_review", "closed", order.master_id, "Историческая синтетическая приёмка"),
        "rework": ("ai_review", "rework", order.master_id, "Уточнить описание результата"),
    }
    allowed_keys = {"synthetic", "seed", "pattern", "actor_role", "actor_brigade_id", "seed_snapshot_source",
                    "executor_id", "executor_brigade_id", "brigade_id", "due_at", "norm_minutes", "priority"}
    for event in events:
        signature = transitions.get(event.action)
        if signature != (event.from_status, event.to_status, event.actor_id, event.reason):
            continue
        details = dict(event.details or {})
        if (details.get("synthetic") is not True or details.get("seed") != DEMO_MARKER
                or details.get("pattern") != pattern or set(details) - allowed_keys):
            continue
        original = _original_actor_snapshot(event.actor_id)
        if any(name in details and details[name] is not None and details[name] != value for name, value in original.items()):
            continue
        if details.get("seed_snapshot_source") not in {None, "original_roster_v1"}:
            continue
        original_assignment = {"executor_id": worker_id, "executor_brigade_id": original_brigade,
                               "brigade_id": None, "due_at": order.due_at.isoformat(),
                               "norm_minutes": order.norm_minutes, "priority": order.priority}
        if event.action == "issued" and any(name in details and details[name] is not None and details[name] != value
                                            for name, value in original_assignment.items()):
            continue
        for name, value in original.items():
            if name not in details or details[name] is None:
                details[name] = value
        if event.action == "issued":
            for name, value in original_assignment.items():
                if name not in details or details[name] is None:
                    details[name] = value
        if details != event.details:
            details["seed_snapshot_source"] = "original_roster_v1"
            event.details = details


def synthetic_png(key: str, width: int = 96, height: int = 64) -> bytes:
    """Make a deterministic abstract PNG fixture, never a purported site photo.

    Each key affects many pixels so the fixtures are distinguishable by file and
    image hash. No image library or network request is needed for demo loading.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            block = (x // 8 + (y // 8) * 12) % len(digest)
            raw.extend((digest[block], digest[(block + 7) % 32], 128 + (x + y) % 128))

    def chunk(name: bytes, value: bytes) -> bytes:
        checksum = zlib.crc32(name + value) & 0xFFFFFFFF
        return struct.pack("!I", len(value)) + name + value + struct.pack("!I", checksum)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"tEXt", b"Description\x00SYNTHETIC DEMO ILLUSTRATION - NOT A REAL REPAIR PHOTO")
        + chunk(b"IDAT", zlib.compress(bytes(raw), level=6))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    main()
