import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionButton
from PySide6.QtGui import QFontDatabase
from PySide6.QtCore import Qt
from recorder_app.i18n import UI_LANGUAGES, UiText, translate
from recorder_app import ui
from recorder_app.config import AppSettings, RECORDING_SOURCE_APPLICATION_AUDIO, RECORDING_SOURCE_SYSTEM_AUDIO, RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE, RECORDING_SOURCE_MICROPHONE


class ModernUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if sys.platform == "win32":
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")

    def setUp(self):
        self.patches = [patch.object(ui, "application_state_directory", return_value=Path("output")),
                        patch.object(ui, "TranscriptionService"), patch.object(ui, "GlobalHotkeyManager"),
                        patch.object(ui, "AudioCaptureService"), patch.object(ui.MainWindow, "_register_hotkeys")]
        mocks = [p.start() for p in self.patches]
        self.service = mocks[3].return_value
        self.service.is_recording = False
        self.service.list_loopback_devices.return_value = []
        self.service.list_microphone_devices.return_value = []
        self.service.list_application_audio_sessions.return_value = []
        self.service.application_audio_capability.return_value = ui.ApplicationAudioCapability(supported=True, reason="test")
        store = MagicMock()
        store.load.return_value = AppSettings(output_directory=str(Path("output").resolve()), recording_source=RECORDING_SOURCE_APPLICATION_AUDIO)
        self.window = ui.MainWindow(store)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.service.is_recording = False
        self.window._remove_log_handler()
        self.window.close()
        for p in reversed(self.patches):
            p.stop()

    def test_recording_cleanup_for_all_transcription_outcomes(self):
        w = self.window
        for outcome in ("finished", "cancelled", "failed"):
            for enabled, imported in ((True, False), (False, False), (True, True)):
                with self.subTest(outcome=outcome, enabled=enabled, imported=imported):
                    audio = Path("output") / f"cleanup-test-{uuid4().hex}.wav"
                    audio.write_bytes(b"test recording")
                    try:
                        w.settings.delete_recorded_audio_after_transcription = enabled
                        w.active_transcription_job = w._build_transcription_job(
                            audio, None if imported else audio
                        )
                        with patch.object(ui.QMessageBox, "critical"):
                            if outcome == "finished":
                                w._on_transcription_finished(SimpleNamespace(
                                    audio_path=audio, transcript_path=audio.with_suffix(".txt"),
                                    timed_transcript_text="Test", detected_language="en",
                                ))
                            else:
                                getattr(w, f"_on_transcription_{outcome}")("test")
                        self.assertEqual(audio.exists(), not enabled or imported)
                    finally:
                        audio.unlink(missing_ok=True)
                        w.active_transcription_job = None

    def test_dictation_uses_microphone_without_changing_regular_source(self):
        w = self.window
        self.service.start_recording.return_value = Path("output/dictation-test.wav")
        with patch.object(w.dictation_input, "capture_target", return_value="target"):
            self.assertTrue(w._start_push_to_transcribe_recording())
        used_settings = self.service.start_recording.call_args.args[0]
        self.assertEqual(used_settings.recording_source, RECORDING_SOURCE_MICROPHONE)
        self.assertEqual(w.settings.recording_source, RECORDING_SOURCE_APPLICATION_AUDIO)
        self.assertEqual(w._dictation_target, "target")
        self.assertTrue(w.dictation_bar.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(w.dictation_bar.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        w.duration_timer.stop()

    def test_dictation_waits_for_modifiers_and_restores_only_unchanged_clipboard(self):
        w = self.window
        clipboard = self.app.clipboard()
        clipboard.setText("previous")
        w._dictation_target = "target"
        w._pending_dictation_text = "dictated"
        w._dictation_insert_deadline = ui.time.monotonic() + 3
        w.push_to_transcribe_state = ui.PushToTranscribeState.INSERTING
        with patch.object(w.dictation_input, "target_is_current", return_value=True), \
             patch.object(w.dictation_input, "modifiers_are_down", return_value=True), \
             patch.object(w, "_copy_text_to_clipboard") as copy:
            w._try_insert_dictation()
            copy.assert_not_called()
            self.assertEqual(w._pending_dictation_text, "dictated")
        with patch.object(w.dictation_input, "target_is_current", return_value=True), \
             patch.object(w.dictation_input, "modifiers_are_down", return_value=False), \
             patch.object(w.dictation_input, "paste", return_value=True), \
             patch.object(w.dictation_input, "clipboard_sequence", return_value=7), \
             patch.object(w, "_copy_text_to_clipboard", return_value=True):
            w._try_insert_dictation()
            self.assertEqual(w.push_to_transcribe_state, ui.PushToTranscribeState.IDLE)
            clipboard.setText("new user copy")
            with patch.object(w.dictation_input, "clipboard_sequence", return_value=8):
                w._restore_dictation_clipboard()
            self.assertEqual(clipboard.text(), "new user copy")

    def test_dictation_focus_change_uses_copy_fallback(self):
        w = self.window
        w._dictation_target = "old window"
        w._pending_dictation_text = "dictated"
        with patch.object(w.dictation_input, "target_is_current", return_value=False), \
             patch.object(w.dictation_input, "paste") as paste, \
             patch.object(w, "_copy_text_to_clipboard", return_value=True) as copy:
            w._try_insert_dictation()
        paste.assert_not_called()
        self.assertEqual(copy.call_args.args[0], "dictated")
        self.assertIsNone(w._pending_dictation_text)
        self.assertEqual(w._dictation_feedback, "Push: bitte Strg+V")

    def test_cleanup_rejects_mismatched_paths_and_handles_delete_failure(self):
        w = self.window
        audio = Path("output") / f"cleanup-test-{uuid4().hex}.wav"
        audio.write_bytes(b"test recording")
        try:
            w.settings.delete_recorded_audio_after_transcription = True
            w.active_transcription_job = w._build_transcription_job(audio, audio)
            w._cleanup_recorded_audio_after_transcription(
                SimpleNamespace(audio_path=audio.with_name("other.wav"))
            )
            self.assertTrue(audio.exists())
            w.active_transcription_job = w._build_transcription_job(
                audio.with_name("other.wav"), audio
            )
            w._cleanup_recorded_audio_after_transcription()
            self.assertTrue(audio.exists())
            w.active_transcription_job = w._build_transcription_job(audio, audio)
            with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
                with self.assertLogs(ui.logger, level="ERROR"):
                    w._on_transcription_cancelled("test")
            self.assertTrue(audio.exists())
            w._cleanup_recorded_audio_after_transcription()
            self.assertFalse(audio.exists())
            w._cleanup_recorded_audio_after_transcription()
        finally:
            audio.unlink(missing_ok=True)
            w.active_transcription_job = None

    def test_smooth_wheel_accumulates_reverses_and_cancels(self):
        from PySide6.QtCore import QPoint, QPointF, QEvent
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QScrollArea

        area = QScrollArea()
        controller = ui.SmoothWheelScroll(area)
        bar = area.verticalScrollBar()
        bar.setRange(0, 1000)
        bar.setSingleStep(20)
        bar.setValue(300)
        animation = controller.animations[bar]

        def wheel(amount):
            event = QWheelEvent(QPointF(), QPointF(), QPoint(), QPoint(0, amount),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False)
            self.assertTrue(controller.eventFilter(area.viewport(), event))

        wheel(-120)
        target = animation.endValue()
        self.assertEqual(bar.value(), 300)
        animation.setCurrentTime(60)
        self.assertGreater(bar.value(), 300)
        self.assertLess(bar.value(), target)
        wheel(-120)
        self.assertGreater(animation.endValue(), target)
        current = bar.value()
        wheel(120)
        self.assertLess(animation.endValue(), current)
        controller.eventFilter(area.viewport(), QEvent(QEvent.Type.MouseButtonPress))
        self.assertEqual(animation.state(), ui.QAbstractAnimation.State.Stopped)
        bar.setValue(990)
        wheel(-120)
        animation.setCurrentTime(animation.duration())
        self.assertEqual(bar.value(), 1000)
        area.close()

    def test_sources_and_busy_states(self):
        w = self.window
        self.assertTrue(w.app_fields.isVisible())
        w.app_audio_microphone_checkbox.setChecked(True)
        self.assertTrue(w.microphone_device_combo.isEnabled())
        w._select_source(RECORDING_SOURCE_SYSTEM_AUDIO)
        w.system_microphone_checkbox.setChecked(True)
        self.assertEqual(w._current_recording_source(), RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE)
        self.assertFalse(w.app_fields.isVisible())
        self.service.is_recording = True
        w._refresh_actions()
        self.assertFalse(w.start_button.isVisible())
        self.assertTrue(w.stop_button.isVisible())
        self.assertFalse(w.microphone_device_combo.isEnabled())
        self.assertTrue(all(not b.isEnabled() for b in w.source_buttons.values()))
        self.service.is_recording = False
        with patch.object(w, "_is_transcribing", return_value=True):
            w._refresh_actions()
            self.assertTrue(w.progress_panel.isVisible())
            self.assertTrue(w.cancel_button.isEnabled())
            self.assertFalse(w.start_button.isEnabled())

    def test_toggle_animation_reverses_without_delaying_state(self):
        toggle = self.window.app_audio_microphone_checkbox
        toggle.setChecked(True)
        self.assertTrue(toggle.isChecked())
        toggle._slide.setCurrentTime(80)
        midpoint = toggle._thumb_position
        self.assertGreater(midpoint, 0)
        self.assertLess(midpoint, 1)
        toggle.setChecked(False)
        self.assertFalse(toggle.isChecked())
        self.assertAlmostEqual(toggle._thumb_position, midpoint)
        toggle._slide.setCurrentTime(160)
        self.assertEqual(toggle._thumb_position, 0)
        self.assertIsInstance(self.window.cleanup_recordings_checkbox, ui.ToggleCheckBox)

    def test_source_colors_fade_and_reverse_immediately(self):
        button = self.window.source_buttons[RECORDING_SOURCE_SYSTEM_AUDIO]
        button._fade.setCurrentTime(140)
        before = button._colors
        button.setChecked(True)
        self.assertTrue(button.isChecked())
        self.assertEqual(button._colors, before)
        button._fade.setCurrentTime(60)
        midway = button._colors
        self.assertNotEqual(midway, before)
        self.assertNotEqual(midway, button._to_colors)
        button.setChecked(False)
        self.assertFalse(button.isChecked())
        self.assertEqual(button._colors, midway)
        button._fade.setCurrentTime(140)
        self.assertEqual(button._colors, button._to_colors)

    def test_header_status_and_measured_level(self):
        w = self.window
        self.service.is_recording = True
        self.service.audio_level = 0.64
        w._update_audio_status()
        self.assertEqual(w.audio_status._state, "recording")
        self.assertGreater(w.audio_status._level, 0)
        w.audio_status._fade.setCurrentTime(180)
        self.assertEqual(w.audio_status._color.name(), "#ff777f")
        self.service.is_recording = False
        with patch.object(w, "_is_transcribing", return_value=True):
            w._update_audio_status()
            self.assertEqual(w.audio_status._state, "transcribing")
        w._update_audio_status()
        self.assertEqual(w.audio_status._state, "ready")

    def test_navigation_transcript_and_layout(self):
        w = self.window
        w._show_page(1)
        self.assertEqual(w.pages.currentIndex(), 1)
        self.assertTrue(w.storage_input.isVisible())
        w._show_page(0)
        self.assertFalse(w.diagnostics_panel.isVisible())
        w.diagnostics_button.setChecked(True)
        self.assertTrue(w.log_view.isVisible())
        w.diagnostics_button.setChecked(False)
        w.transcript_edit.setPlainText("00:00 Testtranskript")
        self.assertTrue(w.copy_transcript_button.isEnabled())
        self.assertTrue(w.export_button.isEnabled())
        w.resize(1000, 720)
        self.app.processEvents()
        self.assertGreaterEqual(w.transcript_edit.height(), 180)
        self.assertLessEqual(w.centralWidget().width(), w.width())

    def test_live_languages_preserve_transcript_and_form(self):
        w = self.window
        w._show_page(1)
        w._set_combo_data(w.language_combo, "ro")
        w._set_combo_data(w.model_combo, "small")
        w.transcript_edit.setPlainText("Eigener Text: café / 日本語 / العربية {name}")
        w.transcript_edit.moveCursor(w.transcript_edit.textCursor().MoveOperation.End)
        w.transcript_edit.insertPlainText(" edited")
        cursor = w.transcript_edit.textCursor().position()
        text = w.transcript_edit.toPlainText()
        w._set_ui_text(w.recording_value, "02:34")
        w._set_ui_text(w.autosave_value, "D:/example/meeting.txt")
        w._set_ui_text(w.status_value, "Aufnahme läuft ({value0})", value0=UiText("Mikrofon"))
        for code in UI_LANGUAGES:
            with self.subTest(code=code):
                w._set_combo_data(w.ui_language_combo, code)
                self.app.processEvents()
                self.assertEqual(w.settings_button.text(), translate(code, "Einstellungen"))
                self.assertEqual(w.save_settings_button.text(), translate(code, "Einstellungen speichern"))
                self.assertEqual(w.language_combo.currentData(), "ro")
                self.assertEqual(w.model_combo.currentData(), "small")
                self.assertEqual(w.transcript_edit.toPlainText(), text)
                self.assertEqual(w.transcript_edit.textCursor().position(), cursor)
                self.assertTrue(w.transcript_edit.document().isUndoAvailable())
                self.assertEqual(w.pages.currentIndex(), 1)
                self.assertEqual(w.recording_value.text(), "02:34")
                self.assertEqual(w.autosave_value.text(), "D:/example/meeting.txt")
                self.assertIn(translate(code, "Mikrofon"), w.status_value.text())
                self.assertEqual(w.layoutDirection(), Qt.LayoutDirection.RightToLeft if code == "ar"
                                 else Qt.LayoutDirection.LeftToRight)
                self.assertEqual(w.storage_input.layoutDirection(), Qt.LayoutDirection.LeftToRight)
                self.assertIn(translate(code, "Einstellungen"), w._build_help_markdown())
        self.service.start_recording.assert_not_called()
        w.settings_store.save.assert_not_called()

    def test_language_save_and_reload(self):
        w = self.window
        w._select_source(RECORDING_SOURCE_SYSTEM_AUDIO)
        w._set_combo_data(w.ui_language_combo, "ro")
        w._set_combo_data(w.language_combo, "fr")
        with patch.object(w, "_register_hotkeys", return_value=True):
            self.assertTrue(w._persist_form_settings(show_feedback=True))
        saved = w.settings_store.save.call_args.args[0]
        self.assertEqual(saved.ui_language, "ro")
        self.assertEqual(saved.language, "fr")
        store = MagicMock()
        store.load.return_value = saved
        reopened = ui.MainWindow(store)
        try:
            self.assertEqual(reopened.settings_button.text(), "Setări")
            self.assertEqual(reopened.ui_language_combo.currentData(), "ro")
            self.assertEqual(reopened.language_combo.currentData(), "fr")
        finally:
            reopened._remove_log_handler()
            reopened.close()

    def test_language_switch_while_busy_keeps_actions_disabled(self):
        w = self.window
        self.service.is_recording = True
        w._set_combo_data(w.ui_language_combo, "ar")
        self.assertFalse(w.start_button.isVisible())
        self.assertTrue(w.stop_button.isVisible())
        self.assertFalse(w.save_settings_button.isEnabled())
        self.service.is_recording = False
        with patch.object(w, "_is_transcribing", return_value=True):
            w._set_combo_data(w.ui_language_combo, "en")
            self.assertFalse(w.start_button.isEnabled())
            self.assertFalse(w.save_settings_button.isEnabled())
            self.assertTrue(w.cancel_button.isEnabled())

    def test_arabic_toggle_thumb_stays_inside_track(self):
        toggle = self.window.timestamps_checkbox
        for code in ("de", "ar", "en"):
            self.window._set_combo_data(self.window.ui_language_combo, code)
            self.app.processEvents()
            option = QStyleOptionButton()
            toggle.initStyleOption(option)
            track = toggle.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, toggle)
            for position in (0.0, 0.5, 1.0):
                toggle._thumb_position = position
                self.assertTrue(track.contains(toggle._thumb_rect().toAlignedRect()), (code, position))


if __name__ == "__main__":
    unittest.main()
