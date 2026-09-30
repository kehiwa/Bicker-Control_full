"""SNMPv2c/v3 adapter backed by the shared Bicker Control policy and store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from .api import DevicePolicy
from .state_store import StateStore


@dataclass(frozen=True, slots=True)
class SnmpV3User:
    username: str
    user_id: int
    auth_key: str
    privacy_key: str


@dataclass(frozen=True, slots=True)
class SnmpConfig:
    bind_host: str = "127.0.0.1"
    port: int = 161
    community: str | None = None
    community_user_id: int | None = None
    v3_users: tuple[SnmpV3User, ...] = ()
    base_oid: tuple[int, ...] = (1, 3, 6, 1, 4, 1, 53864, 1)

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("SNMP port must be between 1 and 65535")
        if self.community is not None and self.community_user_id is None:
            raise ValueError("community_user_id is required with a community")
        for user in self.v3_users:
            if len(user.auth_key) < 8 or len(user.privacy_key) < 8:
                raise ValueError("SNMPv3 authPriv keys must contain at least 8 characters")


class SnmpAgent:
    """Small AgentX-compatible UDP agent exposing documented controller values."""

    STATUS_OID = 1
    BATTERY_SOC_OID = 2
    INPUT_VOLTAGE_OID = 3
    OUTPUT_VOLTAGE_OID = 4
    BACKUP_TIME_OID = 5

    def __init__(self, store: StateStore, policy: DevicePolicy, config: SnmpConfig) -> None:
        self._store = store
        self._policy = policy
        self._config = config
        self._engine: Any = None
        self._dispatcher_started = False
        self._security_users: dict[str, int] = {}
        self._instances: dict[str, Any] = {}

    async def start(self) -> None:
        if self._engine is not None:
            return
        from pysnmp.carrier.asyncio.dgram import udp
        from pysnmp.entity import config as snmp_config
        from pysnmp.entity import engine
        from pysnmp.entity.rfc3413 import cmdrsp, context

        self._engine = engine.SnmpEngine()
        access_subtree = self._config.base_oid[:-1]
        snmp_config.add_transport(
            self._engine,
            udp.DOMAIN_NAME,
            udp.UdpTransport().open_server_mode((self._config.bind_host, self._config.port)),
        )
        if self._config.community is not None:
            security_name = "bicker-v2c"
            snmp_config.add_v1_system(self._engine, security_name, self._config.community)
            snmp_config.add_vacm_user(
                self._engine,
                2,
                security_name,
                "noAuthNoPriv",
                readSubTree=access_subtree,
            )
            self._security_users[security_name] = self._config.community_user_id  # type: ignore[assignment]

        for user in self._config.v3_users:
            snmp_config.add_v3_user(
                self._engine,
                user.username,
                snmp_config.USM_AUTH_HMAC96_SHA,
                user.auth_key,
                snmp_config.USM_PRIV_CFB128_AES,
                user.privacy_key,
            )
            snmp_config.add_vacm_user(
                self._engine,
                3,
                user.username,
                "authPriv",
                readSubTree=access_subtree,
                writeSubTree=access_subtree,
            )
            self._security_users[user.username] = user.user_id

        snmp_context = context.SnmpContext(self._engine)
        self._register_mib(snmp_context)
        cmdrsp.GetCommandResponder(self._engine, snmp_context)
        cmdrsp.NextCommandResponder(self._engine, snmp_context)
        cmdrsp.SetCommandResponder(self._engine, snmp_context)
        self._engine.transport_dispatcher.job_started(1)
        self._dispatcher_started = True

    async def stop(self) -> None:
        if self._engine is None:
            return
        if self._dispatcher_started:
            self._engine.transport_dispatcher.job_finished(1)
            self._dispatcher_started = False
        self._engine.transport_dispatcher.close_dispatcher()
        self._engine = None

    def _register_mib(self, snmp_context: Any) -> None:
        from pysnmp.smi import builder

        mib_builder = snmp_context.get_mib_instrum().get_mib_builder()
        mib_scalar, mib_instance, integer32 = mib_builder.import_symbols(
            "SNMPv2-SMI", "MibScalar", "MibScalarInstance", "Integer32"
        )

        class DynamicInstance(mib_instance):
            def __init__(self, type_name, getter: Callable[[dict[str, Any]], int], setter=None):
                super().__init__(type_name, (0,), integer32())
                self._getter = getter
                self._setter = setter

            def readGet(self, name, val, **context):
                return name, self.syntax.clone(self._getter(context))

            def writeTest(self, name, val, **context):
                if self._setter is None:
                    from pysnmp.smi import error

                    raise error.NoAccessError(name=name)
                return name, val

            def writeCommit(self, name, val, **context):
                self._setter(self._security_name(context), int(val))
                return name, val

            @staticmethod
            def _security_name(context):
                value = context.get("securityName", "")
                return value.decode() if isinstance(value, bytes) else str(value)

        objects = (
            (self.STATUS_OID, "bickerUpsStatus", lambda snapshot: int(snapshot["flags"]), None),
            (self.BATTERY_SOC_OID, "bickerBatterySoc", lambda snapshot: int(snapshot["measurements"].get("battery_soc", {}).get("value", 0)), None),
            (self.INPUT_VOLTAGE_OID, "bickerInputVoltage", lambda snapshot: int(snapshot["measurements"].get("input_voltage", {}).get("value", 0)), None),
            (self.OUTPUT_VOLTAGE_OID, "bickerOutputVoltage", lambda snapshot: int(snapshot["measurements"].get("output_voltage", {}).get("value", 0)), None),
            (self.BACKUP_TIME_OID, "bickerBackupTime", lambda snapshot: int(self._store.get_setting(self._root_or_user(), "ups.backup_time", permission="view_settings", default=0) or 0), self._set_backup_time),
        )
        for relative_oid, symbol, getter, setter in objects:
            oid = self._config.base_oid + (relative_oid,)
            access_mode = "read-write" if setter else "read-only"
            scalar = mib_scalar(oid, integer32()).setMaxAccess(access_mode)
            instance = DynamicInstance(scalar.name, lambda context, fn=getter: fn(self._snapshot(context)), setter)
            instance.setMaxAccess(access_mode)
            self._instances[symbol] = instance
            mib_builder.export_symbols("BICKER-CONTROL-MIB", **{symbol: scalar, f"{symbol}Instance": instance})

    def _root_or_user(self) -> int:
        if self._security_users:
            return next(iter(self._security_users.values()))
        raise RuntimeError("SNMP has no configured security identity")

    def _snapshot(self, context: dict[str, Any]) -> dict[str, Any]:
        actor_id = self._security_users.get(self._security_name(context))
        if actor_id is None:
            raise PermissionError("unknown SNMP security identity")
        actor = self._store.get_user(actor_id)
        return self._policy.status(actor)

    def _set_backup_time(self, security_name: str, value: int) -> None:
        actor_id = self._security_users.get(security_name)
        if actor_id is None:
            raise PermissionError("unknown SNMP security identity")
        actor = self._store.get_user(actor_id)
        self._policy.set_setting(actor, "ups.backup_time", value)

    @staticmethod
    def _security_name(context: dict[str, Any]) -> str:
        value = context.get("securityName", "")
        return value.decode() if isinstance(value, bytes) else str(value)