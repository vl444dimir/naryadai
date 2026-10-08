from contextlib import asynccontextmanager
import threading
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from .config import get_settings
from .db import SessionLocal
from .api import auth, catalog, events, orders, photos, notifications, reports


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    if settings.worker_enabled:
        from .worker import run_worker
        handle = threading.Event()
        thread = threading.Thread(target=run_worker, args=(handle,), daemon=True)
        thread.start()
    else:
        handle = None
    yield
    if handle is not None:
        handle.set()
        thread.join(timeout=5)


app = FastAPI(title="НарядAI Backend", version="0.1.0", description="Сервер выдачи, исполнения и приёмки ремонтных нарядов по ТЗ", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins, allow_credentials=False, allow_methods=["GET", "POST", "PUT", "PATCH"], allow_headers=["Authorization", "Content-Type"])
for module in (auth, catalog, orders, photos, events, notifications, reports):
    app.include_router(module.router, prefix="/api")


@app.get("/health", tags=["Состояние"])
def health():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unavailable"})
