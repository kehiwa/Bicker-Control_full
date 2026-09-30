"""Shared authorization facade and HTTP API for web and future SNMP adapters."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .state_store import (
    ADMIN_ROLE,
    ROOT_ROLE,
    USER_ROLE,
    AuthenticationError,
    AuthorizationError,
    BootstrapError,
    StateStore,
    StateStoreError,
    StateValidationError,
    User,
)
from .ups.commands import UpsCommandService
from .ups.monitor import UpsMonitor, UpsSnapshot
from .network import NetworkConfig, NetworkService
from .device_io import InputAction

RoleName = Literal["admin", "user"]

_SETTING_PERMISSIONS = {
    "inputs": "configure_inputs",
    "network": "configure_network",
    "snmp": "configure_snmp",
    "ups": "configure_ups",
}


@dataclass(frozen=True, slots=True)
class AuthSession:
    token: str
    user: User
    expires_at: float


class SessionRegistry:
    """In-memory bearer sessions; reboot revokes every active session."""

    def __init__(self, store: StateStore, *, session_ttl_seconds: int = 8 * 60 * 60) -> None:
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")
        self._store = store
        self._ttl = session_ttl_seconds
        self._sessions: dict[str, AuthSession] = {}
        self._lock = threading.RLock()

    def login(self, username: str, password: str) -> AuthSession:
        user = self._store.authenticate(username, password)
        token = secrets.token_urlsafe(32)
        session = AuthSession(token, user, time.monotonic() + self._ttl)
        with self._lock:
            self._sessions[token] = session
        self._store.append_audit(user.user_id, "auth.login", target=f"user:{user.user_id}")
        return session

    def resolve(self, token: str) -> User:
        now = time.monotonic()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise AuthenticationError("invalid or expired session")
            if session.expires_at <= now:
                del self._sessions[token]
                raise AuthenticationError("invalid or expired session")
        # Reload enabled state and role from persistent storage on each request.
        return self._store.get_user(session.user.user_id)

    def logout(self, token: str) -> None:
        with self._lock:
            session = self._sessions.pop(token, None)
        if session is not None:
            self._store.append_audit(
                session.user.user_id,
                "auth.logout",
                target=f"user:{session.user.user_id}",
            )


class DevicePolicy:
    """Shared domain authorization for web/API and future SNMP AgentX calls."""

    def __init__(
        self,
        store: StateStore,
        monitor: UpsMonitor,
        commands: UpsCommandService,
        network: NetworkService | None = None,
        device: Any | None = None,
    ) -> None:
        self.store = store
        self.monitor = monitor
        self.commands = commands
        self.network = network
        self.device = device

    def status(self, actor: User) -> dict[str, Any]:
        self.store.authorize(actor.user_id, "view_status")
        return self.serialize_snapshot(self.monitor.snapshot())

    @staticmethod
    def serialize_snapshot(snapshot: UpsSnapshot) -> dict[str, Any]:
        now = time.monotonic()
        measurements = {
            name: {
                "value": item.value,
                "unit": item.unit,
                "age_seconds": max(0.0, now - item.observed_at),
            }
            for name, item in snapshot.measurements.items()
        }
        status_age = (
            None
            if snapshot.status_observed_at is None
            else max(0.0, now - snapshot.status_observed_at)
        )
        return {
            "status_valid": snapshot.status_valid,
            "status_age_seconds": status_age,
            "status_fresh": snapshot.is_status_fresh(2.0, now=now),
            "communication_ok": snapshot.communication_ok,
            "consecutive_status_failures": snapshot.consecutive_status_failures,
            "flags": int(snapshot.flags),
            "on_battery": snapshot.on_battery,
            "measurements": measurements,
            "communication_error": snapshot.communication_error,
            "telemetry_error": snapshot.telemetry_error,
        }

    def get_setting(self, actor: User, key: str) -> Any:
        self.store.authorize(actor.user_id, "view_settings")
        return self.store.get_setting(actor.user_id, key, permission="view_settings")

    def set_setting(self, actor: User, key: str, value: Any) -> None:
        permission = self._setting_permission(actor, key)
        self.store.set_setting(actor.user_id, key, value, permission=permission)
        self.store.append_audit(
            actor.user_id,
            "api.setting.update",
            target=f"setting:{key}",
            details={"permission": permission},
        )

    async def set_network(self, actor: User, value: dict[str, Any]) -> dict[str, Any]:
        self.store.authorize(actor.user_id, "configure_network")
        config = NetworkConfig(**value)
        if self.network is None:
            raise StateValidationError("network backend is not configured")
        await self.network.apply(config)
        self.store.set_setting(actor.user_id, "network.config", value, permission="configure_network")
        self.store.append_event("network.configuration.applied", source="network", details=value)
        return value

    def input_configuration(self, actor: User) -> list[dict[str, Any]]:
        self.store.authorize(actor.user_id, "view_settings")
        if self.device is None:
            return []
        return self.device.input_configuration()

    def configure_input(self, actor: User, name: str, value: dict[str, Any]) -> dict[str, Any]:
        self.store.authorize(actor.user_id, "configure_inputs")
        action = InputAction(value.get("action", InputAction.NONE.value))
        active_high = bool(value.get("active_high", True))
        if self.device is None:
            raise StateValidationError("device runtime is not configured")
        self.device.configure_input(name, action, active_high=active_high)
        self.store.set_setting(
            actor.user_id,
            f"inputs.{name}",
            {"action": action.value, "active_high": active_high},
            permission="configure_inputs",
        )
        self.store.append_event(
            "input.configuration.updated",
            source=f"api.inputs.{name}",
            details={"action": action.value, "active_high": active_high},
        )
        return next(item for item in self.device.input_configuration() if item["name"] == name)

    def list_users(self, actor: User) -> list[dict[str, Any]]:
        self.store.authorize(actor.user_id, "manage_users")
        return self.store.list_users(actor.user_id)

    def list_events(self, actor: User, limit: int = 100) -> list[dict[str, Any]]:
        self.store.authorize(actor.user_id, "view_logs")
        return [
            {
                "event_id": event.event_id,
                "created_at": event.created_at,
                "event_type": event.event_type,
                "severity": event.severity,
                "source": event.source,
                "details": event.details,
            }
            for event in self.store.device_events(limit=limit)
        ]

    def create_user(
        self,
        actor: User,
        *,
        username: str,
        role: str,
        password: str,
        permissions: list[str],
    ) -> User:
        self.store.authorize(actor.user_id, "manage_users")
        return self.store.create_user(
            actor.user_id,
            username,
            role,
            password,
            permissions,
        )

    def grant_permissions(self, actor: User, user_id: int, permissions: list[str]) -> list[str]:
        granted = self.store.grant_permissions(actor.user_id, user_id, permissions)
        return sorted(granted)

    def grant_admin_capabilities(self, actor: User, admin_id: int, permissions: list[str]) -> list[str]:
        self.store.authorize(actor.user_id, "manage_users")
        target = self.store.get_user(admin_id)
        if actor.role != ROOT_ROLE or target.role != ADMIN_ROLE:
            raise AuthorizationError("only root can assign capabilities to an admin")
        return self.grant_permissions(actor, admin_id, permissions)

    async def restart_output(self, actor: User, delay_seconds: int) -> None:
        self.store.authorize(actor.user_id, "ups_restart")
        await self.commands.restart_output(delay_seconds)
        self.store.append_audit(
            actor.user_id,
            "ups.output.restart",
            target="ups:output",
            details={"delay_seconds": delay_seconds},
        )

    async def shutdown_output(self, actor: User) -> None:
        self.store.authorize(actor.user_id, "ups_shutdown")
        await self.commands.shutdown_output()
        self.store.append_audit(actor.user_id, "ups.output.shutdown", target="ups:output")

    async def turn_output_on(self, actor: User) -> None:
        self.store.authorize(actor.user_id, "ups_restart")
        await self.commands.turn_output_on()
        self.store.append_audit(actor.user_id, "ups.output.on", target="ups:output")

    def _setting_permission(self, actor: User, key: str) -> str:
        if not key or len(key) > 128:
            raise StateValidationError("setting key must contain 1..128 characters")
        namespace = key.split(".", maxsplit=1)[0]
        permission = _SETTING_PERMISSIONS.get(namespace)
        if permission is None:
            if actor.role == ROOT_ROLE:
                return "manage_users"
            raise AuthorizationError("this setting namespace is root-managed")
        return permission


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class BootstrapRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=1024)


class SettingUpdate(BaseModel):
    value: Any


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    role: RoleName
    password: str = Field(min_length=12, max_length=1024)
    permissions: list[str] = Field(default_factory=list)


class PermissionUpdate(BaseModel):
    permissions: list[str]


class RestartRequest(BaseModel):
    delay_seconds: int = Field(ge=1, le=254)


def create_app(
    store: StateStore,
    monitor: UpsMonitor,
    commands: UpsCommandService,
    *,
    session_ttl_seconds: int = 8 * 60 * 60,
    network: NetworkService | None = None,
    device: Any | None = None,
) -> FastAPI:
    """Create the HTTP adapter around a shared policy service."""
    app = FastAPI(title="Bicker Control", version="0.1.0")
    policy = DevicePolicy(store, monitor, commands, network, device)
    sessions = SessionRegistry(store, session_ttl_seconds=session_ttl_seconds)
    bearer = HTTPBearer(auto_error=False)
    app.state.policy = policy
    app.state.sessions = sessions

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> User:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")
        try:
            return sessions.resolve(credentials.credentials)
        except AuthenticationError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    @app.exception_handler(StateStoreError)
    async def state_error_handler(request: object, exc: StateStoreError) -> Any:
        if isinstance(exc, AuthenticationError):
            code = status.HTTP_401_UNAUTHORIZED
        elif isinstance(exc, AuthorizationError):
            code = status.HTTP_403_FORBIDDEN
        elif isinstance(exc, BootstrapError):
            code = status.HTTP_409_CONFLICT
        elif isinstance(exc, StateValidationError):
            code = status.HTTP_422_UNPROCESSABLE_ENTITY
        else:
            code = status.HTTP_400_BAD_REQUEST
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=code, content={"detail": str(exc)})

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.post("/api/v1/bootstrap")
    def bootstrap(body: BootstrapRequest) -> dict[str, Any]:
        if store.is_bootstrapped():
            raise HTTPException(status.HTTP_409_CONFLICT, "device is already initialized")
        user = store.create_initial_root(body.username, body.password)
        session = sessions.login(user.username, body.password)
        return {"access_token": session.token, "token_type": "bearer", "role": user.role}

    @app.post("/api/v1/auth/login")
    def login(body: LoginRequest) -> dict[str, Any]:
        try:
            session = sessions.login(body.username, body.password)
        except AuthenticationError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password") from exc
        return {
            "access_token": session.token,
            "token_type": "bearer",
            "expires_in": session.expires_at - time.monotonic(),
            "role": session.user.role,
        }

    @app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        actor: User = Depends(current_user),
    ) -> None:
        del actor
        if credentials is not None:
            sessions.logout(credentials.credentials)

    @app.get("/api/v1/status")
    def get_status(actor: User = Depends(current_user)) -> dict[str, Any]:
        return policy.status(actor)

    @app.get("/api/v1/users")
    def get_users(actor: User = Depends(current_user)) -> list[dict[str, Any]]:
        return policy.list_users(actor)

    @app.get("/api/v1/events")
    def get_events(limit: int = 100, actor: User = Depends(current_user)) -> list[dict[str, Any]]:
        return policy.list_events(actor, limit)

    @app.get("/api/v1/settings/{key:path}")
    def get_setting(key: str, actor: User = Depends(current_user)) -> dict[str, Any]:
        return {"key": key, "value": policy.get_setting(actor, key)}

    @app.put("/api/v1/settings/{key:path}")
    def update_setting(
        key: str,
        body: SettingUpdate,
        actor: User = Depends(current_user),
    ) -> dict[str, Any]:
        policy.set_setting(actor, key, body.value)
        return {"key": key, "value": body.value}

    @app.put("/api/v1/network")
    async def update_network(
        body: dict[str, Any],
        actor: User = Depends(current_user),
    ) -> dict[str, Any]:
        return await policy.set_network(actor, body)

    @app.get("/api/v1/inputs")
    def get_inputs(actor: User = Depends(current_user)) -> list[dict[str, Any]]:
        return policy.input_configuration(actor)

    @app.put("/api/v1/inputs/{name}")
    def update_input(name: str, body: dict[str, Any], actor: User = Depends(current_user)) -> dict[str, Any]:
        return policy.configure_input(actor, name, body)

    @app.post("/api/v1/users", status_code=status.HTTP_201_CREATED)
    def create_user(body: UserCreate, actor: User = Depends(current_user)) -> dict[str, Any]:
        user = policy.create_user(
            actor,
            username=body.username,
            role=body.role,
            password=body.password,
            permissions=body.permissions,
        )
        return {"user_id": user.user_id, "username": user.username, "role": user.role}

    @app.put("/api/v1/users/{user_id}/permissions")
    def update_permissions(
        user_id: int,
        body: PermissionUpdate,
        actor: User = Depends(current_user),
    ) -> dict[str, Any]:
        return {"user_id": user_id, "permissions": policy.grant_permissions(actor, user_id, body.permissions)}

    @app.put("/api/v1/admins/{admin_id}/capabilities")
    def update_admin_capabilities(
        admin_id: int,
        body: PermissionUpdate,
        actor: User = Depends(current_user),
    ) -> dict[str, Any]:
        granted = policy.grant_admin_capabilities(actor, admin_id, body.permissions)
        return {"admin_id": admin_id, "capabilities": granted}

    @app.post("/api/v1/ups/output/restart", status_code=status.HTTP_202_ACCEPTED)
    async def restart_output(
        body: RestartRequest,
        actor: User = Depends(current_user),
    ) -> dict[str, str]:
        await policy.restart_output(actor, body.delay_seconds)
        return {"status": "restart_scheduled"}

    @app.post("/api/v1/ups/output/shutdown", status_code=status.HTTP_202_ACCEPTED)
    async def shutdown_output(actor: User = Depends(current_user)) -> dict[str, str]:
        await policy.shutdown_output(actor)
        return {"status": "output_shutdown_requested"}

    @app.post("/api/v1/ups/output/on", status_code=status.HTTP_202_ACCEPTED)
    async def turn_output_on(actor: User = Depends(current_user)) -> dict[str, str]:
        await policy.turn_output_on(actor)
        return {"status": "output_on_requested"}

    return app