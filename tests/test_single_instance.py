import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from uuid import uuid4


class SingleInstanceTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("SCRIBE_TEST_LOCAL_IPC") == "1",
                         "Opt in outside sandboxes that block per-user local IPC")
    def test_second_launch_activates_first_and_exits(self):
        root = Path(__file__).resolve().parents[1]
        state = root / "output" / f"instance-test-{uuid4().hex}"
        state.mkdir()
        script = '''
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
from recorder_app import main as entry
state = Path(sys.argv[1])
entry.application_state_directory = lambda: state
class TestWindow(QWidget):
    def __init__(self, _store):
        super().__init__()
        QTimer.singleShot(0, lambda: (state / "ready").write_text("ready"))
        QTimer.singleShot(7000, QApplication.instance().quit)
    def showNormal(self):
        (state / "activated").write_text("activated")
        super().showNormal()
        QTimer.singleShot(100, QApplication.instance().quit)
entry.MainWindow = TestWindow
raise SystemExit(entry.main())
'''
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(root / "src")}
        command = [sys.executable, "-c", script, str(state)]
        first = subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (state / "ready").exists() and time.monotonic() < deadline and first.poll() is None:
                time.sleep(0.02)
            self.assertTrue((state / "ready").exists(), "First instance did not become ready")
            second = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=5)
            self.assertEqual(second.returncode, 0, second.stderr.decode(errors="replace"))
            _, errors = first.communicate(timeout=5)
            self.assertEqual(first.returncode, 0, errors.decode(errors="replace"))
            self.assertTrue((state / "activated").exists(), "Existing window was not activated")
        finally:
            if first.poll() is None:
                first.terminate()
                first.communicate(timeout=5)
            for name in ("ready", "activated", "scribe.lock"):
                (state / name).unlink(missing_ok=True)
            state.rmdir()


if __name__ == "__main__":
    unittest.main()
