import tempfile
import unittest
from pathlib import Path

from bicker_control.state_store import (
    ADMIN_ROLE,
    ROOT_ROLE,
    USER_ROLE,
    AuthenticationError,
    AuthorizationError,
    BootstrapError,
    StateStore,
    StateValidationError,
)


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "state.sqlite3"
        self.store = StateStore(self.database)
        self.root = self.store.create_initial_root("root-owner", "root-password-long-1")

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_root_bootstrap_is_one_time_and_password_is_verified(self) -> None:
        self.assertEqual(self.root.role, ROOT_ROLE)
        self.assertEqual(self.store.authenticate("ROOT-OWNER", "root-password-long-1"), self.root)
        with self.assertRaises(AuthenticationError):
            self.store.authenticate("root-owner", "wrong-password-long")
        with self.assertRaises(BootstrapError):
            self.store.create_initial_root("second-root", "another-password-long-1")

    def test_root_must_grant_admin_capabilities_after_creation(self) -> None:
        with self.assertRaises(StateValidationError):
            self.store.create_user(
                self.root.user_id,
                "premature-admin",
                ADMIN_ROLE,
                "premature-admin-password-1",
                {"manage_users"},
            )

        admin = self.store.create_user(
            self.root.user_id,
            "customer-admin",
            ADMIN_ROLE,
            "admin-password-long-1",
        )
        self.assertEqual(self.store.effective_permissions(admin.user_id), frozenset())
        self.store.grant_permissions(self.root.user_id, admin.user_id, {"manage_users"})
        self.assertEqual(self.store.effective_permissions(admin.user_id), {"manage_users"})

    def test_admin_can_only_delegate_subset_of_root_grants(self) -> None:
        admin = self.store.create_user(
            self.root.user_id,
            "customer-admin",
            ADMIN_ROLE,
            "admin-password-long-1",
        )
        self.assertEqual(self.store.effective_permissions(admin.user_id), frozenset())
        self.store.grant_permissions(
            self.root.user_id,
            admin.user_id,
            {"manage_users", "view_status", "configure_inputs"},
        )
        user = self.store.create_user(
            admin.user_id,
            "customer-user",
            USER_ROLE,
            "user-password-long-1",
            {"view_status"},
        )
        self.assertEqual(self.store.effective_permissions(user.user_id), {"view_status"})

        with self.assertRaises(AuthorizationError):
            self.store.grant_permissions(admin.user_id, user.user_id, {"configure_network"})
        with self.assertRaises(AuthorizationError):
            self.store.create_user(
                admin.user_id,
                "second-admin",
                ADMIN_ROLE,
                "admin-password-long-2",
                {"view_status"},
            )

    def test_settings_require_capability_and_survive_reopen(self) -> None:
        admin = self.store.create_user(
            self.root.user_id,
            "network-admin",
            ADMIN_ROLE,
            "network-admin-password-1",
        )
        self.store.grant_permissions(
            self.root.user_id,
            admin.user_id,
            {"configure_network", "view_settings"},
        )
        self.store.set_setting(
            admin.user_id,
            "network.ipv4.mode",
            {"mode": "dhcp"},
            permission="configure_network",
        )
        self.assertEqual(
            self.store.get_setting(admin.user_id, "network.ipv4.mode"),
            {"mode": "dhcp"},
        )

        self.store.close()
        self.store = StateStore(self.database)
        self.assertEqual(
            self.store.get_setting(self.root.user_id, "network.ipv4.mode"),
            {"mode": "dhcp"},
        )

    def test_settings_reject_unauthorized_user(self) -> None:
        user = self.store.create_user(
            self.root.user_id,
            "readonly-user",
            USER_ROLE,
            "readonly-password-long-1",
            {"view_status"},
        )
        with self.assertRaises(AuthorizationError):
            self.store.set_setting(user.user_id, "ups.backup_time", 30)

    def test_audit_chain_detects_record_modification(self) -> None:
        self.store.set_setting(self.root.user_id, "ups.mode", "auto")
        self.assertTrue(self.store.verify_audit_chain())
        events = self.store.audit_events()
        self.assertEqual([event.action for event in events], ["root.bootstrap", "setting.update"])

        self.store._connection.execute(
            "UPDATE audit_events SET details_json = ? WHERE event_id = ?",
            ('{"changed":true}', events[-1].event_id),
        )
        self.assertFalse(self.store.verify_audit_chain())

    def test_usernames_are_normalized(self) -> None:
        admin = self.store.create_user(
            self.root.user_id,
            "MixedCaseAdmin",
            ADMIN_ROLE,
            "normalized-admin-password-1",
        )
        self.store.grant_permissions(self.root.user_id, admin.user_id, {"view_status"})
        self.assertEqual(admin.username, "mixedcaseadmin")
        self.assertEqual(self.store.authenticate("MIXEDCASEADMIN", "normalized-admin-password-1"), admin)


if __name__ == "__main__":
    unittest.main()