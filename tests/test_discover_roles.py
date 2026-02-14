import sys
import types
import unittest


# Minimal stubs to import app.core/app.discover without external runtime deps.
pythonosc = types.ModuleType("pythonosc")
pythonosc.dispatcher = types.SimpleNamespace(Dispatcher=object)
pythonosc.osc_server = types.SimpleNamespace(ThreadingOSCUDPServer=object)
pythonosc.udp_client = types.SimpleNamespace(SimpleUDPClient=object)
osc_builder = types.ModuleType("pythonosc.osc_message_builder")
osc_builder.OscMessageBuilder = object

sys.modules.setdefault("pythonosc", pythonosc)
sys.modules.setdefault("pythonosc.dispatcher", pythonosc.dispatcher)
sys.modules.setdefault("pythonosc.osc_server", pythonosc.osc_server)
sys.modules.setdefault("pythonosc.udp_client", pythonosc.udp_client)
sys.modules.setdefault("pythonosc.osc_message_builder", osc_builder)

from app import discover


class DecideRolesTests(unittest.TestCase):
    def test_selects_complete_main_backup_pair(self):
        responders = [
            ("10.0.0.1", {"show_main": "main-id"}),
            ("10.0.0.2", {"show_backup": "backup-id"}),
        ]
        assigned = discover.decide_roles(responders)
        self.assertEqual(assigned["main"].ip, "10.0.0.1")
        self.assertEqual(assigned["backup"].ip, "10.0.0.2")

    def test_rejects_duplicate_role_candidate(self):
        responders = [
            ("10.0.0.1", {"show_main": "id-1"}),
            ("10.0.0.2", {"show_main": "id-2"}),
        ]
        with self.assertRaises(discover.ConflictError):
            discover.decide_roles(responders)

    def test_plain_workspace_fallback(self):
        responders = [("10.0.0.3", {"ShowUnique": "main-id"})]
        assigned = discover.decide_roles(responders)
        self.assertEqual(assigned["main"].workspace_name, "ShowUnique")
        self.assertNotIn("backup", assigned)


if __name__ == "__main__":
    unittest.main()
