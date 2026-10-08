import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.models import Assessment, Equipment, Notification, OrderEvent, Photo, ReviewJob, Section, Submission, TelegramBotState, TelegramLink, User, WorkOrder
from app.services.notifications import apply_telegram_updates, create_notification, create_telegram_link, deliver_pending, poll_telegram_updates, scan_deadlines
from app.services.review import ProviderOptions, ProviderUnavailable, StaleReview, active_work_minutes, assess_snapshot, check_snapshot, generate_summary, model_payload, redact_text, run_review, snapshot_from_order


NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def snapshot(**changes):
    value = {"synthetic": True, "problem": "Течь масла из соединения насоса", "work_description": "Заменено уплотнение, проверено соединение", "work_type": "unplanned", "fault_code": "М-01", "materials_none": True, "materials": [], "photos": [{"id": "p1", "kind": "after", "sha256": "a" * 64, "phash": "0" * 16, "captured_at": NOW.isoformat(), "uploaded_at": NOW.isoformat()}], "previous_photos": [], "started_at": (NOW - timedelta(minutes=20)).isoformat(), "submitted_at": NOW.isoformat(), "due_at": (NOW + timedelta(minutes=20)).isoformat(), "norm_minutes": 60, "redact_names": []}
    value.update(changes)
    return value


def model_output(**changes):
    output = {"verdict": "accepted", "score": 95, "explanation": "Описание работ соответствует проблеме. Фото не анализировалось.", "good": ["Замена уплотнения описана."], "improve": [], "findings": [{"code": "semantic_match", "severity": "info", "message": "Работы соответствуют описанной проблеме.", "evidence": ["field:problem", "field:work_description"]}], "needs_master_review": True}
    output.update(changes)
    return output


def options(**changes):
    value = dict(enabled=True, base_url="https://model.example/v1", model="test-model", api_key="do-not-leak")
    value.update(changes)
    return ProviderOptions(**value)


def model_client(output=None, status=200, inspect=None):
    def handler(request):
        if inspect:
            inspect(request)
        return httpx.Response(status, json={"choices": [{"message": {"content": json.dumps(output or model_output(), ensure_ascii=False)}}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_disabled_is_rules_and_does_not_claim_semantic_or_visual_ai():
    result = asyncio.run(assess_snapshot(snapshot(), ProviderOptions()))
    assert result["engine"] == "rules"
    assert result["model_status"] == "disabled"
    assert result["model"] is None
    assert result["score_basis"] == "completeness_rules"
    assert result["needs_master_review"]


def test_real_data_is_blocked_without_explicit_configuration():
    def unexpected(_):
        pytest.fail("real data must not reach provider")
    result = asyncio.run(assess_snapshot(snapshot(synthetic=False), options(), client=model_client(inspect=unexpected)))
    assert result["model_status"] == "blocked_data_policy"
    assert result["engine"] == "rules"


def test_real_model_request_is_bounded_and_redacts_known_identity():
    data = snapshot(problem="Течь; Тестовый Иван, ivan@example.org, +7 777 111 22 33", redact_names=["Тестовый Иван"])
    def inspect(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert "Тестовый Иван" not in request.content.decode()
        assert "ivan@example.org" not in request.content.decode()
        assert "777 111" not in request.content.decode()
        assert body["model"] == "test-model"
    result = asyncio.run(assess_snapshot(data, options(), client=model_client(inspect=inspect)))
    assert result["engine"] == "llm"
    assert result["model_status"] == "completed"


def test_model_cannot_override_missing_photo_or_invent_evidence():
    result = asyncio.run(assess_snapshot(snapshot(photos=[]), options(), client=model_client()))
    assert result["verdict"] == "requires_rework"
    assert any(f["code"] == "missing_after_photo" for f in result["findings"])
    fabricated = model_output(findings=[{"code": "claim", "severity": "error", "message": "Причина", "evidence": ["photo:made-up"]}])
    bad = asyncio.run(assess_snapshot(snapshot(), options(), client=model_client(fabricated)))
    assert bad["model_status"] == "failed"
    assert bad["model_error"] == "model_evidence_invalid"
    assert bad["engine"] == "rules"


def test_provider_failure_does_not_expose_credentials_or_raw_response():
    result = asyncio.run(assess_snapshot(snapshot(), options(), client=model_client(status=429)))
    assert result["model_error"] == "model_http_429"
    assert "do-not-leak" not in json.dumps(result)


def test_ollama_uses_real_chat_protocol_and_schema():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert body["stream"] is False
        assert body["format"]["type"] == "object"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"message": {"content": json.dumps(model_output())}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(assess_snapshot(snapshot(), options(provider="ollama", base_url="http://127.0.0.1:11434"), client=client))
    assert result["model_status"] == "completed"


def test_malformed_model_result_falls_back_without_claiming_ai_completion():
    incomplete = model_output()
    del incomplete["score"]
    result = asyncio.run(assess_snapshot(snapshot(), options(), client=model_client(incomplete)))
    assert result["model_status"] == "failed"
    assert result["model_error"] == "model_response_invalid"
    assert result["engine"] == "rules"


def test_summary_allowlist_drops_identity_and_rejects_invented_numbers():
    facts = {"synthetic": True, "employee_name": "Тестовый Иван", "counts": {"issued": 5, "completed": 3, "overdue": 2, "rejected": 0, "employee_phone": 7771112233}}
    good = {"text": "Выдано 5 нарядов, закрыто 3; просрочено 2.", "evidence": ["counts.issued", "counts.completed", "counts.overdue"]}
    def inspect(request):
        assert "Тестовый Иван" not in request.content.decode()
        assert "7771112233" not in request.content.decode()
        assert "employee_name" not in request.content.decode()
    result = generate_summary(facts, options(), client=model_client(good, inspect=inspect))
    assert result["engine"] == "llm"
    assert result["model_status"] == "completed"
    bad = {"text": "Просрочено 99 нарядов.", "evidence": ["counts.overdue"]}
    result = generate_summary(facts, options(), client=model_client(bad))
    assert result["engine"] == "rules"
    assert result["model_status"] == "failed"
    assert result["model_error"] == "summary_number_invented"


def test_disabled_summary_is_honest_and_incomplete_rating_remains_incomplete():
    result = generate_summary({"counts": {"issued": 2, "completed": 1}, "synthetic": True}, ProviderOptions())
    assert result["engine"] == "rules"
    assert result["model_status"] == "disabled"
    result = generate_summary({"counts": {"closed": 0}, "score": None, "provisional": True}, ProviderOptions())
    assert "недостаточно" in result["text"]
    assert result["model"] is None


def test_photos_check_exact_previous_and_current_before_without_claiming_fraud():
    same = {**snapshot()["photos"][0], "id": "old", "kind": "before"}
    data = snapshot(photos=snapshot()["photos"] + [same])
    assert any(f["code"] == "photo_exact_duplicate" for f in check_snapshot(data)["findings"])
    similar = {**same, "sha256": "b" * 64, "phash": "0000000000000001"}
    data = snapshot(previous_photos=[similar])
    result = check_snapshot(data)
    assert result["verdict"] == "accepted_with_remarks"
    assert any(f["code"] == "photo_similar_to_previous" and f["severity"] == "warning" for f in result["findings"])


def test_exact_photo_copy_is_not_hidden_by_a_newer_similar_photo():
    photo = snapshot()["photos"][0]
    near = {**photo, "id": "near", "sha256": "b" * 64, "phash": "0000000000000001"}
    exact = {**photo, "id": "exact"}
    result = check_snapshot(snapshot(previous_photos=[near, exact]))
    assert result["verdict"] == "requires_rework"
    matching = [item for item in result["findings"] if item["code"] == "photo_exact_duplicate"]
    assert matching[0]["evidence"] == ["photo:p1", "photo:exact"]
    assert not any(item["code"] == "photo_similar_to_previous" for item in result["findings"])


@pytest.mark.parametrize("alias", ["Тестов И. С.", "Тестов И.С.", "Тестов И.", "И. С. Тестов", "Иван Тестов", "Иван", "Тестов", "Сергеевич", "тестов и. с."])
def test_known_personal_name_aliases_are_minimized(alias):
    result = redact_text("Сообщить: " + alias + "; проверить насос.", ["Тестов Иван Сергеевич"])
    assert not any(token.casefold() in result.casefold() for token in ["Тестов", "Иван", "Сергеевич"])
    assert "насос" in result
    if "." in alias:
        assert "И." not in result and "С." not in result


def test_name_minimization_respects_word_boundaries_and_catalog_text():
    assert "Тестовый" in redact_text("Тестовый стенд", ["Тестов Иван Сергеевич"])
    data = snapshot(redact_names=["Тестов Иван Сергеевич"], materials=[{"id": "m", "name": "Материал Иван Тестов", "quantity": 1, "unit": "шт"}])
    payload = model_payload(data, check_snapshot(data))
    assert "Тестов" not in payload["materials"][0]["name"]
    assert "Иван" not in payload["materials"][0]["name"]


def test_evidence_allowlist_does_not_expose_unrelated_history_or_fake_links():
    previous = [{"id": f"unrelated-{index}", "kind": "after", "sha256": f"{index:064x}", "phash": "f" * 16} for index in range(5000)]
    data = snapshot(previous_photos=previous)
    rules = check_snapshot(data)
    rules["findings"].append({"code": "invalid_test_link", "severity": "warning", "message": "test", "evidence": ["photo:made-up", "https://example.invalid/fake"]})
    payload = model_payload(data, rules)
    assert len(payload["valid_evidence_ids"]) < 20
    assert "photo:unrelated-1" not in payload["valid_evidence_ids"]
    assert "photo:made-up" not in payload["valid_evidence_ids"]
    assert "https://example.invalid/fake" not in payload["valid_evidence_ids"]
    fabricated = model_output(findings=[{"code": "claim", "severity": "error", "message": "test", "evidence": ["photo:unrelated-1"]}])
    result = asyncio.run(assess_snapshot(data, options(), client=model_client(fabricated)))
    assert result["model_status"] == "failed"
    assert result["model_error"] == "model_evidence_invalid"


def test_missing_exif_is_warning_and_nan_quantity_is_rejected():
    photo = {**snapshot()["photos"][0], "captured_at": None}
    result = check_snapshot(snapshot(photos=[photo]))
    assert result["verdict"] == "accepted_with_remarks"
    assert any(f["code"] == "photo_capture_time_unknown" for f in result["findings"])
    result = check_snapshot(snapshot(materials_none=False, materials=[{"id": "m", "quantity": float("nan"), "unit": "шт"}]))
    assert result["verdict"] == "requires_rework"


def test_active_work_excludes_pause_and_does_not_penalize_calendar_wait():
    events = [SimpleNamespace(occurred_at=NOW - timedelta(minutes=120), to_status="in_progress"), SimpleNamespace(occurred_at=NOW - timedelta(minutes=110), to_status="paused"), SimpleNamespace(occurred_at=NOW - timedelta(minutes=10), to_status="in_progress"), SimpleNamespace(occurred_at=NOW, to_status="performed")]
    active = active_work_minutes(events, NOW)
    assert active == 20
    result = check_snapshot(snapshot(started_at=(NOW - timedelta(minutes=120)).isoformat(), active_minutes=active, norm_minutes=30))
    assert result["time"]["calendar_minutes"] == 120
    assert result["time"]["waiting_minutes"] == 100
    assert not any(f["code"] == "execution_longer_than_norm" for f in result["findings"])


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Section(id="s", name="Участок"))
        db.add_all([User(id="master", login="master", full_name="Тестовый мастер", password_hash="hash", role="master", synthetic=True), User(id="worker", login="worker", full_name="Тестовый рабочий", password_hash="hash", role="worker", specialty="слесарь", on_shift=True, synthetic=True), User(id="other", login="other", full_name="Другой рабочий", password_hash="hash", role="worker", specialty="слесарь", on_shift=True, synthetic=True)])
        db.flush()
        db.add(Equipment(id="e", name="Насос", inventory_number="N-1", section_id="s", equipment_type="pump"))
        db.commit()
    yield factory
    engine.dispose()


def add_order(db, **changes):
    value = dict(id="order", number=1, work_type="unplanned", description="Течь насоса", section_id="s", equipment_id="e", executor_id="worker", master_id="master", status="issued", priority="normal", due_at=NOW + timedelta(minutes=10), created_at=NOW - timedelta(minutes=15), norm_minutes=60, synthetic=True)
    value.update(changes)
    order = WorkOrder(**value)
    db.add(order)
    db.commit()
    return order


def add_submission(db, order, *, photos=True):
    order.result_version = 1
    order.status = "ai_review"
    order.started_at = NOW - timedelta(minutes=20)
    order.submitted_at = NOW
    from app.models import FaultCode
    db.add(FaultCode(id="f", code="М-01", name="Уплотнение"))
    db.flush()
    db.add(Submission(order_id=order.id, result_version=1, actor_id="worker", work_description="Заменено уплотнение", fault_code_id="f", materials_none=True, submitted_at=NOW))
    if photos:
        db.add(Photo(id="p", order_id=order.id, author_id="worker", kind="after", path="test.jpg", sha256="a" * 64, phash="0" * 16, captured_at=NOW, uploaded_at=NOW, result_version=1, synthetic=True))
    db.commit()


def test_review_is_persisted_once_and_master_decision_is_not_made(factory):
    with factory() as db:
        order = add_order(db)
        add_submission(db, order)
        first = run_review(db, order.id, 1, options=ProviderOptions())
        second = run_review(db, order.id, 1, options=ProviderOptions())
        assert first.id == second.id
        assert first.score is None
        assert first.details["model_status"] == "disabled"
        assert db.get(WorkOrder, order.id).status == "ai_review"
        assert db.get(WorkOrder, order.id).closed_at is None
        assert db.scalar(select(func.count()).select_from(Assessment)) == 1
        assert db.scalar(select(func.count()).select_from(Notification)) == 2


def test_exact_photo_older_than_similarity_window_is_found_by_indexed_lookup(factory):
    with factory() as db:
        order = add_order(db)
        add_submission(db, order)
        older = Photo(id="older-exact", order_id=order.id, author_id="worker", kind="after", path="old.jpg", sha256="a" * 64, phash="0" * 16, uploaded_at=NOW - timedelta(days=1), captured_at=NOW - timedelta(days=1), result_version=0, synthetic=True)
        db.add(older)
        db.add_all([Photo(id=f"history-{index}", order_id=order.id, author_id="worker", kind="after", path="history.jpg", sha256=f"{index + 1000:064x}", phash="f" * 16, uploaded_at=NOW - timedelta(seconds=1), captured_at=NOW - timedelta(seconds=1), result_version=0, synthetic=True) for index in range(5001)])
        db.commit()
        exact_queries = []
        def record_exact_query(connection, cursor, statement, parameters, context, executemany):
            if "WHERE photos.sha256 =" in statement:
                exact_queries.append(statement)
        event.listen(db.bind, "before_cursor_execute", record_exact_query)
        try:
            data = snapshot_from_order(db, order, 1)
        finally:
            event.remove(db.bind, "before_cursor_execute", record_exact_query)
        assert len(exact_queries) == 1  # One current after hash, one equality lookup.
        assert len(data["previous_photos"]) == 5001
        assert any(photo["id"] == older.id for photo in data["previous_photos"])
        rules = check_snapshot(data)
        exact_finding = next(item for item in rules["findings"] if item["code"] == "photo_exact_duplicate")
        assert "photo:older-exact" in exact_finding["evidence"]
        assert rules["verdict"] == "requires_rework"
        payload = model_payload(data, rules)
        assert "photo:older-exact" in payload["valid_evidence_ids"]
        assert len(payload["valid_evidence_ids"]) < 20
        assert not any(item.startswith("photo:history-") for item in payload["valid_evidence_ids"])
        assert len(json.dumps(payload, ensure_ascii=False)) < 8000


def test_rework_and_notification_are_not_duplicated(factory):
    with factory() as db:
        order = add_order(db)
        add_submission(db, order, photos=False)
        run_review(db, order.id, 1, options=ProviderOptions())
        run_review(db, order.id, 1, options=ProviderOptions())
        assert db.get(WorkOrder, order.id).rework_count == 1
        assert db.get(WorkOrder, order.id).status == "rework"
        assert db.scalar(select(func.count()).select_from(Assessment)) == 1


def test_stale_model_result_is_not_saved(factory):
    with factory() as db:
        order = add_order(db)
        add_submission(db, order)
        def changed(_):
            with factory() as newer:
                current = newer.get(WorkOrder, "order")
                current.result_version = 2
                newer.commit()
        with pytest.raises(StaleReview):
            run_review(db, order.id, 1, options=options(), client=model_client(inspect=changed))
        assert db.scalar(select(func.count()).select_from(Assessment)) == 0


def deadline_settings(**changes):
    value = dict(reminder_minutes=30, repeat_minutes=30, accept_minutes=10, emergency_accept_minutes=3, telegram_bot_token="", public_app_url="https://app.example")
    value.update(changes)
    return SimpleNamespace(**value)


def test_deadline_notifications_are_durable_deduplicated_and_repeat(factory):
    with factory() as db:
        add_order(db, status="in_progress", accepted_at=NOW - timedelta(minutes=10), due_at=NOW - timedelta(minutes=1))
        assert scan_deadlines(db, NOW, settings=deadline_settings()) == 2
        assert scan_deadlines(db, NOW, settings=deadline_settings()) == 0
        assert scan_deadlines(db, NOW + timedelta(minutes=30), settings=deadline_settings()) == 2
        assert db.get(WorkOrder, "order").status == "in_progress"
        assert set(db.scalars(select(Notification.user_id))) == {"worker", "master"}


def test_review_waiting_has_no_deadline_warning_and_rework_resumes_control(factory):
    with factory() as db:
        order = add_order(db, status="ai_review", due_at=NOW - timedelta(minutes=1))
        assert scan_deadlines(db, NOW, settings=deadline_settings()) == 0
        order.status = "rework"
        db.commit()
        assert scan_deadlines(db, NOW, settings=deadline_settings()) == 2


def test_emergency_accept_threshold_and_reassignment_reset(factory):
    with factory() as db:
        order = add_order(db, priority="emergency", created_at=NOW - timedelta(minutes=4), due_at=NOW + timedelta(hours=1))
        assert scan_deadlines(db, NOW, settings=deadline_settings()) == 1
        db.add(OrderEvent(order_id=order.id, action="reassigned", actor_id="master", occurred_at=NOW))
        db.commit()
        assert scan_deadlines(db, NOW + timedelta(minutes=2), settings=deadline_settings()) == 0
        assert scan_deadlines(db, NOW + timedelta(minutes=3), settings=deadline_settings()) == 1


def test_prolonged_overdue_escalates_to_leader_once_per_interval(factory):
    with factory() as db:
        db.add(User(id="leader", login="leader", full_name="Руководитель", password_hash="hash", role="leader"))
        db.commit()
        add_order(db, status="in_progress", due_at=NOW - timedelta(minutes=121))
        assert scan_deadlines(db, NOW, settings=deadline_settings(leader_escalation_minutes=120)) == 3
        assert scan_deadlines(db, NOW, settings=deadline_settings(leader_escalation_minutes=120)) == 0
        item = db.scalar(select(Notification).where(Notification.user_id == "leader"))
        assert item.kind == "prolonged_overdue"


def test_outbox_does_not_fake_delivery_and_retries_safely(factory):
    with factory() as db:
        item = create_notification(db, "worker", "test", "Тест", "Текст", dedupe_key="one")
        db.commit()
        assert deliver_pending(db, settings=deadline_settings()) == 0
        assert db.get(Notification, item.id).telegram_status == "unconfigured"
        db.get(User, "worker").telegram_chat_id = "12345"
        db.commit()
        failed = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})))
        assert deliver_pending(db, settings=deadline_settings(telegram_bot_token="secret-token"), client=failed) == 0
        stored = db.get(Notification, item.id)
        assert stored.telegram_status == "retry"
        assert stored.last_error == "telegram_http_429"
        assert "secret-token" not in stored.last_error
        success = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})))
        assert deliver_pending(db, now=stored.next_attempt_at + timedelta(seconds=1), settings=deadline_settings(telegram_bot_token="secret-token"), client=success) == 1
        assert db.get(Notification, item.id).telegram_status == "sent"
        assert deliver_pending(db, now=stored.next_attempt_at + timedelta(days=1), settings=deadline_settings(telegram_bot_token="secret-token"), client=success) == 0


def outbox_transport(calls):
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def link_test_users(db):
    db.get(User, "worker").telegram_chat_id = "12345"
    db.get(User, "master").telegram_chat_id = "54321"
    db.commit()


def normalize_notification_clock(db):
    for item in db.scalars(select(Notification)):
        item.created_at = NOW
        item.next_attempt_at = NOW
    db.commit()


def test_outbox_cancels_inactive_recipient_without_network(factory):
    with factory() as db:
        link_test_users(db)
        item = create_notification(db, "worker", "test", "Тест", "Текст", dedupe_key="inactive")
        db.commit()
        db.get(User, "worker").active = False
        db.commit()
        calls = []
        assert deliver_pending(db, settings=deadline_settings(telegram_bot_token="fake"), client=outbox_transport(calls)) == 0
        assert calls == []
        assert db.get(Notification, item.id).telegram_status == "cancelled"
        assert db.get(Notification, item.id).last_error == "recipient_inactive"


def test_outbox_cancels_recipient_who_lost_order_access(factory):
    with factory() as db:
        order = add_order(db)
        link_test_users(db)
        item = create_notification(db, "worker", "assigned", "Наряд", "Текст", order_id=order.id, dedupe_key="assignment")
        db.commit()
        order.executor_id = "other"
        order.working_executor_id = None
        db.commit()
        calls = []
        assert deliver_pending(db, settings=deadline_settings(telegram_bot_token="fake"), client=outbox_transport(calls)) == 0
        assert calls == []
        assert db.get(Notification, item.id).last_error == "recipient_order_access_revoked"


@pytest.mark.parametrize("status", ["closed", "cancelled", "ai_review"])
def test_obsolete_overdue_alerts_are_not_sent_after_order_state_changes(factory, status):
    with factory() as db:
        order = add_order(db, status="in_progress", due_at=NOW - timedelta(minutes=1))
        link_test_users(db)
        scan_deadlines(db, NOW, settings=deadline_settings())
        normalize_notification_clock(db)
        order.status = status
        db.commit()
        calls = []
        assert deliver_pending(db, now=NOW, settings=deadline_settings(telegram_bot_token="fake"), client=outbox_transport(calls)) == 0
        assert calls == []
        assert set(db.scalars(select(Notification.telegram_status))) == {"cancelled"}


def test_postponed_deadline_invalidates_previously_queued_overdue(factory):
    with factory() as db:
        order = add_order(db, status="in_progress", due_at=NOW - timedelta(minutes=1))
        link_test_users(db)
        scan_deadlines(db, NOW, settings=deadline_settings())
        normalize_notification_clock(db)
        order.due_at = NOW + timedelta(hours=1)
        db.commit()
        calls = []
        assert deliver_pending(db, now=NOW, settings=deadline_settings(telegram_bot_token="fake"), client=outbox_transport(calls)) == 0
        assert calls == []
        assert set(db.scalars(select(Notification.last_error))) == {"notification_deadline_obsolete"}


def test_reassignment_cancels_old_nonaccepted_alert(factory):
    with factory() as db:
        order = add_order(db, due_at=NOW + timedelta(hours=1))
        link_test_users(db)
        scan_deadlines(db, NOW, settings=deadline_settings())
        normalize_notification_clock(db)
        order.executor_id = "other"
        db.add(OrderEvent(order_id=order.id, action="reassigned", occurred_at=NOW + timedelta(seconds=1), to_status="issued"))
        db.commit()
        calls = []
        assert deliver_pending(db, now=NOW + timedelta(seconds=2), settings=deadline_settings(telegram_bot_token="fake"), client=outbox_transport(calls)) == 0
        assert calls == []
        assert set(db.scalars(select(Notification.last_error))) == {"notification_assignment_obsolete"}


@pytest.mark.parametrize("status", ["accepted", "queued"])
def test_acceptance_cancels_old_nonaccepted_alert(factory, status):
    with factory() as db:
        order = add_order(db, due_at=NOW + timedelta(hours=1))
        link_test_users(db)
        scan_deadlines(db, NOW, settings=deadline_settings())
        normalize_notification_clock(db)
        order.status = status
        order.accepted_at = NOW
        db.commit()
        calls = []
        assert deliver_pending(db, now=NOW, settings=deadline_settings(telegram_bot_token="fake"), client=outbox_transport(calls)) == 0
        assert calls == []
        assert set(db.scalars(select(Notification.last_error))) == {"notification_acceptance_obsolete"}


def test_valid_expired_delivery_lease_is_recovered_and_current_alert_sent(factory):
    with factory() as db:
        add_order(db, status="in_progress", due_at=NOW - timedelta(minutes=1))
        link_test_users(db)
        scan_deadlines(db, NOW, settings=deadline_settings())
        normalize_notification_clock(db)
        for item in db.scalars(select(Notification)):
            item.telegram_status = "sending"
            item.next_attempt_at = NOW - timedelta(seconds=1)
        db.commit()
        calls = []
        assert deliver_pending(db, now=NOW, settings=deadline_settings(telegram_bot_token="fake"), client=outbox_transport(calls)) == 2
        assert len(calls) == 2
        assert set(db.scalars(select(Notification.telegram_status))) == {"sent"}


def test_a_lost_delivery_lease_does_not_overwrite_new_worker_state(factory):
    with factory() as db:
        link_test_users(db)
        item = create_notification(db, "worker", "test", "Тест", "Текст", dedupe_key="lease")
        db.commit()
        def changed(request):
            with factory() as newer:
                current = newer.get(Notification, item.id)
                current.telegram_status = "cancelled"
                current.last_error = "cancelled_by_new_owner"
                current.next_attempt_at = NOW + timedelta(hours=1)
                newer.commit()
            return httpx.Response(200, json={"ok": True})
        client = httpx.AsyncClient(transport=httpx.MockTransport(changed))
        assert deliver_pending(db, settings=deadline_settings(telegram_bot_token="fake"), client=client) == 1
        db.expire_all()
        assert db.get(Notification, item.id).telegram_status == "cancelled"
        assert db.get(Notification, item.id).last_error == "cancelled_by_new_owner"


def test_notification_api_keeps_users_in_their_own_inbox(factory):
    from app.api.notifications import router
    from app.security import get_current_user
    with factory() as db:
        own = create_notification(db, "worker", "test", "Моё", "Текст", dedupe_key="own")
        other = create_notification(db, "other", "test", "Чужое", "Текст", dedupe_key="other")
        db.commit()
        application = FastAPI()
        application.include_router(router)
        application.dependency_overrides[get_db] = lambda: db
        application.dependency_overrides[get_current_user] = lambda: db.get(User, "worker")
        with TestClient(application) as client:
            response = client.get("/notifications")
            assert response.status_code == 200
            assert [item["id"] for item in response.json()["items"]] == [own.id]
            assert client.post(f"/notifications/{other.id}/read").status_code == 404
            assert client.post(f"/notifications/{own.id}/read").status_code == 200
            assert client.get("/notifications").json()["unread_count"] == 0
            assert client.put("/notifications/telegram", json={"chat_id": "12345"}).status_code == 403


def telegram_update(token, update_id=1, chat_id=12345, chat_type="private", sender_id=None):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id, "type": chat_type}, "from": {"id": chat_id if sender_id is None else sender_id}, "text": "/start " + token}}


def test_telegram_binding_requires_private_chat_owner_and_one_time_token(factory):
    with factory() as db:
        link = create_telegram_link(db, db.get(User, "worker"), settings=deadline_settings(telegram_bot_token="secret", telegram_bot_username="NaryadTestBot"))
        token = link["url"].split("start=")[1]
        stored = db.scalar(select(TelegramLink))
        assert stored.token_hash != token
        assert apply_telegram_updates(db, [telegram_update(token, chat_type="group")]) == 0
        assert apply_telegram_updates(db, [telegram_update(token, update_id=2, sender_id=54321)]) == 0
        assert apply_telegram_updates(db, [telegram_update(token, update_id=3)]) == 1
        assert db.get(User, "worker").telegram_chat_id == "12345"
        assert db.get(TelegramBotState, 1).update_offset == 4
        assert apply_telegram_updates(db, [telegram_update(token, update_id=4)]) == 0
        assert db.scalar(select(func.count()).select_from(Notification)) == 1


def test_expired_link_and_chat_already_bound_to_another_user_are_rejected(factory):
    with factory() as db:
        settings = deadline_settings(telegram_bot_token="secret", telegram_bot_username="NaryadTestBot")
        link = create_telegram_link(db, db.get(User, "worker"), settings=settings)
        token = link["url"].split("start=")[1]
        assert apply_telegram_updates(db, [telegram_update(token)], now=link["expires_at"] + timedelta(seconds=1)) == 0
        link = create_telegram_link(db, db.get(User, "worker"), settings=settings)
        db.get(User, "other").telegram_chat_id = "12345"
        db.commit()
        assert apply_telegram_updates(db, [telegram_update(link["url"].split("start=")[1], update_id=2)]) == 0


def test_polling_uses_durable_offset_and_never_logs_token(factory):
    with factory() as db:
        db.add(TelegramBotState(id=1, update_offset=42))
        db.commit()
        def handler(request):
            assert request.url.path.endswith("/getUpdates")
            assert json.loads(request.content)["offset"] == 42
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 42, "message": {"text": "hello"}}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert poll_telegram_updates(db, settings=deadline_settings(telegram_bot_token="secret"), client=client) == 0
        assert db.get(TelegramBotState, 1).update_offset == 43


def test_telegram_token_is_not_emitted_by_httpx_info_logs(caplog):
    import logging
    from app.services.notifications import _send_telegram
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})))
    with caplog.at_level(logging.INFO, logger="httpx"):
        ok, error, _ = asyncio.run(_send_telegram("private-bot-token", "12345", "test", client=client))
    assert ok and error is None
    assert "private-bot-token" not in caplog.text


def test_deadline_tick_does_not_wait_for_network_delivery(factory, monkeypatch):
    import app.worker as worker
    def never(*args, **kwargs):
        pytest.fail("deadline tick must never invoke model or Telegram")
    monkeypatch.setattr(worker, "deliver_pending", never)
    monkeypatch.setattr(worker, "poll_telegram_updates", never)
    monkeypatch.setattr(worker, "run_review", never)
    assert worker.deadline_tick(factory) == {"queued": 0}


def test_durable_review_failure_has_finite_retries_and_manual_fallback(factory, monkeypatch):
    import app.worker as worker
    with factory() as db:
        order = add_order(db)
        add_submission(db, order)
        job = ReviewJob(order_id=order.id, input_version=1, attempts=2, next_attempt_at=NOW)
        db.add(job)
        db.commit()
        job_id = job.id
    def unavailable(*args, **kwargs):
        raise ProviderUnavailable("model_http_503")
    monkeypatch.setattr(worker, "run_review", unavailable)
    assert worker.process_review_jobs(factory, now=NOW) == 0
    with factory() as db:
        assert db.get(ReviewJob, job_id).state == "failed"
        assert db.get(ReviewJob, job_id).attempts == 3
        assessment = db.scalar(select(Assessment))
        assert assessment.score is None
        assert assessment.engine == "rules"
        assert assessment.details["model_status"] == "failed"
        assert db.get(WorkOrder, "order").status == "ai_review"
        assert db.scalar(select(func.count()).select_from(Notification)) == 2
