"""Network configuration adapter with a safe dry-run backend for development."""

from __future__ import annotations

import asyncio
import ipaddress
import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    ethernet_mode: str = "dhcp"
    ethernet_address: str | None = None
    ethernet_gateway: str | None = None
    wifi_ssid: str | None = None
    wifi_password: str | None = None

    def __post_init__(self) -> None:
        if self.ethernet_mode not in {"dhcp", "static"}:
            raise ValueError("ethernet mode must be dhcp or static")
        if self.ethernet_mode == "static" and not self.ethernet_address:
            raise ValueError("static ethernet mode requires an address")
        if self.ethernet_address:
            ipaddress.ip_interface(self.ethernet_address)
        if self.ethernet_gateway:
            ipaddress.ip_address(self.ethernet_gateway)
        if self.wifi_ssid is not None and not self.wifi_ssid.strip():
            raise ValueError("Wi-Fi SSID must not be empty")


class NetworkBackend(Protocol):
    def apply(self, config: NetworkConfig) -> None: ...


class MemoryNetworkBackend:
    def __init__(self) -> None:
        self.current: NetworkConfig | None = None

    def apply(self, config: NetworkConfig) -> None:
        self.current = config


class NmcliBackend:
    """Apply network settings through NetworkManager without shell interpolation."""

    def __init__(self, *, ethernet_connection: str = "Wired connection 1", wifi_connection: str = "bicker-control") -> None:
        self._ethernet_connection = ethernet_connection
        self._wifi_connection = wifi_connection

    def apply(self, config: NetworkConfig) -> None:
        ethernet = self._ethernet_connection
        self._run("connection", "modify", ethernet, "ipv4.method", config.ethernet_mode)
        if config.ethernet_mode == "static":
            self._run("connection", "modify", ethernet, "ipv4.addresses", config.ethernet_address or "")
            if config.ethernet_gateway:
                self._run("connection", "modify", ethernet, "ipv4.gateway", config.ethernet_gateway)
        else:
            self._run("connection", "modify", ethernet, "ipv4.addresses", "", "ipv4.gateway", "")
        self._run("connection", "up", ethernet)
        if config.wifi_ssid is not None:
            self._run("device", "wifi", "connect", config.wifi_ssid, "password", config.wifi_password or "", "name", self._wifi_connection)

    @staticmethod
    def _run(*args: str) -> None:
        subprocess.run(("nmcli", *args), check=True, capture_output=True, text=True)


class NetworkService:
    def __init__(self, backend: NetworkBackend) -> None:
        self._backend = backend

    async def apply(self, config: NetworkConfig) -> None:
        await asyncio.to_thread(self._backend.apply, config)