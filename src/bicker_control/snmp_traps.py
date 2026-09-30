"""Best-effort SNMPv2c trap/inform forwarding for structured device events."""

from __future__ import annotations

import asyncio
import threading

_SEVERITIES = ("debug", "info", "warning", "error", "critical")

NOTIFICATION_OID = (1, 3, 6, 1, 4, 1, 53864, 2, 1)
EVENT_TYPE_OID = (1, 3, 6, 1, 4, 1, 53864, 2, 2, 0)
EVENT_SEVERITY_OID = (1, 3, 6, 1, 4, 1, 53864, 2, 3, 0)
EVENT_SOURCE_OID = (1, 3, 6, 1, 4, 1, 53864, 2, 4, 0)
_SNMP_TRAP_OID = (1, 3, 6, 1, 6, 3, 1, 1, 4, 1, 0)


class SnmpTrapSender:
    """Send enterprise traps/informs from a dedicated background event loop."""

    def __init__(
        self,
        host: str,
        port: int = 162,
        community: str = "public",
        *,
        notify_type: str = "trap",
        min_severity: str = "warning",
    ) -> None:
        if not host.strip():
            raise ValueError("trap host must not be empty")
        if not 1 <= port <= 65535:
            raise ValueError("trap port must be between 1 and 65535")
        if notify_type not in ("trap", "inform"):
            raise ValueError("notify_type must be trap or inform")
        if min_severity not in _SEVERITIES:
            raise ValueError("invalid minimum severity")
        if not community:
            raise ValueError("trap community must not be empty")
        self._host = host
        self._port = port
        self._community = community
        self._notify_type = notify_type
        self._min_severity_index = _SEVERITIES.index(min_severity)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="snmp-trap-sender", daemon=True
        )
        self._thread.start()

    def __call__(self, event_type: str, severity: str, source: str, details: dict) -> None:
        del details
        if _SEVERITIES.index(severity) < self._min_severity_index:
            return
        asyncio.run_coroutine_threadsafe(
            self._send(event_type, severity, source), self._loop
        )

    async def _send(self, event_type: str, severity: str, source: str) -> None:
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            send_notification,
        )
        from pysnmp.proto import rfc1902

        try:
            transport = await UdpTransportTarget.create(
                (self._host, self._port), timeout=1.0, retries=0
            )
            await send_notification(
                SnmpEngine(),
                CommunityData(self._community, mpModel=1),
                transport,
                ContextData(),
                self._notify_type,
                ObjectType(
                    ObjectIdentity(_SNMP_TRAP_OID),
                    rfc1902.ObjectIdentifier(NOTIFICATION_OID),
                ),
                ObjectType(ObjectIdentity(EVENT_TYPE_OID), rfc1902.OctetString(event_type)),
                ObjectType(ObjectIdentity(EVENT_SEVERITY_OID), rfc1902.OctetString(severity)),
                ObjectType(ObjectIdentity(EVENT_SOURCE_OID), rfc1902.OctetString(source)),
            )
        except Exception:
            # Forwarding must never break local event recording.
            return

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._loop.close()
