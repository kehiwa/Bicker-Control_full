"""Policy-aware commands for UPS output and backup-time settings."""

from __future__ import annotations

from .protocol import (
    CMD_STATUS_FLAGS,
    CMD_UPS_OUTPUT,
    INDEX_GENERIC,
    PARAM_MAX_BACKUP_TIME,
    Frame,
    Parameter,
    StatusFlag,
    parse_status_flags,
    ups_output_frame,
)
from .transport import UpsPreconditionFailed, UpsSerialService


class UpsCommandService:
    """Expose documented UPS actions with root-defined safety limits."""

    def __init__(
        self,
        serial: UpsSerialService,
        *,
        max_restart_delay_seconds: int = 254,
        allow_shutdown_on_battery: bool = False,
    ) -> None:
        if not 1 <= max_restart_delay_seconds <= 254:
            raise ValueError("max_restart_delay_seconds must be between 1 and 254")
        self._serial = serial
        self._max_restart_delay_seconds = max_restart_delay_seconds
        self._allow_shutdown_on_battery = allow_shutdown_on_battery

    async def set_backup_time_profile(
        self,
        *,
        enabled: bool,
        seconds: int,
        root_max_seconds: int,
    ) -> Parameter:
        """Set Maximum Backup Time without exceeding root's configured ceiling."""
        if not 1 <= root_max_seconds <= 0xFFFF:
            raise ValueError("root_max_seconds must be between 1 and 65535")
        if not 1 <= seconds <= min(root_max_seconds, 0xFFFF):
            raise UpsPreconditionFailed(
                f"backup time must be between 1 and {root_max_seconds} seconds"
            )

        current = await self._serial.get_parameter(PARAM_MAX_BACKUP_TIME)
        if not current.minimum <= seconds <= current.maximum:
            raise UpsPreconditionFailed(
                f"UPS permits backup time {current.minimum}..{current.maximum} seconds"
            )
        return await self._serial.set_parameter_verified(
            PARAM_MAX_BACKUP_TIME,
            enabled=enabled,
            value=seconds,
        )

    async def shutdown_output(self) -> bytes:
        """Turn off the UPS output; battery shutdown requires root opt-in."""
        return await self._guarded_output_request(ups_output_frame(0), require_mains=False)

    async def restart_output(self, delay_seconds: int) -> bytes:
        """Request UPS output off and timed restart, only with fresh mains status."""
        if not 1 <= delay_seconds <= self._max_restart_delay_seconds:
            raise UpsPreconditionFailed(
                "restart delay exceeds the root-configured range "
                f"1..{self._max_restart_delay_seconds} seconds"
            )
        return await self._guarded_output_request(ups_output_frame(delay_seconds), require_mains=True)

    async def turn_output_on(self) -> bytes:
        """Send the documented UPS output-on value."""
        return await self._serial.request(INDEX_GENERIC, CMD_UPS_OUTPUT, b"\xff")

    async def _guarded_output_request(self, request: Frame, *, require_mains: bool) -> bytes:
        guard = self._mains_guard if require_mains else self._allow_shutdown_guard
        responses = await self._serial.request_many_guarded(
            (Frame(INDEX_GENERIC, CMD_STATUS_FLAGS), request),
            guard_before_last=guard,
        )
        return responses[-1]

    def _mains_guard(self, previous_responses: tuple[bytes, ...]) -> None:
        flags = self._status_from_guard(previous_responses)
        if not flags & StatusFlag.POWER_PRESENT:
            raise UpsPreconditionFailed("UPS output restart is allowed only on mains power")

    def _allow_shutdown_guard(self, previous_responses: tuple[bytes, ...]) -> None:
        flags = self._status_from_guard(previous_responses)
        if not flags & StatusFlag.POWER_PRESENT and not self._allow_shutdown_on_battery:
            raise UpsPreconditionFailed("UPS output shutdown on battery is disabled by root policy")

    @staticmethod
    def _status_from_guard(previous_responses: tuple[bytes, ...]) -> StatusFlag:
        if not previous_responses:
            raise UpsPreconditionFailed("UPS status precondition is missing")
        return parse_status_flags(previous_responses[-1])