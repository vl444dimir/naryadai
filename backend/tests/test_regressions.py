"""Independent scenarios for assignment, closure evidence and request replay."""
import io
from uuid import uuid4

from PIL import Image
import pytest
from sqlalchemy import func, select

from app.models import Photo, Submission, WorkOrder


def _operation(version=None):
    data = {"operation_id": str(uuid4())}
    if version is not None:
        data["expected_version"] = version
    return data


def _create(client, headers, core, **updates):
    body = {**_operation(), "work_type": "planned", "description": "Заменить уплотнение насоса",
            "equipment_id": core["ids"]["equipment"], "executor_id": core["ids"]["users"]["worker"],
            "priority": "normal", **updates}
    response = client.post("/api/orders", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _action(client, order, headers, action, **updates):
    response = client.post(f"/api/orders/{order['id']}/actions",
                           json={**_operation(order["version"]), "action": action, **updates}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _start(client, order, headers):
    return _action(client, _action(client, order, headers, "accept"), headers, "start")


def _image(color="blue"):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buffer, "PNG")
    return buffer.getvalue()


def _upload(client, order, headers, operation=None, content=None):
    return client.post(f"/api/orders/{order['id']}/photos",
                       data={**(operation or _operation(order["version"])), "kind": "after"},
                       files={"file": ("repair.png", content or _image(), "image/png")}, headers=headers)


def test_reassigned_worker_can_attach_result_after_old_worker_filled_drafts(client, core, tokens):
    master, worker, other = tokens("master"), tokens("worker"), tokens("other")
    order = _start(client, _create(client, master, core, work_type="unplanned"), worker)
    for index in range(5):
        response = _upload(client, order, worker, content=_image((index * 30, 80, 150)))
        assert response.status_code == 201, response.text
        order = response.json()
    reassigned = client.patch(f"/api/orders/{order['id']}",
                             json={**_operation(order["version"]), "executor_id": core["ids"]["users"]["other"],
                                   "reason": "Другой исполнитель продолжает ремонт"}, headers=master)
    assert reassigned.status_code == 200, reassigned.text
    order = _start(client, reassigned.json(), other)
    assert client.get(f"/api/orders/{order['id']}", headers=worker).status_code == 404
    response = _upload(client, order, other, content=_image("green"))
    assert response.status_code == 201, response.text


def test_material_unit_cannot_silently_rewrite_existing_consumption(client, core, tokens):
    master, worker, admin = tokens("master"), tokens("worker"), tokens("admin")
    order = _start(client, _create(client, master, core), worker)
    response = client.post(f"/api/orders/{order['id']}/result", headers=worker,
                           json={**_operation(order["version"]), "work_description": "Заменил уплотнение",
                                 "fault_code_id": core["ids"]["fault"],
                                 "materials": [{"material_id": core["ids"]["material"], "quantity": 2}]})
    assert response.status_code == 200, response.text
    assert response.json()["materials"][0]["unit"] == "шт"
    changed = client.put(f"/api/catalog/materials/{core['ids']['material']}", headers=admin,
                         json={"name": "Уплотнение", "unit": "кг", "allowed_fault_prefixes": ["М"]})
    assert changed.status_code == 409, "Used material units must be protected or snapshotted; " + changed.text
    current = client.get(f"/api/orders/{order['id']}", headers=master)
    assert current.json()["materials"][0]["unit"] == "шт"


def test_brigade_claim_has_one_writer_and_other_member_keeps_read_access(client, core, tokens):
    master, worker, other = tokens("master"), tokens("worker"), tokens("other")
    order = _create(client, master, core, executor_id=None, brigade_id=core["ids"]["brigade"])
    claimed = _action(client, order, worker, "accept")
    assert claimed["working_executor_id"] == core["ids"]["users"]["worker"]
    assert client.get(f"/api/orders/{order['id']}", headers=other).status_code == 200
    blocked = client.post(f"/api/orders/{order['id']}/actions", headers=other,
                          json={**_operation(claimed["version"]), "action": "start"})
    assert blocked.status_code == 409
    started = _action(client, claimed, worker, "start")
    assert started["active_executor_id"] == core["ids"]["users"]["worker"]


def test_photo_replay_is_exact_and_changed_file_is_conflict(client, core, tokens):
    master, worker = tokens("master"), tokens("worker")
    order = _start(client, _create(client, master, core), worker)
    operation = _operation(order["version"])
    first = _upload(client, order, worker, operation=operation, content=_image("blue"))
    assert first.status_code == 201
    repeated = _upload(client, order, worker, operation=operation, content=_image("blue"))
    assert repeated.status_code == 201 and repeated.json() == first.json()
    changed = _upload(client, order, worker, operation=operation, content=_image("red"))
    assert changed.status_code == 409
    with core["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Photo)) == 1
    assert len(list(core["storage"].iterdir())) == 1


def test_stale_photo_or_result_does_not_create_extra_records(client, core, tokens):
    master, worker = tokens("master"), tokens("worker")
    order = _start(client, _create(client, master, core), worker)
    first = _upload(client, order, worker)
    assert first.status_code == 201
    assert _upload(client, order, worker, content=_image("green")).status_code == 409
    response = client.post(f"/api/orders/{order['id']}/result", headers=worker,
                           json={**_operation(order["version"]), "work_description": "Ремонт выполнен",
                                 "fault_code_id": core["ids"]["fault"], "materials_none": True})
    assert response.status_code == 409
    with core["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Photo)) == 1
        assert session.scalar(select(func.count()).select_from(Submission)) == 0


def test_used_photo_cannot_move_to_another_result_version(client, core, tokens):
    from app.services.review import run_review

    master, worker = tokens("master"), tokens("worker")
    order = _start(client, _create(client, master, core, work_type="unplanned"), worker)
    uploaded = _upload(client, order, worker)
    assert uploaded.status_code == 201
    order = uploaded.json()
    photo_id = order["photos"][-1]["id"]
    result = {**_operation(order["version"]), "work_description": "Заменил уплотнение",
              "fault_code_id": core["ids"]["fault"], "materials_none": True, "photo_ids": [photo_id]}
    submitted = client.post(f"/api/orders/{order['id']}/result", headers=worker, json=result)
    assert submitted.status_code == 200, submitted.text
    with core["factory"]() as session:
        run_review(session, order["id"], 1)
    order = client.get(f"/api/orders/{order['id']}", headers=master).json()
    rework = client.post(f"/api/orders/{order['id']}/decision", headers=master,
                        json={**_operation(order["version"]), "verdict": "requires_rework", "score": 2,
                              "reason": "Уточнить результаты проверки под нагрузкой"})
    assert rework.status_code == 200, rework.text
    order = _action(client, rework.json(), worker, "start")
    response = client.post(f"/api/orders/{order['id']}/result", headers=worker,
                           json={**_operation(order["version"]), "work_description": "Проверил повторно",
                                 "fault_code_id": core["ids"]["fault"], "materials_none": True, "photo_ids": [photo_id]})
    assert response.status_code == 422
    with core["factory"]() as session:
        assert session.get(Photo, photo_id).result_version == 1
        assert session.get(WorkOrder, order["id"]).result_version == 1


def test_worker_cannot_finalize_order_or_start_outside_shift(client, core, tokens):
    master, worker = tokens("master"), tokens("worker")
    order = _create(client, master, core)
    decision = client.post(f"/api/orders/{order['id']}/decision", headers=worker,
                           json={**_operation(order["version"]), "verdict": "accepted", "score": 5,
                                 "reason": "Самостоятельная приёмка запрещена"})
    assert decision.status_code == 403
    changed = client.put(f"/api/catalog/users/{core['ids']['users']['worker']}/attendance",
                         headers=master, json={"on_shift": False})
    assert changed.status_code == 200
    denied = client.post(f"/api/orders/{order['id']}/actions", headers=worker,
                         json={**_operation(order["version"]), "action": "accept"})
    assert denied.status_code == 409


def test_demo_account_does_not_certify_new_free_text_as_synthetic(client, core, tokens):
    # The fixture account is synthetic; that does not certify its new input.
    with core["factory"]() as session:
        from app.models import User
        assert session.get(User, core["ids"]["users"]["master"]).synthetic is True
    order = _create(client, tokens("master"), core, description="Свободный текст новой заявки")
    assert order["synthetic"] is False


def test_order_counters_cover_all_filtered_rows_without_pagination_or_foreign_leaks(client, core, tokens):
    master, worker, other = tokens("master"), tokens("worker"), tokens("other")
    issued = _create(client, master, core)
    accepted = _action(client, _create(client, master, core, priority="high"), worker, "accept")
    listing = client.get("/api/orders?limit=1", headers=worker).json()
    assert len(listing["items"]) == 1 and listing["total"] == 2
    assert listing["counts_by_status"]["issued"] == 1 and listing["counts_by_status"]["accepted"] == 1
    assert listing["overdue_count"] == 0
    assert client.get("/api/orders", headers=other).json()["total"] == 0
    high = client.get("/api/orders?priority=high&offset=10&limit=1", headers=master).json()
    assert high["items"] == [] and high["total"] == 1 and high["counts_by_status"]["accepted"] == 1
    with core["factory"]() as db:
        from datetime import timedelta
        from app.db import utcnow
        db.get(WorkOrder, issued["id"]).due_at = utcnow() - timedelta(minutes=3)
        db.commit()
    late = client.get("/api/orders?overdue=true&limit=1", headers=master).json()
    assert late["total"] == late["overdue_count"] == 1 and late["items"][0]["id"] == issued["id"]


@pytest.mark.parametrize("assignment_kind", ["individual", "brigade"])
def test_reassignment_delivers_private_removal_signal_to_previous_assignees(client, core, tokens, assignment_kind):
    from app.models import OrderEventRevocation, User

    master, worker, other = tokens("master"), tokens("worker"), tokens("other")
    with core["factory"]() as db:
        replacement = User(login="replacement", full_name="Replacement", password_hash=db.get(User, core["ids"]["users"]["worker"]).password_hash, role="worker", on_shift=True)
        db.add(replacement)
        db.commit()
        replacement_id = replacement.id
    updates = {"executor_id": None, "brigade_id": core["ids"]["brigade"]} if assignment_kind == "brigade" else {}
    order = _create(client, master, core, **updates)
    cursor = client.get("/api/events", headers=worker).json()["cursor"]
    body = {**_operation(order["version"]), "executor_id": replacement_id, "reason": "Закрытое пояснение мастера о новом назначении"}
    reassigned = client.patch(f"/api/orders/{order['id']}", headers=master, json=body)
    assert reassigned.status_code == 200, reassigned.text
    # Brigade membership changes must not erase the already addressed removal.
    with core["factory"]() as db:
        db.get(User, core["ids"]["users"]["worker"]).brigade_id = None
        db.commit()
    recipients = [worker, other] if assignment_kind == "brigade" else [worker]
    for headers in recipients:
        assert client.get(f"/api/orders/{order['id']}", headers=headers).status_code == 404
        items = client.get(f"/api/events?after={cursor}", headers=headers).json()["items"]
        assert len(items) == 1
        assert items[0]["order_id"] == order["id"]
        assert items[0]["action"] == "assignment_revoked"
        assert items[0]["to_status"] is None and items[0]["reason"] is None
    current = tokens("replacement")
    visible = client.get(f"/api/events?after={cursor}", headers=current).json()["items"]
    assert visible[-1]["action"] == "reassigned" and visible[-1]["reason"] == body["reason"]
    repeated = client.patch(f"/api/orders/{order['id']}", headers=master, json=body)
    assert repeated.status_code == 200 and repeated.json() == reassigned.json()
    with core["factory"]() as db:
        assert db.scalar(select(func.count()).select_from(OrderEventRevocation)) == len(recipients)
    response = client.post(f"/api/orders/{order['id']}/comments", headers=master,
                           json={**_operation(reassigned.json()["version"]), "text": "Комментарий для нового исполнителя"})
    assert response.status_code == 200, response.text
    for headers in recipients:
        assert len(client.get(f"/api/events?after={cursor}", headers=headers).json()["items"]) == 1


@pytest.mark.parametrize("terminal", ["cancelled", "closed"])
def test_existing_downtime_can_end_after_terminal_order_but_cannot_restart(client, core, tokens, terminal):
    from app.models import EquipmentDowntime
    from app.services.review import run_review

    master, worker = tokens("master"), tokens("worker")
    order = _create(client, master, core, equipment_stopped=True)
    if terminal == "cancelled":
        terminal_response = client.patch(f"/api/orders/{order['id']}", headers=master,
                                        json={**_operation(order["version"]), "cancel": True,
                                              "reason": "Работу отменили, факт простоя остаётся"})
    else:
        order = _start(client, order, worker)
        submitted = client.post(f"/api/orders/{order['id']}/result", headers=worker,
                                json={**_operation(order["version"]), "work_description": "Заменил уплотнение",
                                      "fault_code_id": core["ids"]["fault"], "materials_none": True})
        assert submitted.status_code == 200, submitted.text
        with core["factory"]() as session:
            run_review(session, order["id"], 1)
        order = client.get(f"/api/orders/{order['id']}", headers=master).json()
        terminal_response = client.post(f"/api/orders/{order['id']}/decision", headers=master,
                                       json={**_operation(order["version"]), "verdict": "accepted", "score": 5,
                                             "reason": "Ремонт принят, оборудование ещё не запущено"})
    assert terminal_response.status_code == 200, terminal_response.text
    order = terminal_response.json()
    assert order["status"] == terminal
    terminal_times = {name: order[name] for name in ("cancelled_at", "closed_at")}
    with core["factory"]() as session:
        downtime = session.scalar(select(EquipmentDowntime).where(EquipmentDowntime.order_id == order["id"]))
        assert downtime is not None and downtime.ended_at is None
        original_downtime_id = downtime.id
    end_body = {**_operation(order["version"]), "action": "end", "reason": "Оборудование фактически запущено"}
    ended = client.post(f"/api/orders/{order['id']}/downtime", headers=master, json=end_body)
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == terminal
    assert {name: ended.json()[name] for name in terminal_times} == terminal_times
    assert ended.json()["version"] > order["version"]
    repeated = client.post(f"/api/orders/{order['id']}/downtime", headers=master, json=end_body)
    assert repeated.status_code == 200 and repeated.json() == ended.json()
    restarted = client.post(f"/api/orders/{order['id']}/downtime", headers=master,
                            json={**_operation(ended.json()["version"]), "action": "start",
                                  "reason": "Новый отказ должен быть отдельным нарядом"})
    assert restarted.status_code == 409
    with core["factory"]() as session:
        downtimes = list(session.scalars(select(EquipmentDowntime).where(EquipmentDowntime.order_id == order["id"])))
        assert len(downtimes) == 1 and downtimes[0].id == original_downtime_id
        assert downtimes[0].ended_at is not None and downtimes[0].ended_at >= downtimes[0].started_at
