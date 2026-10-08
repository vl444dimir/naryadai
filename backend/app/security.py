import hashlib
import hmac
import secrets
from datetime import timedelta
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db, utcnow
from .models import AuthSession, User, WorkOrder

bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, value: str | None) -> bool:
    try:
        algorithm, salt, expected = value.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(digest.hex(), expected)
    except (AttributeError, ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    if not credentials:
        raise HTTPException(401, "Требуется вход", headers={"WWW-Authenticate": "Bearer"})
    auth = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(credentials.credentials), AuthSession.revoked.is_(False), AuthSession.expires_at > utcnow()))
    user = db.get(User, auth.user_id) if auth else None
    if not user or not user.active:
        raise HTTPException(401, "Сессия недействительна", headers={"WWW-Authenticate": "Bearer"})
    return user


def require_roles(user: User, *roles):
    if user.role not in roles:
        raise HTTPException(403, "Недостаточно прав")


def can_access_order(user: User, order: WorkOrder):
    return user.role in {"master", "leader", "admin"} or order.executor_id == user.id or order.working_executor_id == user.id or (order.brigade_id is not None and user.brigade_id == order.brigade_id)


def ensure_order_access(db: Session, user: User, order: WorkOrder, write=False):
    if not can_access_order(user, order):
        raise HTTPException(404, "Наряд не найден")
    if write:
        require_roles(user, "worker")
        if order.working_executor_id and order.working_executor_id != user.id:
            raise HTTPException(409, "Наряд уже принят другим исполнителем")
