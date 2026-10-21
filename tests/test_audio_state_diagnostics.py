from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recorder_app.audio import (  # noqa: E402
    AppAudioStateMonitor,
    ApplicationAudioCapability,
    ApplicationAudioSessionInfo,
    AudioEndpointStateSnapshot,
    AudioSystemStateSnapshot,
    DualSourceAudioCaptureService,
)
from recorder_app.config import RECORDING_SOURCE_APPLICATION_AUDIO  # noqa: E402


def make_state(volume: float, muted: bool = False) -> AudioSystemStateSnapshot:
    return AudioSystemStateSnapshot(
        captured_at=datetime.now(),
        default_render=AudioEndpointStateSnapshot(
            flow="render",
            role="console",
            endpoint_id="render-1",
            master_volume_scalar=0.8,
            muted=False,
        ),
        default_capture=AudioEndpointStateSnapshot(
            flow="capture",
            role="console",
            endpoint_id="mic-1",
            master_volume_scalar=volume,
            muted=muted,
        ),
    )


class FakeAudioStateReader:
    def __init__(self, snapshots: list[AudioSystemStateSnapshot]) -> None:
        self._snapshots = list(snapshots)
        self.calls = 0

    def read_default_state(self) -> AudioSystemStateSnapshot:
        self.calls += 1
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]


class AudioStateDiagnosticsTests(unittest.TestCase):
    def test_microphone_volume_change_is_detected(self) -> None:
        before = make_state(0.72)
        after = make_state(0.40)

        self.assertTrue(after.default_capture.has_level_or_mute_change(before.default_capture))

    def test_monitor_logs_unrequested_microphone_state_change(self) -> None:
        reader = FakeAudioStateReader([make_state(0.72), make_state(0.40), make_state(0.40)])
        monitor = AppAudioStateMonitor(reader)  # type: ignore[arg-type]

        with self.assertLogs("recorder_app.audio", level="WARNING") as captured:
            monitor.start("Browser (chrome, PID 123, aktiv)")
            monitor.poll()
            monitor.finish()

        self.assertIn("System-Mikrofonzustand hat sich", "\n".join(captured.output))
        self.assertGreaterEqual(reader.calls, 2)

    def test_app_audio_start_does_not_resolve_microphone_device(self) -> None:
        class AppAudioOnlyHarness(DualSourceAudioCaptureService):
            def __init__(self) -> None:
                super().__init__(audio_state_reader=FakeAudioStateReader([make_state(0.7)]))  # type: ignore[arg-type]
                self.microphone_resolved = False

            def application_audio_capability(self, *, force_refresh: bool = False):
                return ApplicationAudioCapability(supported=True, reason="test")

            def list_application_audio_sessions(self, *, force_refresh: bool = False):
                return [
                    ApplicationAudioSessionInfo(
                        id="123|browser.exe",
                        pid=123,
                        process_name="browser",
                        display_name="Browser",
                        executable_path="browser.exe",
                        state="Active",
                    )
                ]

            def _resolve_input_microphone(self, device_id: str):
                self.microphone_resolved = True
                raise AssertionError("App-Audio-only darf kein Mikrofon initialisieren.")

            def _capture_loop(self) -> None:
                if self._app_audio_ready_event is not None:
                    self._app_audio_ready_event.set()
                self._last_finished_at = datetime.now()

        class Settings:
            recording_source = RECORDING_SOURCE_APPLICATION_AUDIO
            audio_device_id = ""
            microphone_device_id = "mic-that-must-not-open"
            application_audio_session_id = "123|browser.exe"

            def recordings_path(self) -> Path:
                return ROOT

        service = AppAudioOnlyHarness()

        with patch("recorder_app.audio.sys.platform", "win32"):
            service.start_recording(Settings())  # type: ignore[arg-type]

        if service._thread is not None:
            service._thread.join(timeout=1.0)

        self.assertFalse(service.microphone_resolved)


if __name__ == "__main__":
    unittest.main()
