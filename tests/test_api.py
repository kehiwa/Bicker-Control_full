import tempfile
import unittest
from pathlib import Path

import httpx

from bicker_control.api import create_app
from bicker_control.state_store import ADMIN_ROLE, USER_ROLE, StateStore
from bicker_control.ups.monitor import UpsMonitor


class FakeReader:
    async def request(self, index: int, command: int, data: bytes = b"") -> bytes:
        return b"\x0c" if command == 0x40 else b"\x00\x00"


class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    async def restart_output(self, delay_seconds: int) -> None:
        self.calls.append(("restart", delay_seconds))

    async def shutdown_output(self) -> None:
        self.calls.append(("shutdown", None))

    async def turn_output_on(self) -> None:
        self.calls.append(("on", None))


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "api.sqlite3")
        self.root = self.store.create_initial_root("root-owner", "root-password-long-1")
        self.monitor = UpsMonitor(FakeReader())
        self.commands = FakeCommands()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.store, self.monitor, self.commands)),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.store.close()
        self.temp_dir.cleanup()

    async def login(self, username: str, password: str) -> dict[str, str]:
        response = await self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def test_bootstrap_is_available_only_once(self) -> None:
        empty_store = StateStore(Path(self.temp_dir.name) / "fresh.sqlite3")
        fresh_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(empty_store, self.monitor, self.commands)),
            base_url="http://testserver",
        )
        try:
            first = await fresh_client.post(
                "/api/v1/bootstrap",
                json={"username": "first-root", "password": "first-root-password-1"},
            )
            second = await fresh_client.post(
                "/api/v1/bootstrap",
                json={"username": "other-root", "password": "other-root-password-1"},
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 409)
        finally:
            await fresh_client.aclose()
            empty_store.close()

    async def test_root_page_serves_frontend(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Bicker Control", response.text)

    async def test_root_can_list_users(self) -> None:
        self.store.create_user(
            self.root.user_id,
            "service-user",
            USER_ROLE,
            "service-user-password-1",
            {"view_status"},
        )
        root_headers = await self.login("root-owner", "root-password-long-1")
        response = await self.client.get("/api/v1/users", headers=root_headers)
        self.assertEqual(response.status_code, 200)
        users = response.json()
        self.assertTrue(any(user["username"] == "root-owner" for user in users))
        self.assertTrue(any(user["username"] == "service-user" for user in users))

    async def test_authentication_and_status_permission(self) -> None:
        no_token = await self.client.get("/api/v1/status")
        self.assertEqual(no_token.status_code, 401)

        root_headers = await self.login("root-owner", "root-password-long-1")
        response = await self.client.get("/api/v1/status", headers=root_headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["status_valid"] is False)

    async def test_admin_delegation_limits_user_settings(self) -> None:
        admin = self.store.create_user(
            self.root.user_id,
            "customer-admin",
            ADMIN_ROLE,
            "customer-admin-password-1",
        )
        root_headers = await self.login("root-owner", "root-password-long-1")
        grant_response = await self.client.put(
            f"/api/v1/admins/{admin.user_id}/capabilities",
            headers=root_headers,
            json={"permissions": ["manage_users", "view_status", "configure_inputs", "view_settings"]},
        )
        self.assertEqual(grant_response.status_code, 200)
        self.assertEqual(
            set(grant_response.json()["capabilities"]),
            {"manage_users", "view_status", "configure_inputs", "view_settings"},
        )

        admin_headers = await self.login(admin.username, "customer-admin-password-1")
        denied_grant = await self.client.put(
            f"/api/v1/admins/{admin.user_id}/capabilities",
            headers=admin_headers,
            json={"permissions": ["configure_network"]},
        )
        self.assertEqual(denied_grant.status_code, 403)

        user_response = await self.client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={
                "username": "customer-user",
                "role": USER_ROLE,
                "password": "customer-user-password-1",
                "permissions": ["view_status"],
            },
        )
        self.assertEqual(user_response.status_code, 201)
        user_id = user_response.json()["user_id"]

        escalation = await self.client.put(
            f"/api/v1/users/{user_id}/permissions",
            headers=admin_headers,
            json={"permissions": ["configure_network"]},
        )
        self.assertEqual(escalation.status_code, 403)

        user_headers = await self.login("customer-user", "customer-user-password-1")
        denied = await self.client.put(
            "/api/v1/settings/inputs.IN1",
            headers=user_headers,
            json={"value": {"function": "alarm"}},
        )
        self.assertEqual(denied.status_code, 403)

        accepted = await self.client.put(
            "/api/v1/settings/inputs.IN1",
            headers=admin_headers,
            json={"value": {"function": "alarm"}},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["value"]["function"], "alarm")

    async def test_restart_endpoint_uses_policy_permission(self) -> None:
        user = self.store.create_user(
            self.root.user_id,
            "readonly-user",
            USER_ROLE,
            "readonly-user-password-1",
            {"view_status"},
        )
        headers = await self.login(user.username, "readonly-user-password-1")
        response = await self.client.post(
            "/api/v1/ups/output/restart",
            headers=headers,
            json={"delay_seconds": 15},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.commands.calls, [])


if __name__ == "__main__":
    unittest.main()