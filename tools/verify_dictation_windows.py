"""Opt-in Windows integration check using only a dedicated test text field.

No microphone is opened and no speech model is loaded. Ctrl+Shift+F24 avoids
triggering the user's normal dictation shortcut in any already running Scribe.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.pop("QT_QPA_PLATFORM", None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication, QLineEdit
from recorder_app import ui
from recorder_app.config import AppSettings, RECORDING_SOURCE_APPLICATION_AUDIO, RECORDING_SOURCE_MICROPHONE
from recorder_app.dictation import WindowsDictationInput, _Input, _KeyboardInput


def run() -> None:
    if sys.platform != "win32":
        raise RuntimeError("This integration check requires Windows.")
    app = QApplication.instance() or QApplication([])
    clipboard = app.clipboard()
    previous = ui.snapshot_native_windows_clipboard()
    native = WindowsDictationInput()
    test_clipboard_sequence = None
    expected = "Diktat-Test: Grüße, äöü und Unicode ✓."
    field = QLineEdit()
    field.setWindowTitle("scribe - isolierter Einfuegetest")
    field.resize(660, 80)
    window = None

    def wait(milliseconds):
        deadline = time.monotonic() + milliseconds / 1000
        while time.monotonic() < deadline:
            app.processEvents()
            # Yield the GIL so keyboard's Python listener threads can run.
            time.sleep(0.01)

    def keys(*items):
        inputs = (_Input * len(items))(*(
            _Input(type=1, ki=_KeyboardInput(wVk=key, wScan=native._api.MapVirtualKeyW(key, 0),
                                           dwFlags=2 if up else 0))
            for key, up in items
        ))
        sent = native._api.SendInput(len(inputs), inputs, ctypes.sizeof(_Input))
        if sent != len(inputs):
            raise RuntimeError(f"SendInput accepted {sent}/{len(inputs)} test events")
        wait(180)

    service = MagicMock()
    service.is_recording = False
    service.audio_level = 0.4
    service.list_loopback_devices.return_value = []
    service.list_microphone_devices.return_value = []
    service.list_application_audio_sessions.return_value = []
    service.application_audio_capability.return_value = ui.ApplicationAudioCapability(supported=True, reason="test")
    audio_path = ROOT / "output" / "dictation-integration-not-created.wav"

    def start(settings):
        assert settings.recording_source == RECORDING_SOURCE_MICROPHONE
        service.is_recording = True
        return audio_path

    def stop():
        service.is_recording = False
        return SimpleNamespace(file_path=audio_path, duration_seconds=1.0, capture_device="Test microphone")

    service.start_recording.side_effect = start
    service.stop_recording.side_effect = stop
    settings = AppSettings(output_directory=str(ROOT / "output"), start_hotkey="", stop_hotkey="",
                           push_to_transcribe_hotkey="ctrl+shift+f24",
                           recording_source=RECORDING_SOURCE_APPLICATION_AUDIO)
    store = MagicMock()
    store.load.return_value = settings
    try:
        with patch.object(ui, "AudioCaptureService", return_value=service), \
             patch.object(ui, "TranscriptionService"), \
             patch.object(ui, "application_state_directory", return_value=ROOT / "output"), \
             patch.object(ui.MainWindow, "_start_transcription") as transcribe:
            window = ui.MainWindow(store)
            window.showMinimized()
            field.show()
            field.raise_()
            field.activateWindow()
            native._api.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            native._api.SetForegroundWindow.restype = ctypes.c_int
            native._api.SetForegroundWindow(int(field.winId()))
            field.setFocus()
            wait(300)
            target = native.capture_target()
            if target is None or target.window != int(field.winId()):
                # A tool-launched process may not inherit foreground permission.
                # Temporarily share input queues only to activate our test field.
                api = native._api
                api.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int]
                api.AttachThreadInput.restype = ctypes.c_int
                current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
                foreground_thread = api.GetWindowThreadProcessId(api.GetForegroundWindow(), None)
                attached = api.AttachThreadInput(current_thread, foreground_thread, True)
                try:
                    field.raise_()
                    api.SetForegroundWindow(int(field.winId()))
                finally:
                    if attached:
                        api.AttachThreadInput(current_thread, foreground_thread, False)
                wait(150)
                target = native.capture_target()
            assert target is not None and target.window == int(field.winId()), (
                f"Test field is not focused: target={target}, test_pid={os.getpid()}, "
                f"test_hwnd={int(field.winId())}, bar_hwnd={int(window.dictation_bar.winId())}, active={field.isActiveWindow()}"
            )
            assert window.dictation_bar.isVisible(), "Bar vanished with minimized main window"
            native._api.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
            native._api.RegisterClipboardFormatW.restype = ctypes.c_uint
            png_format = native._api.RegisterClipboardFormatW("PNG")
            sentinel_formats = {
                13: "scribe integration clipboard sentinel\0".encode("utf-16-le"),
                png_format: (ROOT / "src" / "recorder_app" / "assets" / "scribe.png").read_bytes(),
            }
            assert ui.restore_native_windows_clipboard(sentinel_formats, int(field.winId()))
            wait(100)
            assert ui.read_native_windows_clipboard_text().text == "scribe integration clipboard sentinel"
            test_clipboard_sequence = native.clipboard_sequence()

            # keyboard deliberately filters injected events while Alt is down.
            # Physical Ctrl+Alt+Space matching is covered in test_hotkeys.py.
            keys((0x11, False), (0x10, False), (0x87, False))
            assert window.push_to_transcribe_state == ui.PushToTranscribeState.RECORDING, (
                f"Live global hotkey did not start: expected={window.hotkey_manager._push_scan_codes}, "
                f"pressed={window.hotkey_manager._pressed_scan_codes}"
            )
            assert native.capture_target() == target, "Dictation bar stole focus"
            window.dictation_bar.grab().save(str(ROOT / "output" / "dictation-bar-recording.png"))
            keys((0x87, True))
            transcribe.assert_called_once_with(audio_path, auto_copy_to_clipboard=True, cleanup_audio_path=audio_path)
            window.transcription_auto_clipboard_requested = True
            window._on_transcription_finished(SimpleNamespace(
                transcript_path=audio_path.with_suffix(".txt"), audio_path=audio_path,
                timed_transcript_text=expected, detected_language="de",
            ))
            assert field.text() == "", (
                f"Text appeared before modifier release: is_dictation={field.text() == expected}, "
                f"modifiers_down={native.modifiers_are_down()}"
            )
            keys((0x10, True), (0x11, True))
            wait(300)
            test_clipboard_sequence = native.clipboard_sequence()
            assert field.text() == expected, (
                f"Native paste failed: empty={not bool(field.text())}, feedback={window._dictation_feedback}, "
                f"target_current={native.target_is_current(target)}"
            )
            assert window.settings.recording_source == RECORDING_SOURCE_APPLICATION_AUDIO
            restore_info = (window._clipboard_restore_sequence, native.clipboard_sequence())
            window._restore_dictation_clipboard()
            wait(100)
            native_restored = ui.read_native_windows_clipboard_text().text == "scribe integration clipboard sentinel"
            assert native_restored, f"Clipboard was not restored: {restore_info}, sequence_after={native.clipboard_sequence()}"
            restored_formats = ui.snapshot_native_windows_clipboard(int(field.winId()))
            assert restored_formats is not None and restored_formats.get(png_format) == sentinel_formats[png_format], "PNG clipboard data was not preserved"
            test_clipboard_sequence = native.clipboard_sequence()
            field.hide()
            wait(100)
            assert not native.paste(target), "Pasted into a changed target"
            print("PASS: live global hook, microphone settings, release, held modifiers, minimized bar, unchanged focus, native Unicode paste, clipboard restore, changed-target rejection")
    finally:
        # Release only test modifiers and only while our test window owns focus.
        if native.target_is_current(native.capture_target()) and field.isActiveWindow():
            keys((0x87, True), (0x10, True), (0x11, True))
        if window is not None:
            window._pending_dictation_text = None
            service.is_recording = False
            window.close()
        field.close()
        if previous is not None and test_clipboard_sequence is not None and (
            native.clipboard_sequence() == test_clipboard_sequence or
            clipboard.text() in {expected, "scribe integration clipboard sentinel"}
        ):
            ui.restore_native_windows_clipboard(previous, int(field.winId()))
        app.processEvents()


if __name__ == "__main__":
    run()
