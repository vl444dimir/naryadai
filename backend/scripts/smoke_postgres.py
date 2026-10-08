"""Explicit smoke on the isolated localhost demo database, never production."""
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from datetime import timedelta
from PIL import Image
import httpx
from app.config import get_settings
from app.db import SessionLocal, utcnow
from app.models import WorkOrder
from app.worker import process_review_jobs, deadline_tick


BASE = "http://127.0.0.1:8000"


def main():
    from sqlalchemy.engine import make_url
    url = make_url(get_settings().database_url)
    if url.host != "127.0.0.1" or url.port != 54339 or url.database != "naryadai":
        raise RuntimeError("Smoke is restricted to the task's isolated localhost:54339/naryadai database")
    with httpx.Client(base_url=BASE, timeout=30) as client:
        health = client.get("/health")
        assert health.status_code == 200, health.text
        def login(name):
            response = client.post("/api/auth/login", json={"login": name, "password": get_settings().demo_password})
            assert response.status_code == 200, response.text
            return {"Authorization": "Bearer " + response.json()["access_token"]}
        master = login("demo.master1")
        users = client.get("/api/catalog/users?role=worker&on_shift=true", headers=master).json()["items"]
        candidates = [user for user in users if user["availability"] in {"free", "queued"}]
        if not candidates:
            raise RuntimeError("Smoke needs an on-shift demo worker without an active repair")
        worker_record = min(candidates, key=lambda user: user["availability"] != "free")
        worker = login(worker_record["login"])
        equipment = client.get("/api/catalog/equipment", headers=master).json()["items"][0]
        fault = client.get("/api/catalog/fault-codes", headers=master).json()["items"][0]
        operation_id = str(uuid4())
        body = {"operation_id": operation_id, "work_type": "unplanned", "description": "Тест сквозного API: заменить уплотнение насоса", "equipment_id": equipment["id"], "executor_id": worker_record["id"], "priority": "emergency", "norm_minutes": 60}
        def issue(_):
            return client.post("/api/orders", json=body, headers=master)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(issue, range(2)))
        assert all(response.status_code == 201 for response in responses), [r.text for r in responses]
        assert responses[0].json() == responses[1].json()
        order = responses[0].json()
        accept = {"operation_id": str(uuid4()), "expected_version": order["version"], "action": "accept"}
        def take(_):
            return client.post(f"/api/orders/{order['id']}/actions", json=accept, headers=worker)
        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = list(pool.map(take, range(2)))
        assert all(r.status_code == 200 for r in accepted), [r.text for r in accepted]
        assert accepted[0].json() == accepted[1].json()
        order = accepted[0].json()
        response = client.post(f"/api/orders/{order['id']}/actions", json={"operation_id": str(uuid4()), "expected_version": order["version"], "action": "start"}, headers=worker)
        assert response.status_code == 200, response.text
        order = response.json()
        image = Image.new("RGB", (120, 80))
        nonce = uuid4().int
        image.putdata([((nonce >> (i % 64)) % 256, i % 256, (i * 17) % 256) for i in range(120 * 80)])
        content = io.BytesIO()
        image.save(content, "PNG")
        response = client.post(f"/api/orders/{order['id']}/photos", headers=worker, data={"operation_id": str(uuid4()), "expected_version": order["version"], "kind": "after", "source": "camera", "captured_at": utcnow().isoformat()}, files={"file": ("test.png", content.getvalue(), "image/png")})
        assert response.status_code == 201, response.text
        order = response.json()
        response = client.post(f"/api/orders/{order['id']}/result", headers=worker, json={"operation_id": str(uuid4()), "expected_version": order["version"], "work_description": "Уплотнение заменено, соединение проверено", "fault_code_id": fault["id"], "materials_none": True, "photo_ids": [order["photos"][-1]["id"]]})
        assert response.status_code == 200, response.text
        process_review_jobs()
        order = client.get(f"/api/orders/{order['id']}", headers=master).json()
        assert order["assessment"] is not None
        assert order["assessment"]["engine"] == "rules"
        response = client.post(f"/api/orders/{order['id']}/decision", headers=master, json={"operation_id": str(uuid4()), "expected_version": order["version"], "verdict": "accepted", "score": 5, "reason": "Принято в тестовом сквозном сценарии"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "closed"
        queued_body = {**body, "operation_id": str(uuid4()), "description": "Тест просрочки очереди"}
        response = client.post("/api/orders", headers=master, json=queued_body)
        assert response.status_code == 201, response.text
        queued = response.json()
        response = client.post(f"/api/orders/{queued['id']}/actions", headers=worker, json={"operation_id": str(uuid4()), "expected_version": queued["version"], "action": "queue"})
        assert response.status_code == 200
        # This script is explicitly a synthetic test fixture, not a public API that edits time.
        with SessionLocal() as db:
            entity = db.get(WorkOrder, queued["id"])
            assert entity.description == "Тест просрочки очереди"
            assert entity.master_id == order["master_id"]
            entity.due_at = utcnow() - timedelta(minutes=5)
            db.commit()
        tick = deadline_tick()
        inbox = client.get("/api/notifications", headers=worker).json()
        assert any(item["order_id"] == queued["id"] and item["kind"] == "overdue" for item in inbox["items"])
        report = client.get("/api/reports/shift", headers=master)
        assert report.status_code == 200, report.text
        xlsx = client.get("/api/reports/export.xlsx", headers=master)
        assert xlsx.status_code == 200 and xlsx.content.startswith(b"PK")
        events = client.get("/api/events?after=0&limit=500", headers=worker)
        assert events.status_code == 200
        cursor = events.json()["cursor"]
        ready = threading.Event()
        received = threading.Event()
        elapsed = []
        started = time.monotonic()
        def listen():
            with httpx.Client(base_url=BASE, timeout=8) as listener:
                with listener.stream("GET", f"/api/events/stream?after={cursor}", headers=worker) as stream:
                    assert stream.status_code == 200
                    ready.set()
                    for line in stream.iter_lines():
                        if line.startswith("data:"):
                            elapsed.append(time.monotonic() - started)
                            received.set()
                            return
        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        assert ready.wait(5), "SSE connection did not open"
        started = time.monotonic()
        pending = []
        for number in range(2):
            response = client.post("/api/orders", headers=master, json={**body, "operation_id": str(uuid4()), "description": f"Тест конкурентной очереди {number}"})
            assert response.status_code == 201
            pending.append(response.json())
        assert received.wait(5), "No real-time event"
        thread.join(timeout=2)
        assert elapsed[0] < 5
        def queue(item):
            return client.post(f"/api/orders/{item['id']}/actions", headers=worker, json={"operation_id": str(uuid4()), "expected_version": item["version"], "action": "queue"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            queues = list(pool.map(queue, pending))
        assert all(r.status_code == 200 for r in queues), [r.text for r in queues]
        assert len({r.json()["queue_position"] for r in queues}) == 2
        cursor = client.get("/api/events?after=0&limit=500", headers=worker).json()["cursor"]
        while True:
            batch = client.get(f"/api/events?after={cursor}&limit=500", headers=worker).json()
            cursor = batch["cursor"]
            if not batch["items"]:
                break
        removed_order = queues[0].json()
        replacement = next(record for record in users if record["id"] != worker_record["id"])
        ready.clear()
        received.clear()
        removal_elapsed, listener_errors = [], []
        def listen_removal():
            try:
                with httpx.Client(base_url=BASE, timeout=8) as listener:
                    with listener.stream("GET", f"/api/events/stream?after={cursor}", headers=worker) as stream:
                        assert stream.status_code == 200
                        ready.set()
                        for line in stream.iter_lines():
                            if line.startswith("data:"):
                                signal = json.loads(line[5:].strip())
                                if signal.get("order_id") == removed_order["id"] and signal.get("action") == "assignment_revoked":
                                    assert signal["reason"] is None and signal["to_status"] is None
                                    removal_elapsed.append(time.monotonic() - started)
                                    received.set()
                                    return
            except Exception as exc:
                listener_errors.append(str(exc))
        removal_thread = threading.Thread(target=listen_removal, daemon=True)
        removal_thread.start()
        assert ready.wait(5), "Removal SSE did not open"
        started = time.monotonic()
        response = client.patch(f"/api/orders/{removed_order['id']}", headers=master,
                                json={"operation_id": str(uuid4()), "expected_version": removed_order["version"],
                                      "executor_id": replacement["id"], "reason": "Тест снятия старого назначения"})
        assert response.status_code == 200, response.text
        assert received.wait(5), f"No removal signal: {listener_errors}"
        removal_thread.join(timeout=2)
        assert not listener_errors and removal_elapsed[0] < 5
        assert client.get(f"/api/orders/{removed_order['id']}", headers=worker).status_code == 404
        print(json.dumps({"postgresql": "passed", "api_cycle": "passed", "concurrent_same_operation": "passed", "concurrent_queue_positions": "passed", "sse_local_seconds": round(elapsed[0], 3), "sse_reassignment_removal_seconds": round(removal_elapsed[0], 3), "photos": "passed", "durable_review": "rules_only", "deadlines_and_inbox": "passed", "reports_and_xlsx": "passed", "model_live": "not_configured", "phone_push": "not_configured"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
