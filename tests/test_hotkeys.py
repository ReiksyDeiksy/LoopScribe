import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from recorder_app import hotkeys


class PushHotkeyTests(unittest.TestCase):
    def setUp(self):
        self.manager = hotkeys.GlobalHotkeyManager()
        self.events = []
        self.manager.push_pressed.connect(lambda: self.events.append("down"))
        self.manager.push_released.connect(lambda: self.events.append("up"))
        with patch.object(hotkeys.keyboard, "parse_hotkey", return_value=(((29, 285), (56, 312), (57,)),)), \
             patch.object(hotkeys.keyboard, "hook", return_value=None):
            self.manager.update_hotkeys("", "", "ctrl+alt+space")

    def event(self, code, kind):
        # Localized names must not affect matching physical keys.
        self.manager._handle_push_event(SimpleNamespace(scan_code=code, event_type=kind, name="localized"))

    def test_repeated_space_while_modifiers_stay_held(self):
        for code in (29, 56, 57, 57):
            self.event(code, "down")
        self.event(57, "up")
        self.manager.reset_push_state()
        self.event(57, "down")
        self.event(57, "up")
        self.assertEqual(self.events, ["down", "up", "down", "up"])

    def test_both_control_keys_and_releasing_any_required_key(self):
        for code in (29, 285, 56, 57):
            self.event(code, "down")
        self.event(29, "up")
        self.assertEqual(self.events, ["down"])
        self.event(56, "up")
        self.assertEqual(self.events, ["down", "up"])
        self.event(57, "up")
        self.manager.unregister_all()
        self.assertEqual(self.events, ["down", "up"])

    def test_unregister_active_push_emits_one_release(self):
        for code in (312, 285, 57):
            self.event(code, "down")
        self.manager.unregister_all()
        self.manager.unregister_all()
        self.assertEqual(self.events, ["down", "up"])


if __name__ == "__main__":
    unittest.main()
