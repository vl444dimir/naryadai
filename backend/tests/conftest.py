import os
os.environ.setdefault("DATABASE_URL", "sqlite:///test-import-only.db")
os.environ["WORKER_ENABLED"] = "false"
os.environ["AI_ENABLED"] = "false"
os.environ["TELEGRAM_BOT_TOKEN"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from app.db import Base, get_db, make_engine
from app.models import Brigade, Equipment, FaultCode, Material, OrderCounter, Section, User
from app.security import hash_password


@pytest.fixture
def core(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'core.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        brigade = Brigade(name="Бригада")
        section = Section(name="Дробление")
        db.add_all([brigade, section, OrderCounter(id=1, value=0)])
        db.flush()
        equipment = Equipment(name="Насос", inventory_number="N-1", section_id=section.id, equipment_type="насос", criticality=3)
        fault = FaultCode(code="М-01", name="Уплотнение")
        material = Material(name="Уплотнение", unit="шт", allowed_fault_prefixes=["М"])
        password = hash_password("Test-Password-2026!")
        users = {role: User(login=role, full_name=role, password_hash=password, pin_hash=hash_password("1234"), role="worker" if role == "other" else role, on_shift=True, synthetic=True, brigade_id=brigade.id if role in {"worker", "other"} else None) for role in ["admin", "master", "worker", "other", "leader"]}
        db.add_all([equipment, fault, material, *users.values()])
        db.commit()
        ids = {"users": {k: v.id for k, v in users.items()}, "equipment": equipment.id, "section": section.id, "fault": fault.id, "material": material.id, "brigade": brigade.id}
    yield {"factory": factory, "ids": ids, "engine": engine, "storage": tmp_path / "photos"}
    engine.dispose()


@pytest.fixture
def client(core, monkeypatch):
    from app.main import app
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "upload_dir", core["storage"])
    def override():
        with core["factory"]() as db:
            try:
                yield db
            except Exception:
                db.rollback()
                raise
    app.dependency_overrides[get_db] = override
    with TestClient(app) as instance:
        yield instance
    app.dependency_overrides.clear()


@pytest.fixture
def tokens(client):
    def login(role):
        result = client.post("/api/auth/login", json={"login": role, "password": "Test-Password-2026!"})
        assert result.status_code == 200, result.text
        return {"Authorization": "Bearer " + result.json()["access_token"]}
    return login
