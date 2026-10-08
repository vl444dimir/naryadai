from datetime import datetime
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from pydantic import ValidationError
from ..config import get_settings
from ..db import get_db, utcnow
from ..models import Photo, User
from ..schemas import VersionInput
from ..security import ensure_order_access, get_current_user, require_roles
from ..services.orders import event, load_order, run_operation
from ..services.photos import compress_photo, resolve_photo_path

router = APIRouter(tags=["Фото"])


@router.post("/orders/{order_id}/photos", status_code=201)
async def upload(order_id: UUID, operation_id: UUID = Form(...), expected_version: int = Form(...), kind: str = Form(...), captured_at: datetime | None = Form(None), source: str = Form("upload"), file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    settings = get_settings()
    if kind not in {"before", "after"} or source not in {"upload", "camera", "gallery"}:
        raise HTTPException(422, "Неизвестный тип фото")
    if captured_at is not None and captured_at.tzinfo is None:
        raise HTTPException(422, "Время съёмки должно содержать часовой пояс")
    if expected_version < 1:
        raise HTTPException(422, "Некорректная версия")
    raw = await file.read(settings.max_photo_bytes + 1)
    if len(raw) > settings.max_photo_bytes:
        raise HTTPException(413, "Фото превышает допустимый размер")
    content, sha, fingerprint = compress_photo(raw)
    body = VersionInput(operation_id=operation_id, expected_version=expected_version)
    path_written = None
    def attach():
        nonlocal path_written
        order = load_order(db, user, order_id, expected_version)
        if order.status in {"closed", "cancelled"}:
            raise HTTPException(409, "Наряд завершён")
        if kind == "before":
            require_roles(user, "master", "admin")
            if order.status not in {"issued", "accepted", "queued"}:
                raise HTTPException(409, "Фото до добавляется перед началом работ")
        else:
            ensure_order_access(db, user, order, write=True)
            if order.status not in {"in_progress", "paused", "rework"}:
                raise HTTPException(409, "Фото после добавляется при выполнении")
        count_query = select(func.count(Photo.id)).where(Photo.order_id == order.id, Photo.kind == kind)
        if kind == "after":
            count_query = count_query.where(Photo.result_version.is_(None), Photo.author_id == user.id)
        if db.scalar(count_query) >= 5:
            raise HTTPException(422, "Можно добавить не больше пяти фото")
        photo_id = str(uuid4())
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{photo_id}.jpg"
        path_written = settings.upload_dir / filename
        path_written.write_bytes(content)
        db.add(Photo(id=photo_id, order_id=order.id, author_id=user.id, kind=kind, path=filename, sha256=sha, phash=fingerprint, captured_at=captured_at, source=source, synthetic=order.synthetic, result_version=0 if kind == "before" else None))
        order.version += 1
        event(db, order, user, "photo_uploaded", order.status, details={"photo_id": photo_id, "kind": kind})
        return order
    try:
        capture = captured_at.isoformat() if captured_at else ""
        return run_operation(db, user, body, f"photo:{order_id}:{kind}:{sha}:{capture}:{source}", attach)
    except Exception:
        if path_written and path_written.is_file():
            path_written.unlink()
        raise


@router.get("/photos/{photo_id}/file")
def get_file(photo_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    photo = db.get(Photo, str(photo_id))
    if not photo:
        raise HTTPException(404, "Фото не найдено")
    load_order(db, user, photo.order_id)
    path = resolve_photo_path(get_settings().upload_dir, photo.path)
    return FileResponse(path, media_type=photo.mime_type, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
