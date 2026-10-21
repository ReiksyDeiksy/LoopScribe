from pathlib import Path
import sys
from contextlib import contextmanager
from uuid import uuid4
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from recorder_app.audio import DualSourceAudioCaptureService, AudioCaptureError
from recorder_app.config import AppSettings, RECORDING_SOURCE_APPLICATION_AUDIO


@contextmanager
def recording_directory():
    path = Path(__file__).resolve().parent / ("audio-test-" + uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        for file in path.iterdir():
            file.unlink()
        path.rmdir()


class AppMicrophoneTests(unittest.TestCase):
    def test_app_and_microphone_merge_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), recording_directory() as directory:
                service = DualSourceAudioCaptureService()
                service._recording_source = RECORDING_SOURCE_APPLICATION_AUDIO
                service._current_output_path = Path(directory) / "mixed.wav"
                service._app_microphone_device = SimpleNamespace(name="Mic", channels=1)
                service._app_audio_ready_event = threading.Event()
                session = SimpleNamespace(display_label="Discord", pid=123)

                class Process:
                    returncode = None
                    stdout = SimpleNamespace(readline=lambda: "READY")
                    stdin = SimpleNamespace(write=lambda value: None, flush=lambda: None)
                    def poll(self):
                        return self.returncode
                    def communicate(self, timeout):
                        self.returncode = 0
                        return "", ""
                    def kill(self):
                        self.returncode = -1

                def launch(command, **kwargs):
                    sf.write(command[command.index("--output") + 1], np.full(4800, 0.2), 48000)
                    return Process()

                def microphone(branch, start):
                    if fail:
                        branch.error = AudioCaptureError("mic unavailable")
                    else:
                        sf.write(branch.output_path, np.full(4800, 0.4), 48000)
                    branch.ready_event.set()
                    service._stop_event.set()

                with patch.object(service, "_resolve_application_audio_session", return_value=session), patch.object(service, "_capture_combined_branch_loop", side_effect=microphone), patch("recorder_app.audio.subprocess.Popen", side_effect=launch), patch("recorder_app.audio.AppAudioStateMonitor"):
                    service._capture_application_audio_loop()
                if fail:
                    self.assertIsNotNone(service._error)
                    self.assertFalse(service._current_output_path.exists())
                else:
                    self.assertIsNone(service._error)
                    samples, rate = sf.read(service._current_output_path)
                    self.assertEqual(rate, 48000)
                    self.assertEqual(len(samples), 4800)
                    self.assertTrue(np.all(samples > 0.2))
                    self.assertEqual(list(Path(directory).glob("*.wav")), [service._current_output_path])

    def test_meter_reads_latest_pcm_and_expires(self):
        service = DualSourceAudioCaptureService()
        with recording_directory() as directory:
            path = directory / "meter.wav"
            sf.write(path, np.full(4800, 0.5), 48000, subtype="PCM_16")
            size = service._poll_app_meter(path, 0)
            self.assertGreater(size, 44)
            self.assertAlmostEqual(service.audio_level, 0.5, places=3)
            self.assertEqual(service._poll_app_meter(path, size), size)
            with patch("recorder_app.audio.time.monotonic", return_value=1e20):
                self.assertEqual(service.audio_level, 0)
            self.assertEqual(service._poll_app_meter(directory / "missing.wav", 0), 0)

    def test_microphone_is_opt_in(self):
        self.assertFalse(AppSettings(output_directory=".").application_audio_include_microphone)


if __name__ == "__main__":
    unittest.main()
