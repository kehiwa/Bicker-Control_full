"""Persistent configuration, users, delegated permissions, and audit history."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT_ROLE = "root"
ADMIN_ROLE = "admin"
USER_ROLE = "user"
ROLES = frozenset((ROOT_ROLE, ADMIN_ROLE, USER_ROLE))

KNOWN_PERMISSIONS = frozenset(
    {
        "view_status",
        "view_logs",
        "view_settings",
        "configure_inputs",
        "configure_network",
        "configure_snmp",
        "configure_ups",
        "manage_users",
        "ups_shutdown",
        "ups_restart",
    }
)

_AUDIT_GENESIS = "0" * 64
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LENGTH = 32


class StateStoreError(RuntimeError):
    """Base exception for persistent state or authorization errors."""


class AuthenticationError(StateStoreError):
    """Raised when an account or password is invalid or disabled."""


class AuthorizationError(StateStoreError):
    """Raised when an actor lacks a permission or delegation ceiling."""


class BootstrapError(StateStoreError):
    """Raised when one-time root bootstrap has already been completed."""


class StateValidationError(StateStoreError, ValueError):
    """Raised when a requested role, setting, or permission is invalid."""


@dataclass(frozen=True, slots=True)
class User:
    user_id: int
    username: str
    role: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: int
    created_at: str
    actor_id: int | None
    action: str
    target: str | None
    details: dict[str, Any]
    previous_hash: str
    event_hash: str


class StateStore:
    """Thread-safe SQLite persistence with delegated role capabilities."""

    def __init__(self, database: str | Path) -> None:
        database_path = str(database)
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            database_path,
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if database_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def is_bootstrapped(self) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM users WHERE role = ? LIMIT 1", (ROOT_ROLE,)
            ).fetchone()
        return row is not None

    def create_initial_root(self, username: str, password: str) -> User:
        normalized_username = self._validate_username(username)
        password_hash = self._hash_password(password)
        with self._transaction() as connection:
            root_exists = connection.execute(
                "SELECT 1 FROM users WHERE role = ? LIMIT 1", (ROOT_ROLE,)
            ).fetchone()
            if root_exists is not None:
                raise BootstrapError("root account is already initialized")
            cursor = connection.execute(
                "INSERT INTO users(username, role, password_hash, enabled, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (normalized_username, ROOT_ROLE, password_hash, self._now()),
            )
            user_id = int(cursor.lastrowid)
            self._append_audit(
                connection,
                actor_id=None,
                action="root.bootstrap",
                target=f"user:{user_id}",
                details={"username": normalized_username},
            )
            return User(user_id, normalized_username, ROOT_ROLE, True)

    def authenticate(self, username: str, password: str) -> User:
        normalized_username = self._validate_username(username)
        with self._lock:
            row = self._connection.execute(
                "SELECT user_id, username, role, password_hash, enabled "
                "FROM users WHERE username = ?",
                (normalized_username,),
            ).fetchone()
        if row is None or not row["enabled"]:
            raise AuthenticationError("invalid username or password")
        if not self._verify_password(password, row["password_hash"]):
            raise AuthenticationError("invalid username or password")
        return User(int(row["user_id"]), row["username"], row["role"], bool(row["enabled"]))

    def get_user(self, user_id: int) -> User:
        with self._lock:
            row = self._connection.execute(
                "SELECT user_id, username, role, enabled FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise StateStoreError(f"user {user_id} does not exist")
        return User(int(row["user_id"]), row["username"], row["role"], bool(row["enabled"]))

    def create_user(
        self,
        actor_id: int,
        username: str,
        role: str,
        password: str,
        permissions: Iterable[str] = (),
    ) -> User:
        normalized_username = self._validate_username(username)
        if role not in (ADMIN_ROLE, USER_ROLE):
            raise StateValidationError("only root may create admin/user accounts")
        granted = self._validate_permissions(permissions)
        password_hash = self._hash_password(password)

        with self._transaction() as connection:
            actor = self._get_user_row(connection, actor_id)
            self._require_permission(connection, actor, "manage_users")
            if actor["role"] == ADMIN_ROLE and role != USER_ROLE:
                raise AuthorizationError("admins may create user accounts only")
            if actor["role"] == ADMIN_ROLE and granted:
                self._require_delegable(connection, actor, granted)
            elif actor["role"] == ROOT_ROLE and role == ADMIN_ROLE and granted:
                raise StateValidationError(
                    "create admins without permissions, then grant their root-approved capabilities"
                )
            self._require_delegable(connection, actor, granted)

            cursor = connection.execute(
                "INSERT INTO users(username, role, password_hash, enabled, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (normalized_username, role, password_hash, self._now()),
            )
            user_id = int(cursor.lastrowid)
            self._replace_permissions(connection, user_id, granted)
            self._append_audit(
                connection,
                actor_id=actor_id,
                action="user.create",
                target=f"user:{user_id}",
                details={"username": normalized_username, "role": role, "permissions": sorted(granted)},
            )
            return User(user_id, normalized_username, role, True)

    def grant_permissions(
        self,
        actor_id: int,
        target_user_id: int,
        permissions: Iterable[str],
    ) -> frozenset[str]:
        granted = self._validate_permissions(permissions)
        with self._transaction() as connection:
            actor = self._get_user_row(connection, actor_id)
            target = self._get_user_row(connection, target_user_id)
            self._require_permission(connection, actor, "manage_users")
            if target["role"] == ROOT_ROLE:
                raise AuthorizationError("root permissions cannot be delegated or changed")
            if actor["role"] == ADMIN_ROLE and target["role"] != USER_ROLE:
                raise AuthorizationError("admins may grant permissions to users only")
            self._require_delegable(connection, actor, granted)
            self._replace_permissions(connection, target_user_id, granted)
            self._append_audit(
                connection,
                actor_id=actor_id,
                action="permissions.replace",
                target=f"user:{target_user_id}",
                details={"role": target["role"], "permissions": sorted(granted)},
            )
        return granted

    def list_users(self, actor_id: int) -> list[dict[str, Any]]:
        with self._lock:
            actor = self._get_user_row(self._connection, actor_id)
            self._require_permission(self._connection, actor, "manage_users")
            rows = self._connection.execute(
                "SELECT user_id, username, role, enabled FROM users ORDER BY username"
            ).fetchall()
        users: list[dict[str, Any]] = []
        for row in rows:
            permissions = self._permissions_for_row(self._connection, row)
            users.append(
                {
                    "user_id": int(row["user_id"]),
                    "username": row["username"],
                    "role": row["role"],
                    "enabled": bool(row["enabled"]),
                    "permissions": sorted(permissions),
                }
            )
        return users

    def effective_permissions(self, user_id: int) -> frozenset[str]:
        with self._lock:
            user = self._get_user_row(self._connection, user_id)
            if user["role"] == ROOT_ROLE:
                return KNOWN_PERMISSIONS
            rows = self._connection.execute(
                "SELECT permission FROM user_permissions WHERE user_id = ? ORDER BY permission",
                (user_id,),
            ).fetchall()
        return frozenset(row["permission"] for row in rows)

    def authorize(self, user_id: int, permission: str) -> None:
        if permission not in KNOWN_PERMISSIONS:
            raise StateValidationError(f"unknown permission {permission!r}")
        if permission not in self.effective_permissions(user_id):
            raise AuthorizationError(f"user lacks permission {permission!r}")

    def set_setting(
        self,
        actor_id: int,
        key: str,
        value: Any,
        *,
        permission: str = "configure_ups",
    ) -> None:
        if not key or len(key) > 128:
            raise StateValidationError("setting key must contain 1..128 characters")
        encoded = self._canonical_json(value)
        with self._transaction() as connection:
            actor = self._get_user_row(connection, actor_id)
            self._require_permission(connection, actor, permission)
            connection.execute(
                "INSERT INTO settings(key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (key, encoded, self._now(), actor_id),
            )
            self._append_audit(
                connection,
                actor_id=actor_id,
                action="setting.update",
                target=f"setting:{key}",
                details={"permission": permission},
            )

    def get_setting(
        self,
        actor_id: int,
        key: str,
        *,
        permission: str = "view_settings",
        default: Any = None,
    ) -> Any:
        with self._lock:
            actor = self._get_user_row(self._connection, actor_id)
            self._require_permission(self._connection, actor, permission)
            row = self._connection.execute(
                "SELECT value_json FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["value_json"])

    def append_audit(
        self,
        actor_id: int | None,
        action: str,
        *,
        target: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        if not action or len(action) > 128:
            raise StateValidationError("audit action must contain 1..128 characters")
        with self._transaction() as connection:
            if actor_id is not None:
                self._get_user_row(connection, actor_id)
            return self._append_audit(
                connection,
                actor_id=actor_id,
                action=action,
                target=target,
                details=details or {},
            )

    def audit_events(self, *, limit: int = 100) -> tuple[AuditEvent, ...]:
        if not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM audit_events ORDER BY event_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return tuple(self._audit_from_row(row) for row in reversed(rows))

    def verify_audit_chain(self) -> bool:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM audit_events ORDER BY event_id"
            ).fetchall()
        previous_hash = _AUDIT_GENESIS
        for row in rows:
            event = self._audit_from_row(row)
            if event.previous_hash != previous_hash:
                return False
            expected = self._calculate_audit_hash(
                event.created_at,
                event.actor_id,
                event.action,
                event.target,
                event.details,
                event.previous_hash,
            )
            if not hmac.compare_digest(event.event_hash, expected):
                return False
            previous_hash = event.event_hash
        return True

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    role TEXT NOT NULL CHECK(role IN ('root', 'admin', 'user')),
                    password_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_permissions (
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    permission TEXT NOT NULL,
                    PRIMARY KEY(user_id, permission)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by INTEGER NOT NULL REFERENCES users(user_id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor_id INTEGER REFERENCES users(user_id),
                    action TEXT NOT NULL,
                    target TEXT,
                    details_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL
                );
                """
            )

    class _Transaction:
        def __init__(self, store: StateStore) -> None:
            self.store = store

        def __enter__(self) -> sqlite3.Connection:
            self.store._lock.acquire()
            self.store._connection.execute("BEGIN IMMEDIATE")
            return self.store._connection

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
            try:
                if exc_type is None:
                    self.store._connection.execute("COMMIT")
                else:
                    self.store._connection.execute("ROLLBACK")
            finally:
                self.store._lock.release()
            return False

    def _transaction(self) -> StateStore._Transaction:
        return self._Transaction(self)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _validate_username(username: str) -> str:
        normalized = username.strip().lower()
        if not 3 <= len(normalized) <= 64:
            raise StateValidationError("username must contain 3..64 characters")
        if any(character.isspace() for character in normalized):
            raise StateValidationError("username must not contain whitespace")
        return normalized

    @staticmethod
    def _validate_permissions(permissions: Iterable[str]) -> frozenset[str]:
        result = frozenset(permissions)
        unknown = result - KNOWN_PERMISSIONS
        if unknown:
            raise StateValidationError(f"unknown permissions: {', '.join(sorted(unknown))}")
        return result

    @staticmethod
    def _hash_password(password: str) -> str:
        if len(password) < 12:
            raise StateValidationError("password must contain at least 12 characters")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_SCRYPT_LENGTH,
        )
        return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, n, r, p, salt_hex, expected_hex = encoded.split("$")
            if algorithm != "scrypt":
                return False
            digest = hashlib.scrypt(
                password.encode("utf-8"),
                salt=bytes.fromhex(salt_hex),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(bytes.fromhex(expected_hex)),
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(digest.hex(), expected_hex)

    @staticmethod
    def _canonical_json(value: Any) -> str:
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        except (TypeError, ValueError) as exc:
            raise StateValidationError("value must be JSON serializable") from exc

    @staticmethod
    def _get_user_row(connection: sqlite3.Connection, user_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT user_id, username, role, password_hash, enabled FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None or not row["enabled"]:
            raise AuthenticationError("user does not exist or is disabled")
        return row

    @classmethod
    def _permissions_for_row(cls, connection: sqlite3.Connection, user: sqlite3.Row) -> frozenset[str]:
        if user["role"] == ROOT_ROLE:
            return KNOWN_PERMISSIONS
        rows = connection.execute(
            "SELECT permission FROM user_permissions WHERE user_id = ?",
            (user["user_id"],),
        ).fetchall()
        return frozenset(row["permission"] for row in rows)

    @classmethod
    def _require_permission(
        cls,
        connection: sqlite3.Connection,
        user: sqlite3.Row,
        permission: str,
    ) -> None:
        if permission not in KNOWN_PERMISSIONS:
            raise StateValidationError(f"unknown permission {permission!r}")
        if permission not in cls._permissions_for_row(connection, user):
            raise AuthorizationError(f"user lacks permission {permission!r}")

    @classmethod
    def _require_delegable(
        cls,
        connection: sqlite3.Connection,
        actor: sqlite3.Row,
        permissions: frozenset[str],
    ) -> None:
        allowed = cls._permissions_for_row(connection, actor)
        if not permissions <= allowed:
            denied = permissions - allowed
            raise AuthorizationError(f"cannot delegate permissions: {', '.join(sorted(denied))}")

    @staticmethod
    def _replace_permissions(
        connection: sqlite3.Connection,
        user_id: int,
        permissions: frozenset[str],
    ) -> None:
        connection.execute("DELETE FROM user_permissions WHERE user_id = ?", (user_id,))
        connection.executemany(
            "INSERT INTO user_permissions(user_id, permission) VALUES (?, ?)",
            ((user_id, permission) for permission in sorted(permissions)),
        )

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        actor_id: int | None,
        action: str,
        target: str | None,
        details: dict[str, Any],
    ) -> AuditEvent:
        created_at = self._now()
        details_json = self._canonical_json(details)
        previous = connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous["event_hash"] if previous is not None else _AUDIT_GENESIS
        event_hash = self._calculate_audit_hash(
            created_at, actor_id, action, target, details, previous_hash
        )
        cursor = connection.execute(
            "INSERT INTO audit_events(created_at, actor_id, action, target, details_json, previous_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (created_at, actor_id, action, target, details_json, previous_hash, event_hash),
        )
        return AuditEvent(
            int(cursor.lastrowid),
            created_at,
            actor_id,
            action,
            target,
            details,
            previous_hash,
            event_hash,
        )

    @classmethod
    def _calculate_audit_hash(
        cls,
        created_at: str,
        actor_id: int | None,
        action: str,
        target: str | None,
        details: dict[str, Any],
        previous_hash: str,
    ) -> str:
        record = {
            "action": action,
            "actor_id": actor_id,
            "created_at": created_at,
            "details": details,
            "previous_hash": previous_hash,
            "target": target,
        }
        return hashlib.sha256(cls._canonical_json(record).encode("ascii")).hexdigest()

    @classmethod
    def _audit_from_row(cls, row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            int(row["event_id"]),
            row["created_at"],
            row["actor_id"],
            row["action"],
            row["target"],
            json.loads(row["details_json"]),
            row["previous_hash"],
            row["event_hash"],
        )