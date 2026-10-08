import secrets
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..config import get_settings
from ..db import get_db, utcnow
from ..models import User, AuthSession
from ..schemas import Login
from ..security import bearer, get_current_user, hash_password, token_hash, verify_password

router = APIRouter(prefix="/auth", tags=["Вход"])


def public_user(user):
    return {key: getattr(user, key) for key in ("id", "login", "full_name", "role", "specialty", "grade", "brigade_id", "on_shift", "active")}


@router.post("/login")
def login(body: Login, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.login == body.login).with_for_update())
    now = utcnow()
    if user and user.locked_until and user.locked_until > now:
        raise HTTPException(429, "Вход временно заблокирован. Повторите позже")
    valid = user and user.active and verify_password(body.pin or body.password, user.pin_hash if body.pin else user.password_hash)
    if not valid:
        if user:
            user.failed_logins += 1
            if user.failed_logins >= 5:
                user.locked_until = now + timedelta(minutes=5)
            db.commit()
        else:
            # Avoid a fast path that reveals whether an account exists.
            verify_password(body.password or body.pin, hash_password("invalid-credential"))
        raise HTTPException(401, "Неверный логин или код")
    user.failed_logins = 0
    user.locked_until = None
    token = secrets.token_urlsafe(48)
    expires = now + timedelta(hours=get_settings().session_hours)
    db.add(AuthSession(user_id=user.id, token_hash=token_hash(token), expires_at=expires))
    db.commit()
    return {"access_token": token, "token_type": "bearer", "expires_at": expires, "user": public_user(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return public_user(user)


@router.post("/logout", status_code=204)
def logout(user: User = Depends(get_current_user), credentials=Depends(bearer), db: Session = Depends(get_db)):
    auth = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(credentials.credentials)))
    auth.revoked = True
    db.commit()
