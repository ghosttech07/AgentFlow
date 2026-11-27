"""Password login, browser sessions and login lockout (Part 12 Step 0).

- Passwords: argon2id (argon2-cffi defaults). Unknown names are checked against a dummy hash, so a wrong name and a
  wrong password take the same time and get the same message (no user enumeration).
- Lockout: failures are counted per name AND per client IP in `login_attempts` (survives restarts). After
  LOGIN_MAX_FAILURES within LOGIN_FAILURE_WINDOW_MINUTES the key is locked for LOGIN_LOCKOUT_MINUTES. Counting also
  happens for names that don't exist, so a lockout reveals nothing either.
- Sessions: a random cookie token (only its sha256 is stored), an idle timeout (sliding) and an absolute maximum.
  Each session has a CSRF token: unsafe requests authenticated by the cookie must send it in X-CSRF-Token.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.errors import RateLimitedError, UnauthorizedError
from app.db.base import utcnow
from app.governance.principal import hash_key
from app.models.governance import LoginAttempt, Session, User

_hasher = PasswordHasher()  # argon2id
_DUMMY = _hasher.hash("not-a-real-password-just-for-timing")
GENERIC = "Invalid name or password"
MIN_PASSWORD = 12


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        from app.core.errors import ValidationFailedError

        raise ValidationFailedError(f"password must be at least {MIN_PASSWORD} characters")
    return _hasher.hash(password)


def _verify(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY, password) and stored is not None
    except (VerificationError, InvalidHashError):
        return False


async def _locked(session: AsyncSession, keys: list[str]) -> bool:
    now = utcnow()
    rows = await session.scalars(select(LoginAttempt).where(LoginAttempt.key.in_(keys)))
    return any(r.locked_until and r.locked_until > now for r in rows)


async def _fail(session: AsyncSession, keys: list[str], settings: Settings) -> None:
    now = utcnow()
    window = timedelta(minutes=settings.login_failure_window_minutes)
    for key in keys:
        row = await session.get(LoginAttempt, key, with_for_update=True)
        if row is None:
            row = LoginAttempt(key=key, failures=0)
            session.add(row)
        if row.first_failure_at is None or now - row.first_failure_at > window:
            row.failures, row.first_failure_at = 0, now
        row.failures += 1
        if row.failures >= settings.login_max_failures:
            row.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
    await session.commit()


async def login(session: AsyncSession, name: str, password: str, ip: str | None,
                settings: Settings) -> tuple[str, str, User]:
    """Returns (session token, csrf token, user). Raises 401 (generic) or 429 (locked, generic)."""
    keys = [f"name:{name.strip().lower()[:150]}", f"ip:{ip or 'unknown'}"]
    if await _locked(session, keys):
        raise RateLimitedError("Too many failed logins. Try again later.")
    user = await session.scalar(select(User).where(User.name == name.strip()))
    ok = _verify(user.password_hash if user else None, password)
    if not ok or user is None or not user.active:
        from app.audit.chain import enqueue

        enqueue(session, action="auth.login_failed", team_id=user.team_id if user else None, object_type="user",
                payload={"name": name.strip()[:64], "known_user": False},  # never reveal it, even in the audit
                actor={"actor_type": "user", "actor_id": None, "auth_method": "password", "ip": ip})
        await _fail(session, keys, settings)
        raise UnauthorizedError(GENERIC)
    await session.execute(delete(LoginAttempt).where(LoginAttempt.key.in_(keys)))
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    now = utcnow()
    session.add(Session(token_hash=hash_key(token), csrf_hash=hash_key(csrf), user_id=user.id, created_at=now,
                        last_seen_at=now, expires_at=now + timedelta(hours=settings.session_max_hours),
                        ip=(ip or "")[:64] or None))
    from app.audit.chain import enqueue

    enqueue(session, action="auth.login", team_id=user.team_id, object_type="user", object_id=user.id,
            actor={"actor_type": "user", "actor_id": user.name, "auth_method": "password", "ip": ip})
    if _hasher.check_needs_rehash(user.password_hash or ""):
        user.password_hash = _hasher.hash(password)
    await session.commit()
    return token, csrf, user


async def resolve(session: AsyncSession, token: str, settings: Settings) -> tuple[Session, User] | None:
    """A live session (not expired, not idle too long, active user) or None. Slides the idle timer."""
    row = await session.scalar(select(Session).where(Session.token_hash == hash_key(token)))
    if row is None:
        return None
    now = utcnow()
    if row.expires_at <= now or now - row.last_seen_at > timedelta(minutes=settings.session_idle_minutes):
        await session.delete(row)
        await session.commit()
        return None
    user = await session.get(User, row.user_id)
    if user is None or not user.active:
        return None
    if now - row.last_seen_at > timedelta(seconds=30):  # don't write on every request
        row.last_seen_at = now
        await session.commit()
    return row, user


def csrf_ok(row: Session, header: str | None) -> bool:
    return bool(header) and hmac.compare_digest(hash_key(header or ""), row.csrf_hash)


async def logout(session: AsyncSession, token: str) -> None:
    await session.execute(delete(Session).where(Session.token_hash == hash_key(token)))
    await session.commit()
