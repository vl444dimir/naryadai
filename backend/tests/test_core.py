from datetime import timedelta
import io
from uuid import uuid4
from PIL import Image
from sqlalchemy import select, func
from app.db import utcnow
from app.models import WorkOrder, MaterialUsage, OrderEvent, User


def operation(version=None):
    data = {"operation_id": str(uuid4())}
    if version:
        data["expected_version"] = version
    return data


def create(client, headers, ids, **updates):
    body = {**operation(), "work_type": "unplanned", "description": "Течь масла, заменить уплотнение", "equipment_id": ids["equipment"], "executor_id": ids["users"]["worker"], "priority": "emergency", "norm_minutes": 60, **updates}
    response = client.post("/api/orders", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json(), body


def action(client, order, headers, value, **updates):
    response = client.post(f"/api/orders/{order['id']}/actions", json={**operation(order["version"]), "action": value, **updates}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_full_cycle_photo_review_master_and_replay(client, core, tokens):
    from app.services.review import run_review
    ids = core["ids"]
    master, worker = tokens("master"), tokens("worker")
    order, create_body = create(client, master, ids, equipment_stopped=True)
    replay = client.post("/api/orders", json=create_body, headers=master)
    assert replay.json() == order
    order = action(client, order, worker, "accept")
    order = action(client, order, worker, "start")
    buffer = io.BytesIO()
    Image.new("RGB", (2400, 1800), "blue").save(buffer, "PNG")
    response = client.post(f"/api/orders/{order['id']}/photos", data={**operation(order["version"]), "kind": "after", "source": "camera", "captured_at": utcnow().isoformat()}, files={"file": ("image.png", buffer.getvalue(), "image/png")}, headers=worker)
    assert response.status_code == 201, response.text
    order = response.json()
    photo_id = order["photos"][-1]["id"]
    result = {**operation(order["version"]), "work_description": "Заменил уплотнение, проверил соединение", "fault_code_id": ids["fault"], "materials": [{"material_id": ids["material"], "quantity": 1}], "photo_ids": [photo_id]}
    response = client.post(f"/api/orders/{order['id']}/result", json=result, headers=worker)
    assert response.status_code == 200, response.text
    order = response.json()
    replay = client.post(f"/api/orders/{order['id']}/result", json=result, headers=worker)
    assert replay.json() == order
    with core["factory"]() as db:
        assessment = run_review(db, order["id"], 1)
        assert assessment.engine == "rules"
    order = client.get(f"/api/orders/{order['id']}", headers=master).json()
    response = client.post(f"/api/orders/{order['id']}/decision", json={**operation(order["version"]), "verdict": "accepted", "score": 5, "reason": "Проверено мастером"}, headers=master)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"
    assert response.json()["final_score"] == 5
    photo = client.get(f"/api/photos/{photo_id}/file", headers=worker)
    assert photo.status_code == 200
    image = Image.open(io.BytesIO(photo.content))
    assert max(image.size) <= 1600 and image.format == "JPEG"
    with core["factory"]() as db:
        assert db.scalar(select(func.count(WorkOrder.id))) == 1
        assert db.scalar(select(func.count(MaterialUsage.id))) == 1
        assert db.scalar(select(func.count(OrderEvent.id))) >= 6
    events = client.get("/api/events?after=0", headers=worker).json()
    assert any(item["action"] == "closed" for item in events["items"])


def test_rbac_versions_one_active_job_and_reassignment(client, core, tokens):
    master, worker, other, leader = [tokens(x) for x in ["master", "worker", "other", "leader"]]
    ids = core["ids"]
    first, _ = create(client, master, ids)
    assert client.get(f"/api/orders/{first['id']}", headers=other).status_code == 404
    assert client.post("/api/orders", json={**operation(), "work_type": "planned", "description": "Ремонт", "equipment_id": ids["equipment"], "executor_id": ids["users"]["worker"]}, headers=leader).status_code == 403
    stale = {**operation(first["version"] + 1), "action": "accept"}
    assert client.post(f"/api/orders/{first['id']}/actions", json=stale, headers=worker).status_code == 409
    first = action(client, first, worker, "accept")
    first = action(client, first, worker, "start")
    second, _ = create(client, master, ids)
    second = action(client, second, worker, "queue")
    assert client.post(f"/api/orders/{second['id']}/actions", json={**operation(second["version"]), "action": "start"}, headers=worker).status_code == 409
    first = action(client, first, worker, "pause", reason="Ожидание запчасти")
    second = action(client, second, worker, "start")
    response = client.patch(f"/api/orders/{first['id']}", json={**operation(first["version"]), "executor_id": ids["users"]["other"], "reason": "Смена исполнителя"}, headers=master)
    assert response.status_code == 200
    assert client.post(f"/api/orders/{first['id']}/actions", json={**operation(response.json()["version"]), "action": "accept"}, headers=worker).status_code == 404
    availability = client.get("/api/catalog/users?role=worker", headers=master).json()["items"]
    assert next(u for u in availability if u["id"] == ids["users"]["worker"])["availability"] == "busy"


def test_missing_photo_requires_rework_and_no_fake_ai(client, core, tokens):
    from app.services.review import run_review
    master, worker = tokens("master"), tokens("worker")
    order, _ = create(client, master, core["ids"])
    order = action(client, order, worker, "accept")
    order = action(client, order, worker, "start")
    response = client.post(f"/api/orders/{order['id']}/result", json={**operation(order["version"]), "work_description": "Ремонт выполнен", "fault_code_id": core["ids"]["fault"], "materials_none": True}, headers=worker)
    assert response.status_code == 200
    with core["factory"]() as db:
        assessment = run_review(db, order["id"], 1)
        assert assessment.verdict == "requires_rework"
        assert assessment.engine == "rules" and assessment.score is None
    order = client.get(f"/api/orders/{order['id']}", headers=master).json()
    assert order["status"] == "rework"
    assert client.post(f"/api/orders/{order['id']}/decision", json={**operation(order["version"]), "verdict": "accepted", "score": 5, "reason": "Принять"}, headers=master).status_code == 422


def test_pin_logout_and_bruteforce_lock(client):
    response = client.post("/api/auth/login", json={"login": "worker", "pin": "1234"})
    assert response.status_code == 200
    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    for _ in range(5):
        assert client.post("/api/auth/login", json={"login": "worker", "pin": "0000"}).status_code == 401
    assert client.post("/api/auth/login", json={"login": "worker", "pin": "1234"}).status_code == 429


def test_catalog_roles_data_validation(client, core, tokens):
    master, admin = tokens("master"), tokens("admin")
    assert client.post("/api/catalog/sections", json={"name": "Цех"}, headers=master).status_code == 403
    response = client.post("/api/catalog/sections", json={"name": "Цех"}, headers=admin)
    assert response.status_code == 201
    assert client.post("/api/catalog/sections", json={"name": "Цех"}, headers=admin).status_code == 409
    assert client.post("/api/catalog/equipment", json={"name": "Насос", "inventory_number": "N-2", "equipment_type": "насос", "section_id": str(uuid4())}, headers=admin).status_code == 422
    employees = client.get("/api/catalog/users", headers=admin).json()["items"]
    assert all("password_hash" not in u and "pin_hash" not in u for u in employees)


def test_demo_account_cannot_label_new_input_as_synthetic(client, core, tokens):
    order, _ = create(client, tokens("master"), core["ids"])
    assert order["synthetic"] is False
