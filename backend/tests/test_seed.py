from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Base, make_engine
from app.models import (
    Assessment, Brigade, Equipment, EquipmentDowntime, FaultCode, Material,
    MaterialNorm, MaterialUsage, OrderCounter, OrderEvent, Photo, Section,
    Shift, Submission, User, WorkOrder,
)
from app.seed import DEMO_MARKER, SeedConflict, demo_id, seed_demo


ANCHOR = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
PASSWORD = "Demo-Seed-Test-2026!"


@pytest.fixture
def database(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'seed-test.sqlite'}")
    Base.metadata.create_all(engine)
    yield engine, tmp_path / "photos"
    engine.dispose()


def load(database, **kwargs):
    engine, photos = database
    with Session(engine) as session:
        return seed_demo(session, password=kwargs.pop("password", PASSWORD), upload_dir=photos,
                         anchor=kwargs.pop("anchor", ANCHOR), **kwargs)


def table_counts(session):
    return {table.name: session.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables}


def test_seed_meets_case_sizes_and_has_valid_evidence_and_patterns(database):
    result = load(database)
    engine, photo_dir = database
    assert result["orders_added"] >= 500
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Section)) == 4
        assert session.scalar(select(func.count()).select_from(Equipment)) == 25
        assert session.scalar(select(func.count()).select_from(Brigade)) == 3
        assert session.scalar(select(func.count()).select_from(FaultCode)) == 20
        assert session.scalar(select(func.count()).select_from(Material)) == 40
        users = list(session.scalars(select(User)))
        assert Counter(user.role for user in users) == {"master": 2, "worker": 15, "leader": 1, "admin": 1}
        brigade_sizes = Counter(user.brigade_id for user in users if user.role == "worker")
        assert len(brigade_sizes) == 3 and set(brigade_sizes.values()) == {5}
        assert all(user.synthetic for user in users)
        equipment = {row.id: row for row in session.scalars(select(Equipment))}
        faults = {row.id: row for row in session.scalars(select(FaultCode))}
        orders = list(session.scalars(select(WorkOrder)))
        history = [order for order in orders if order.status == "closed"]
        assert len(history) >= 500
        assert len({order.created_at.date() for order in history}) == 90
        history_start = ANCHOR.replace(hour=0) - timedelta(days=90)
        assert all(history_start <= order.created_at < ANCHOR for order in history)
        assert all(order.synthetic and DEMO_MARKER in order.comment for order in orders)
        assert len({order.number for order in orders}) == len(orders)
        events_by_order = defaultdict(list)
        for event in session.scalars(select(OrderEvent).order_by(OrderEvent.occurred_at, OrderEvent.id)):
            events_by_order[event.order_id].append(event)
            assert event.details["synthetic"] is True
        photos_by_order = defaultdict(list)
        for photo in session.scalars(select(Photo)):
            photos_by_order[photo.order_id].append(photo)
            path = photo_dir / photo.path
            assert path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == photo.sha256
            assert photo.synthetic and photo.source == "seed" and photo.captured_at is None
            assert "SYNTHETIC DEMO ILLUSTRATION" in path.read_bytes().decode("latin1")
        submissions = {(item.order_id, item.result_version) for item in session.scalars(select(Submission))}
        for order in orders:
            assert order.section_id == equipment[order.equipment_id].section_id
            events = events_by_order[order.id]
            assert events[0].from_status is None and events[0].to_status == "issued"
            assert events[-1].to_status == order.status
            assert all(first.to_status == second.from_status for first, second in zip(events, events[1:]))
            assert all(first.occurred_at <= second.occurred_at for first, second in zip(events, events[1:]))
            assert events[0].actor_id == order.master_id
            if order.status == "closed":
                assert order.created_at < order.accepted_at < order.started_at < order.submitted_at < order.closed_at <= ANCHOR
                assert (order.id, order.result_version) in submissions
                assert order.work_description and order.fault_code_id and 1 <= order.master_score <= 5
                assert events[-1].actor_id == order.master_id
                if order.work_type == "unplanned":
                    assert any(photo.kind == "after" and photo.result_version == order.result_version for photo in photos_by_order[order.id])
            for event in events:
                if event.to_status in {"paused", "rejected", "rework"}:
                    assert event.reason
        assert all(usage.quantity > 0 for usage in session.scalars(select(MaterialUsage)))
        assessments = list(session.scalars(select(Assessment)))
        assert len(assessments) >= 500
        assert all(item.engine == "seed" and item.model is None and item.details["synthetic"] is True
                   and 1 <= item.score <= 5 for item in assessments)
        patterns = defaultdict(list)
        for order in orders:
            pattern = order.comment.split("pattern=", 1)[1].split(";", 1)[0]
            patterns[pattern].append(order)
        bearing = sorted(patterns["repeat_bearing"], key=lambda item: item.created_at)
        assert len(bearing) >= 3
        assert len({item.equipment_id for item in bearing}) == 1
        assert all(faults[item.fault_code_id].code == "М-02" for item in bearing)
        assert all(second.created_at - first.closed_at < timedelta(days=7) for first, second in zip(bearing, bearing[1:]))
        for failure in patterns["after_pm_failure"]:
            prior = [item for item in patterns["after_pm_planned"]
                     if item.equipment_id == failure.equipment_id and item.closed_at < failure.created_at]
            assert prior and failure.created_at - max(prior, key=lambda item: item.closed_at).closed_at < timedelta(days=2)
        for order in patterns["material_excess"]:
            usage = session.scalar(select(MaterialUsage).where(MaterialUsage.order_id == order.id))
            norm = session.scalar(select(MaterialNorm).where(MaterialNorm.fault_code_id == order.fault_code_id,
                                  MaterialNorm.equipment_type == equipment[order.equipment_id].equipment_type,
                                  MaterialNorm.material_id == usage.material_id))
            assert norm and usage.quantity > norm.quantity * 2
        downtimes = {item.order_id: item for item in session.scalars(select(EquipmentDowntime))}
        for order in patterns["parts_wait"]:
            pauses = [event for event in events_by_order[order.id] if event.to_status == "paused"]
            resumes = [event for event in events_by_order[order.id] if event.from_status == "paused"]
            assert pauses and resumes and resumes[0].occurred_at - pauses[0].occurred_at >= timedelta(hours=10)
            downtime = downtimes[order.id]
            assert downtime.ended_at - downtime.started_at > timedelta(hours=10)
        assert session.get(OrderCounter, 1).value >= max(order.number for order in orders)


def test_repeat_keeps_ids_passwords_user_changes_and_unrelated_rows(database):
    engine, photo_dir = database
    outsider_id = str(uuid4())
    with Session(engine) as session:
        session.add(User(id=outsider_id, login="existing.worker", full_name="Existing unrelated record",
                         password_hash="existing-password-hash", role="worker", synthetic=False))
        session.add(OrderCounter(id=1, value=9000))
        session.commit()
    first = load(database)
    with Session(engine) as session:
        worker = session.get(User, demo_id("user", 2))
        password_hash = worker.password_hash
        worker.full_name = "Изменено пользователем после первой загрузки"
        order = session.get(WorkOrder, demo_id("order", "baseline:0"))
        order.work_description = "Редактирование результата не должно исчезнуть при повторном seed"
        session.commit()
        counts = table_counts(session)
        identifiers = set(session.scalars(select(WorkOrder.id)))
        assert min(session.scalars(select(WorkOrder.number))) > 9000
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in photo_dir.iterdir()}
    second = load(database, password="A-Different-New-Demo-Password!", anchor=ANCHOR + timedelta(days=5))
    assert first["orders_added"] >= 500 and second["orders_added"] == 0
    assert first["anchor_date"] == second["anchor_date"]
    with Session(engine) as session:
        assert table_counts(session) == counts
        assert set(session.scalars(select(WorkOrder.id))) == identifiers
        worker = session.get(User, demo_id("user", 2))
        assert worker.password_hash == password_hash
        assert worker.full_name == "Изменено пользователем после первой загрузки"
        assert session.get(WorkOrder, demo_id("order", "baseline:0")).work_description.startswith("Редактирование")
        outsider = session.get(User, outsider_id)
        assert outsider.full_name == "Existing unrelated record" and outsider.password_hash == "existing-password-hash"
        assert not outsider.synthetic
    assert files == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in photo_dir.iterdir()}


@pytest.mark.parametrize("collision", ["stable_id", "natural_key"])
def test_conflict_stops_atomically_without_touching_other_data(database, collision):
    engine, photo_dir = database
    with Session(engine) as session:
        if collision == "stable_id":
            existing = User(id=demo_id("user", 0), login="unrelated.user", full_name="Keep this user",
                            password_hash="unchanged", role="worker", synthetic=False)
        else:
            existing = FaultCode(id=str(uuid4()), code="М-01", name="Existing real fault", specialty="слесарь")
        session.add(existing)
        session.commit()
        existing_id = existing.id
        existing_type = type(existing)
    with pytest.raises(SeedConflict):
        load(database)
    with Session(engine) as session:
        assert session.get(existing_type, existing_id) is not None
        assert session.scalar(select(func.count()).select_from(Section)) == 0
        assert session.scalar(select(func.count()).select_from(WorkOrder)) == 0
        if collision == "stable_id":
            assert session.get(User, existing_id).password_hash == "unchanged"
        else:
            assert session.get(FaultCode, existing_id).name == "Existing real fault"
    assert not photo_dir.exists()


def test_photo_collision_rolls_back_database_and_keeps_existing_file(database):
    engine, photo_dir = database
    photo_dir.mkdir()
    photo_id = demo_id("photo", f"{demo_id('order', 'baseline:0')}:before:1")
    path = photo_dir / f"{photo_id}.png"
    path.write_bytes(b"existing unrelated file")
    with pytest.raises(SeedConflict):
        load(database)
    assert path.read_bytes() == b"existing unrelated file"
    assert list(photo_dir.iterdir()) == [path]
    with Session(engine) as session:
        assert all(count == 0 for count in table_counts(session).values())


def test_actual_module_cli_loads_without_import_order_error(database):
    engine, photo_dir = database
    environment = dict(os.environ)
    environment.update(DATABASE_URL=str(engine.url), UPLOAD_DIR=str(photo_dir), DEMO_PASSWORD=PASSWORD,
                       PYTHONIOENCODING="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "app.seed", "--anchor", ANCHOR.isoformat()],
        cwd=Path(__file__).resolve().parents[1], env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert completed.returncode == 0, completed.stderr
    result, _ = json.JSONDecoder().raw_decode(completed.stdout)
    assert result["orders_added"] >= 500
    assert result["historical_assessment_engine"] == "seed"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(WorkOrder)) == result["orders_added"]


def test_snapshot_upgrade_uses_original_roster_and_preserves_edits_and_real_nulls(database):
    load(database)
    engine, _ = database
    original_order_id = demo_id("order", "baseline:0")
    edited_order_id = demo_id("order", "baseline:1")
    original_brigade = demo_id("brigade", 0)
    changed_brigade = demo_id("brigade", 1)
    real_order_id = str(uuid4())
    with Session(engine) as session:
        # Emulate legacy missing columns without manufacturing an order edit.
        from sqlalchemy import update
        session.execute(update(WorkOrder).where(WorkOrder.id == original_order_id).values(executor_brigade_id=None))
        session.execute(update(Submission).where(Submission.order_id == original_order_id).values(actor_brigade_id=None))
        for event in session.scalars(select(OrderEvent).where(OrderEvent.order_id == original_order_id)):
            event.details = {name: event.details[name] for name in ("synthetic", "seed", "pattern")}
        session.get(User, demo_id("user", 2)).brigade_id = changed_brigade
        edited = session.get(WorkOrder, edited_order_id)
        edited.executor_brigade_id = None
        edited.work_description = "Результат отредактирован пользователем; не менять"
        for submission in session.scalars(select(Submission).where(Submission.order_id == edited_order_id)):
            submission.actor_brigade_id = None
        for event in session.scalars(select(OrderEvent).where(OrderEvent.order_id == edited_order_id)):
            event.details = {name: event.details[name] for name in ("synthetic", "seed", "pattern")}
        edited_event = session.scalar(select(OrderEvent).where(
            OrderEvent.order_id == demo_id("order", "baseline:2"), OrderEvent.action == "accepted"))
        edited_event_details = dict(edited_event.details)
        edited_event_details.pop("actor_brigade_id")
        edited_event_details["actor_role"] = "leader"
        edited_event.details = edited_event_details
        edited_event_id = edited_event.id
        explicit_submission = session.scalar(select(Submission).where(Submission.order_id == demo_id("order", "baseline:3")))
        explicit_submission.actor_brigade_id = changed_brigade
        explicit_submission_id = explicit_submission.id
        session.add(WorkOrder(
            id=real_order_id, number=10000, work_type="planned", description="Посторонняя старая запись",
            section_id=demo_id("section", 0), equipment_id=demo_id("equipment", 0),
            executor_id=demo_id("user", 2), executor_brigade_id=None, master_id=demo_id("user", 0),
            due_at=ANCHOR, created_at=ANCHOR - timedelta(days=1), status="closed", synthetic=False,
        ))
        session.commit()
        edited_version = edited.version
        assert edited_version > 1
        counts = table_counts(session)
    result = load(database)
    assert result["orders_added"] == 0
    with Session(engine) as session:
        assert table_counts(session) == counts
        original = session.get(WorkOrder, original_order_id)
        assert original.executor_brigade_id == original_brigade
        assert session.get(User, demo_id("user", 2)).brigade_id == changed_brigade
        submission = session.scalar(select(Submission).where(Submission.order_id == original_order_id))
        assert submission.actor_brigade_id == original_brigade
        for event in session.scalars(select(OrderEvent).where(OrderEvent.order_id == original_order_id)):
            if event.actor_id == demo_id("user", 2):
                assert event.details["actor_brigade_id"] == original_brigade
                assert event.details["actor_role"] == "worker"
                assert event.details["seed_snapshot_source"] == "original_roster_v1"
            if event.action == "issued":
                assert event.details["executor_brigade_id"] == original_brigade
        edited = session.get(WorkOrder, edited_order_id)
        assert edited.executor_brigade_id is None and edited.version == edited_version
        assert edited.work_description == "Результат отредактирован пользователем; не менять"
        assert session.scalar(select(Submission).where(Submission.order_id == edited_order_id)).actor_brigade_id is None
        assert all("actor_role" not in event.details for event in session.scalars(select(OrderEvent).where(OrderEvent.order_id == edited_order_id)))
        assert session.get(WorkOrder, real_order_id).executor_brigade_id is None
        assert session.get(OrderEvent, edited_event_id).details == edited_event_details
        assert session.get(Submission, explicit_submission_id).actor_brigade_id == changed_brigade
        original_version = original.version
    repeated = load(database)
    assert repeated["orders_added"] == 0
    with Session(engine) as session:
        assert table_counts(session) == counts
        assert session.get(WorkOrder, original_order_id).version == original_version
        assert session.get(User, demo_id("user", 2)).brigade_id == changed_brigade
        assert session.get(OrderEvent, edited_event_id).details == edited_event_details


def test_pending_seed_review_runs_rules_and_reaches_normal_master_acceptance(database, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    from app.config import get_settings
    from app.db import get_db
    from app.main import app
    from app.models import ReviewJob
    from app.worker import process_review_jobs

    engine, photo_dir = database
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_enabled", False)
    monkeypatch.setattr(settings, "worker_enabled", False)
    monkeypatch.setattr(settings, "upload_dir", photo_dir)
    load(database)
    order_id = demo_id("order", "current:ai_review")
    job_id = demo_id("review_job", order_id)
    with factory() as session:
        job = session.get(ReviewJob, job_id)
        assert job.state == "pending" and job.attempts == 0 and job.last_error is None
        assert session.scalar(select(Assessment).where(Assessment.order_id == order_id)) is None
        master_login = session.get(User, session.get(WorkOrder, order_id).master_id).login
    assert process_review_jobs(factory, now=ANCHOR + timedelta(hours=1)) == 1
    assert process_review_jobs(factory, now=ANCHOR + timedelta(hours=1)) == 0
    with factory() as session:
        job = session.get(ReviewJob, job_id)
        assert job.state == "completed" and job.attempts == 1
        assessments = list(session.scalars(select(Assessment).where(Assessment.order_id == order_id)))
        assert len(assessments) == 1
        assert assessments[0].engine == "rules" and assessments[0].score is None and assessments[0].model is None
        assert assessments[0].needs_master_review is True
        assert session.get(WorkOrder, order_id).status == "ai_review"

    def override_database():
        with factory() as session:
            yield session

    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = override_database
    try:
        with TestClient(app) as client:
            login = client.post("/api/auth/login", json={"login": master_login, "password": PASSWORD})
            assert login.status_code == 200, login.text
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            order = client.get(f"/api/orders/{order_id}", headers=headers)
            assert order.status_code == 200, order.text
            assert order.json()["assessment"]["engine"] == "rules"
            assert order.json()["final_score"] is None
            accepted = client.post(f"/api/orders/{order_id}/decision", headers=headers,
                                   json={"operation_id": str(uuid4()), "expected_version": order.json()["version"],
                                         "verdict": "accepted", "score": 5, "reason": "Результат проверен мастером вручную"})
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["status"] == "closed" and accepted.json()["final_score"] == 5
            assert accepted.json()["assessment"]["engine"] == "rules"
            assert accepted.json()["assessment"]["score"] is None
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
    # Repeat loading cannot reopen the accepted order or rewrite its processed job.
    assert load(database)["orders_added"] == 0
    with factory() as session:
        assert session.get(WorkOrder, order_id).status == "closed"
        assert session.get(ReviewJob, job_id).state == "completed"
        assert session.get(ReviewJob, job_id).attempts == 1
