from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import logging
from pathlib import Path
import sys
import time
from ctypes import wintypes
from uuid import uuid4

from PySide6.QtCore import QEvent, QMimeData, QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QClipboard, QCloseEvent, QContextMenuEvent, QKeyEvent, QKeySequence, QWheelEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QApplication,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QComboBox,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QMenu,
)

from .audio import (
    ApplicationAudioCapability,
    ApplicationAudioSessionInfo,
    AudioCaptureError,
    DualSourceAudioCaptureService as AudioCaptureService,
    LoopbackDeviceInfo,
    MicrophoneDeviceInfo,
)
from .config import (
    APP_NAME,
    AppSettings,
    RECORDING_SOURCE_APPLICATION_AUDIO,
    RECORDING_SOURCE_MICROPHONE,
    RECORDING_SOURCE_SYSTEM_AUDIO,
    RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE,
    SettingsStore,
    application_state_directory,
)
from .hotkeys import GlobalHotkeyManager, HotkeyRegistrationError, keyboard_to_qt_hotkey, normalize_hotkey
from .transcription import (
    FastTranscriptionService as TranscriptionService,
    FastTranscriptionWorker as TranscriptionWorker,
    TRANSCRIPT_FORMAT_COMPACT,
    TRANSCRIPT_FORMAT_OPTIONS,
    TRANSCRIPTION_PROFILE_FAST,
    TRANSCRIPTION_PROFILE_OPTIONS,
    TranscriptionRequest,
    TranscriptionResult,
    build_export_content,
    format_transcript_text,
)

LANGUAGE_OPTIONS = {
    "Automatisch erkennen": "auto",
    "Deutsch": "de",
    "English": "en",
}

MODEL_OPTIONS = ["tiny", "base", "small", "medium", "large-v3", "turbo"]
EXPORT_FILTER = "Textdatei (*.txt);;SubRip Untertitel (*.srt);;WebVTT Untertitel (*.vtt)"
RECORDING_SOURCE_OPTIONS = {
    "Systemaudio (WASAPI Loopback)": RECORDING_SOURCE_SYSTEM_AUDIO,
    "Mikrofon": RECORDING_SOURCE_MICROPHONE,
    "Systemaudio + Mikrofon": RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE,
    "App-Audio": RECORDING_SOURCE_APPLICATION_AUDIO,
}

logger = logging.getLogger(__name__)


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
WINDOWS_CLIPBOARD_RETRIES = 10
WINDOWS_CLIPBOARD_RETRY_DELAY_SECONDS = 0.04
STANDARD_CLIPBOARD_FORMATS = {
    1: "CF_TEXT",
    2: "CF_BITMAP",
    7: "CF_OEMTEXT",
    8: "CF_DIB",
    13: "CF_UNICODETEXT",
    15: "CF_HDROP",
    16: "CF_LOCALE",
    17: "CF_DIBV5",
}


@dataclass(slots=True)
class NativeClipboardWriteResult:
    ok: bool
    attempts: int = 0
    error: str = ""


@dataclass(slots=True)
class NativeClipboardSnapshot:
    ok: bool
    has_unicode_text: bool = False
    text: str = ""
    formats: list[str] | None = None
    attempts: int = 0
    error: str = ""


def _win32_error(prefix: str) -> str:
    error_code = ctypes.get_last_error()
    details = ctypes.FormatError(error_code).strip() if error_code else "kein Win32-Fehlercode"
    return f"{prefix} fehlgeschlagen (WinError {error_code}: {details})"


def _win32_clipboard_api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.restype = wintypes.UINT
    user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClipboardFormatNameW.restype = ctypes.c_int

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE
    return user32, kernel32


def _open_windows_clipboard(user32, hwnd: int | None) -> int:
    last_error = ""
    owner = wintypes.HWND(hwnd or 0)
    for attempt in range(1, WINDOWS_CLIPBOARD_RETRIES + 1):
        if user32.OpenClipboard(owner):
            return attempt
        last_error = _win32_error("OpenClipboard")
        time.sleep(WINDOWS_CLIPBOARD_RETRY_DELAY_SECONDS)
    raise RuntimeError(f"{last_error}; Clipboard nach {WINDOWS_CLIPBOARD_RETRIES} Versuchen nicht verfügbar")


def _windows_clipboard_format_name(user32, format_id: int) -> str:
    standard_name = STANDARD_CLIPBOARD_FORMATS.get(format_id)
    if standard_name:
        return f"{format_id}:{standard_name}"

    buffer = ctypes.create_unicode_buffer(256)
    length = user32.GetClipboardFormatNameW(format_id, buffer, len(buffer))
    if length > 0:
        return f"{format_id}:{buffer.value}"
    return f"{format_id}:<unbenannt>"


def _windows_clipboard_formats(user32) -> list[str]:
    formats: list[str] = []
    current_format = 0
    while True:
        current_format = user32.EnumClipboardFormats(current_format)
        if current_format == 0:
            return formats
        formats.append(_windows_clipboard_format_name(user32, current_format))


def write_native_windows_clipboard_text(text: str, hwnd: int | None = None) -> NativeClipboardWriteResult:
    if sys.platform != "win32":
        return NativeClipboardWriteResult(ok=False, error="Nativer Windows-Clipboard-Pfad ist nur unter Windows verfügbar.")

    user32, kernel32 = _win32_clipboard_api()
    encoded_text = f"{text}\0".encode("utf-16-le")
    memory_handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded_text))
    if not memory_handle:
        return NativeClipboardWriteResult(ok=False, error=_win32_error("GlobalAlloc"))

    clipboard_open = False
    attempts = 0
    try:
        locked_memory = kernel32.GlobalLock(memory_handle)
        if not locked_memory:
            raise RuntimeError(_win32_error("GlobalLock"))
        try:
            ctypes.memmove(locked_memory, encoded_text, len(encoded_text))
        finally:
            kernel32.GlobalUnlock(memory_handle)

        attempts = _open_windows_clipboard(user32, hwnd)
        clipboard_open = True
        if not user32.EmptyClipboard():
            raise RuntimeError(_win32_error("EmptyClipboard"))
        if not user32.SetClipboardData(CF_UNICODETEXT, memory_handle):
            raise RuntimeError(_win32_error("SetClipboardData(CF_UNICODETEXT)"))

        memory_handle = None
        return NativeClipboardWriteResult(ok=True, attempts=attempts)
    except Exception as exc:
        return NativeClipboardWriteResult(ok=False, attempts=attempts, error=str(exc))
    finally:
        if clipboard_open:
            user32.CloseClipboard()
        if memory_handle:
            kernel32.GlobalFree(memory_handle)


def read_native_windows_clipboard_text(hwnd: int | None = None) -> NativeClipboardSnapshot:
    if sys.platform != "win32":
        return NativeClipboardSnapshot(ok=False, formats=[], error="Nativer Windows-Clipboard-Pfad ist nur unter Windows verfügbar.")

    user32, kernel32 = _win32_clipboard_api()
    clipboard_open = False
    attempts = 0
    try:
        attempts = _open_windows_clipboard(user32, hwnd)
        clipboard_open = True
        formats = _windows_clipboard_formats(user32)
        has_unicode_text = bool(user32.IsClipboardFormatAvailable(CF_UNICODETEXT))
        if not has_unicode_text:
            return NativeClipboardSnapshot(
                ok=True,
                has_unicode_text=False,
                formats=formats,
                attempts=attempts,
            )

        memory_handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not memory_handle:
            raise RuntimeError(_win32_error("GetClipboardData(CF_UNICODETEXT)"))
        locked_memory = kernel32.GlobalLock(memory_handle)
        if not locked_memory:
            raise RuntimeError(_win32_error("GlobalLock(CF_UNICODETEXT)"))
        try:
            text = ctypes.wstring_at(locked_memory)
        finally:
            kernel32.GlobalUnlock(memory_handle)

        return NativeClipboardSnapshot(
            ok=True,
            has_unicode_text=True,
            text=text,
            formats=formats,
            attempts=attempts,
        )
    except Exception as exc:
        return NativeClipboardSnapshot(ok=False, formats=[], attempts=attempts, error=str(exc))
    finally:
        if clipboard_open:
            user32.CloseClipboard()


class PushToTranscribeState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"


@dataclass(slots=True)
class TranscriptionJobContext:
    job_id: str
    audio_path: Path
    cleanup_audio_path: Path | None
    delete_recorded_audio_after_success: bool


PUSH_TO_TRANSCRIBE_TRANSITIONS = {
    PushToTranscribeState.IDLE: {PushToTranscribeState.RECORDING},
    PushToTranscribeState.RECORDING: {
        PushToTranscribeState.IDLE,
        PushToTranscribeState.TRANSCRIBING,
    },
    PushToTranscribeState.TRANSCRIBING: {PushToTranscribeState.IDLE},
}


class WheelGuardComboBox(QComboBox):
    """Ignore mouse-wheel changes while the dropdown itself is closed."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


class IsolatedWheelPlainTextEdit(QPlainTextEdit):
    """Keep transcript scrolling from bubbling into parent scroll areas."""

    copy_requested = Signal(str)

    def _selected_clipboard_text(self) -> str:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return ""
        return cursor.selectedText().replace("\u2029", "\n").replace("\u2028", "\n")

    def copy(self) -> None:
        selected_text = self._selected_clipboard_text()
        if selected_text:
            self.copy_requested.emit(selected_text)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        menu = QMenu(self)
        copy_action = menu.addAction("Kopieren")
        copy_action.setEnabled(self.textCursor().hasSelection())
        copy_action.triggered.connect(self.copy)
        menu.addSeparator()
        select_all_action = menu.addAction("Alles auswählen")
        select_all_action.triggered.connect(self.selectAll)
        menu.exec(event.globalPos())

    def wheelEvent(self, event: QWheelEvent) -> None:
        super().wheelEvent(event)
        event.accept()

    def viewportEvent(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel:
            super().viewportEvent(event)
            event.accept()
            return True
        return super().viewportEvent(event)


class _LogEmitter(QObject):
    message = Signal(str)


class UiLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.emitter = _LogEmitter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self.emitter.message.emit(message)


class MainWindow(QMainWindow):
    def __init__(self, settings_store: SettingsStore) -> None:
        super().__init__()
        self.settings_store = settings_store
        self.settings = self.settings_store.load()

        self.audio_service = AudioCaptureService()
        self.hotkey_manager = GlobalHotkeyManager()
        self.transcription_service = TranscriptionService(
            model_root=application_state_directory() / "models"
        )

        self.transcription_thread: QThread | None = None
        self.transcription_worker: TranscriptionWorker | None = None
        self.current_audio_path: Path | None = None
        self.current_transcript_path: Path | None = None
        self.current_timed_transcript_text = ""
        self.active_transcription_job: TranscriptionJobContext | None = None
        self.recording_started_at: datetime | None = None
        self.log_handler: UiLogHandler | None = None
        self.transcription_cancel_requested = False
        self.transcription_auto_clipboard_requested = False
        self.push_to_transcribe_state = PushToTranscribeState.IDLE
        self.available_audio_devices: list[LoopbackDeviceInfo] = []
        self.available_microphone_devices: list[MicrophoneDeviceInfo] = []
        self.available_app_audio_sessions: list[ApplicationAudioSessionInfo] = []
        self.app_audio_capability = ApplicationAudioCapability(
            supported=False,
            reason="App-Audio wurde noch nicht geprüft.",
        )

        self._build_ui()
        self._apply_style()
        self._install_log_handler()
        self.reload_audio_devices(show_feedback=False)
        self.reload_microphone_devices(show_feedback=False)
        self.reload_application_audio_sessions(show_feedback=False, force_refresh=True)
        self._load_settings_into_form()
        self._connect_signals()
        self._register_hotkeys(notify_on_error=False)
        self._refresh_actions()
        self._apply_recording_source_to_ui()
        self._warn_if_saved_device_missing()

    def _build_ui(self) -> None:
        self.setWindowTitle(f"{APP_NAME} - Systemaudio Transkription")
        self.resize(1440, 900)
        self.setMinimumSize(1120, 720)

        root = QWidget(self)
        root.setObjectName("appRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        hero = QFrame()
        hero.setObjectName("heroCard")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(22, 18, 22, 18)
        hero_layout.setSpacing(16)

        hero_copy = QVBoxLayout()
        hero_copy.setSpacing(8)

        title = QLabel("Systemaudio mitschneiden und lokal transkribieren")
        title.setObjectName("heroTitle")
        author_credit = QLabel("by Daniel Marschner")
        author_credit.setObjectName("heroAuthor")
        subtitle = QLabel(
            "Loopback-Aufnahme, globale Hotkeys und lokale Whisper-Transkription "
            "für Calls, Meetings und Audio-Dateien."
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("heroSubtitle")
        hero_copy.addWidget(title)
        hero_copy.addWidget(author_credit)
        hero_copy.addWidget(subtitle)

        hero_badges = QHBoxLayout()
        hero_badges.setSpacing(8)
        for badge_text in ("WASAPI Loopback", "Mikrofon", "faster-whisper", "Hotkeys"):
            badge = QLabel(badge_text)
            badge.setObjectName("heroBadge")
            hero_badges.addWidget(badge)
        hero_badges.addStretch(1)
        hero_copy.addLayout(hero_badges)

        hero_status = QFrame()
        hero_status.setObjectName("heroStatusCard")
        hero_status.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        hero_status_layout = QVBoxLayout(hero_status)
        hero_status_layout.setContentsMargins(18, 14, 18, 14)
        hero_status_layout.setSpacing(4)
        hero_status_caption = QLabel("Bereit für Aufnahme")
        hero_status_caption.setObjectName("heroStatusCaption")
        hero_status_text = QLabel("Start, Push-to-Transcribe, Import und Export bleiben direkt erreichbar.")
        hero_status_text.setObjectName("heroStatusText")
        hero_status_text.setWordWrap(True)
        hero_status_layout.addWidget(hero_status_caption)
        hero_status_layout.addWidget(hero_status_text)

        hero_layout.addLayout(hero_copy, 1)
        hero_layout.addWidget(hero_status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("mainSplitter")
        splitter.setChildrenCollapsible(False)

        workspace = QWidget()
        workspace.setObjectName("workspacePanel")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(16)

        controls_group = QGroupBox("Aufnahme und Import")
        controls_group.setObjectName("panelCard")
        controls_layout = QVBoxLayout(controls_group)
        controls_layout.setContentsMargins(18, 46, 18, 18)
        controls_layout.setSpacing(16)

        button_grid = QGridLayout()
        button_grid.setContentsMargins(0, 0, 0, 0)
        button_grid.setHorizontalSpacing(10)
        button_grid.setVerticalSpacing(10)
        self.start_button = QPushButton("Aufnahme starten")
        self.start_button.setObjectName("primaryButton")
        self.stop_button = QPushButton("Aufnahme stoppen")
        self.stop_button.setObjectName("dangerButton")
        self.import_button = QPushButton("Audio-Datei importieren")
        self.import_button.setObjectName("secondaryButton")
        self.export_button = QPushButton("Transkript exportieren")
        self.export_button.setObjectName("secondaryButton")
        self.cancel_button = QPushButton("Transkription abbrechen")
        self.cancel_button.setObjectName("dangerButton")
        self.help_button = QPushButton("Hilfe")
        self.help_button.setObjectName("ghostButton")
        for action_button in (
            self.start_button,
            self.stop_button,
            self.import_button,
            self.export_button,
            self.cancel_button,
            self.help_button,
        ):
            action_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button_grid.addWidget(self.start_button, 0, 0)
        button_grid.addWidget(self.stop_button, 0, 1)
        button_grid.addWidget(self.import_button, 1, 0)
        button_grid.addWidget(self.export_button, 1, 1)
        button_grid.addWidget(self.cancel_button, 2, 0)
        button_grid.addWidget(self.help_button, 2, 1)
        button_grid.setColumnStretch(0, 1)
        button_grid.setColumnStretch(1, 1)
        controls_layout.addLayout(button_grid)

        info_group = QFrame()
        info_group.setObjectName("statusBoard")
        info_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        info_layout = QGridLayout(info_group)
        info_layout.setContentsMargins(14, 12, 14, 12)
        info_layout.setHorizontalSpacing(18)
        info_layout.setVerticalSpacing(4)

        self.status_value = QLabel("Bereit")
        self.status_value.setObjectName("metricValue")
        self.status_value.setWordWrap(True)
        self.push_to_transcribe_value = QLabel("Bereit")
        self.push_to_transcribe_value.setObjectName("metricValue")
        self.push_to_transcribe_value.setWordWrap(True)
        self.source_value = QLabel("Noch keine Aufnahme oder importierte Datei.")
        self.source_value.setObjectName("metricValueSmall")
        self.source_value.setWordWrap(True)
        self.recording_value = QLabel("00:00")
        self.recording_value.setObjectName("metricValue")
        self.autosave_value = QLabel("Wird nach der ersten Transkription angezeigt.")
        self.autosave_value.setObjectName("metricValueSmall")
        self.autosave_value.setWordWrap(True)

        def add_metric(row: int, column: int, caption_text: str, value_label: QLabel, column_span: int = 1) -> None:
            caption = QLabel(caption_text)
            caption.setObjectName("metricCaption")
            caption.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            value_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            info_layout.addWidget(caption, row, column, 1, column_span)
            info_layout.addWidget(value_label, row + 1, column, 1, column_span)

        add_metric(0, 0, "Status", self.status_value)
        add_metric(0, 1, "Push-to-Transcribe", self.push_to_transcribe_value)
        add_metric(0, 2, "Dauer", self.recording_value)
        add_metric(2, 0, "Quelle", self.source_value, 3)
        add_metric(4, 0, "Auto-Save", self.autosave_value, 3)
        info_layout.setColumnStretch(0, 1)
        info_layout.setColumnStretch(1, 1)
        info_layout.setColumnStretch(2, 1)
        controls_layout.addWidget(info_group)

        progress_group = QGroupBox("Transkriptionsfortschritt")
        progress_group.setObjectName("panelCard")
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.setContentsMargins(18, 46, 18, 18)
        progress_layout.setSpacing(12)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_detail = QLabel("Noch keine Transkription gestartet.")
        self.progress_detail.setWordWrap(True)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("errorBanner")
        self.error_label.hide()
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.progress_detail)
        progress_layout.addWidget(self.error_label)

        transcript_group = QGroupBox("Transkript")
        transcript_group.setObjectName("transcriptPanel")
        transcript_layout = QVBoxLayout(transcript_group)
        transcript_layout.setContentsMargins(18, 46, 18, 18)
        transcript_layout.setSpacing(12)
        transcript_action_row = QHBoxLayout()
        transcript_action_row.setSpacing(10)
        transcript_hint = QLabel("Erkannter Text")
        transcript_hint.setObjectName("sectionHint")
        transcript_action_row.addWidget(transcript_hint)
        transcript_action_row.addStretch(1)
        self.copy_transcript_button = QPushButton("Transkript kopieren")
        self.copy_transcript_button.setObjectName("secondaryButton")
        self.copy_transcript_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.copy_transcript_button.setToolTip(
            "Kopiert den kompletten sichtbaren Transkripttext in die Zwischenablage."
        )
        transcript_action_row.addWidget(self.copy_transcript_button)
        transcript_layout.addLayout(transcript_action_row)
        self.transcript_edit = IsolatedWheelPlainTextEdit()
        self.transcript_edit.setPlaceholderText(
            "Hier erscheint die Transkription mit Zeitmarken. "
            "Der Text kann vor dem Export noch angepasst werden."
        )
        self.transcript_edit.setReadOnly(False)
        self.transcript_edit.setObjectName("transcriptEditor")
        self.transcript_edit.setUndoRedoEnabled(True)
        self.transcript_edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.transcript_edit.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.transcript_edit.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.transcript_edit.setMinimumHeight(150)
        transcript_layout.addWidget(self.transcript_edit)

        workspace_layout.addWidget(controls_group)
        workspace_layout.addWidget(progress_group)
        workspace_layout.addWidget(transcript_group, 1)

        workspace_scroll = QScrollArea()
        workspace_scroll.setObjectName("workspaceScroll")
        workspace_scroll.setWidgetResizable(True)
        workspace_scroll.setFrameShape(QFrame.Shape.NoFrame)
        workspace_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        workspace_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        workspace_scroll.viewport().setObjectName("workspaceViewport")
        workspace_scroll.setWidget(workspace)

        settings_panel = QScrollArea()
        settings_panel.setObjectName("sideScroll")
        settings_panel.setWidgetResizable(True)
        settings_panel.setFrameShape(QFrame.Shape.NoFrame)
        settings_panel.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_panel.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        settings_content = QWidget()
        settings_content.setObjectName("sidePanel")
        settings_layout = QVBoxLayout(settings_content)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(16)

        settings_group = QGroupBox("Einstellungen")
        settings_group.setObjectName("panelCard")
        settings_form = QVBoxLayout(settings_group)
        settings_form.setContentsMargins(18, 46, 18, 18)
        settings_form.setSpacing(10)

        def add_setting(label_text: str, widget: QWidget) -> None:
            label = QLabel(label_text)
            label.setObjectName("fieldLabel")
            label.setWordWrap(True)
            settings_form.addWidget(label)
            settings_form.addWidget(widget)

        def wrap_inline_layout(inline_layout: QHBoxLayout) -> QWidget:
            wrapper = QWidget()
            wrapper.setObjectName("inlineField")
            inline_layout.setContentsMargins(0, 0, 0, 0)
            wrapper.setLayout(inline_layout)
            return wrapper

        storage_row = QHBoxLayout()
        storage_row.setSpacing(8)
        self.storage_input = QLineEdit()
        self.storage_input.setClearButtonEnabled(True)
        self.storage_button = QPushButton("Ordner wählen")
        self.storage_button.setObjectName("secondaryButton")
        self.storage_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        storage_row.addWidget(self.storage_input, 1)
        storage_row.addWidget(self.storage_button)

        self.language_combo = WheelGuardComboBox()
        for label, code in LANGUAGE_OPTIONS.items():
            self.language_combo.addItem(label, code)

        self.model_combo = WheelGuardComboBox()
        for model in MODEL_OPTIONS:
            self.model_combo.addItem(model, model)

        self.performance_profile_combo = WheelGuardComboBox()
        for label, profile_key in TRANSCRIPTION_PROFILE_OPTIONS.items():
            self.performance_profile_combo.addItem(label, profile_key)
        self.performance_profile_combo.setToolTip(
            "Schnell reduziert CPU-Last. Ausgewogen und Qualität nutzen mehr Suchbreite und können länger dauern."
        )

        self.timestamps_checkbox = QCheckBox("Timestamps anzeigen")
        self.timestamps_checkbox.setChecked(True)
        self.transcript_format_combo = WheelGuardComboBox()
        for label, format_key in TRANSCRIPT_FORMAT_OPTIONS.items():
            self.transcript_format_combo.addItem(label, format_key)
        self.transcript_format_combo.setToolTip(
            "Kompakt verbindet Whisper-Segmente ohne AI-Umschreibung zu natürlicheren Absätzen."
        )
        self.cleanup_recordings_checkbox = QCheckBox(
            "Aufnahmen nach Transkription löschen"
        )
        self.cleanup_recordings_checkbox.setChecked(True)
        self.cleanup_recordings_checkbox.setToolTip(
            "Gilt nur für von der App erstellte WAV-Aufnahmen. Importierte Dateien bleiben erhalten."
        )

        self.recording_source_combo = WheelGuardComboBox()
        for label, value in RECORDING_SOURCE_OPTIONS.items():
            self.recording_source_combo.addItem(label, value)

        audio_device_row = QHBoxLayout()
        audio_device_row.setSpacing(8)
        self.audio_device_combo = WheelGuardComboBox()
        self.refresh_devices_button = QPushButton("Loopback aktualisieren")
        self.refresh_devices_button.setObjectName("secondaryButton")
        self.refresh_devices_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        audio_device_row.addWidget(self.audio_device_combo, 1)
        audio_device_row.addWidget(self.refresh_devices_button)

        microphone_device_row = QHBoxLayout()
        microphone_device_row.setSpacing(8)
        self.microphone_device_combo = WheelGuardComboBox()
        self.refresh_microphones_button = QPushButton("Mikrofone aktualisieren")
        self.refresh_microphones_button.setObjectName("secondaryButton")
        self.refresh_microphones_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        microphone_device_row.addWidget(self.microphone_device_combo, 1)
        microphone_device_row.addWidget(self.refresh_microphones_button)

        app_audio_row = QHBoxLayout()
        app_audio_row.setSpacing(8)
        self.app_audio_session_combo = WheelGuardComboBox()
        self.refresh_app_audio_button = QPushButton("Apps aktualisieren")
        self.refresh_app_audio_button.setObjectName("secondaryButton")
        self.refresh_app_audio_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        app_audio_row.addWidget(self.app_audio_session_combo, 1)
        app_audio_row.addWidget(self.refresh_app_audio_button)
        self.app_audio_hint = QLabel("App-Audio wird nur angezeigt, wenn echte Windows-Audio-Sessions erkannt werden.")
        self.app_audio_hint.setWordWrap(True)
        self.app_audio_hint.setObjectName("mutedLabel")

        self.start_hotkey_edit = QKeySequenceEdit()
        self.stop_hotkey_edit = QKeySequenceEdit()
        self.push_to_transcribe_hotkey_edit = QKeySequenceEdit()
        hotkey_hint = QLabel(
            "Tipp: Wird derselbe Start/Stopp-Hotkey gesetzt, arbeitet die App als Toggle. "
            "Push-to-Transcribe startet beim Drücken und stoppt beim Loslassen."
        )
        hotkey_hint.setWordWrap(True)
        hotkey_hint.setObjectName("mutedLabel")

        add_setting("Speicherpfad", wrap_inline_layout(storage_row))
        add_setting("Sprache", self.language_combo)
        add_setting("Whisper-Modell", self.model_combo)
        add_setting("Performance", self.performance_profile_combo)
        add_setting("Transkript", self.timestamps_checkbox)
        add_setting("Textformat", self.transcript_format_combo)
        add_setting("Aufnahme-Dateien", self.cleanup_recordings_checkbox)
        add_setting("Aufnahmequelle", self.recording_source_combo)
        add_setting("Systemaudio-Gerät", wrap_inline_layout(audio_device_row))
        add_setting("Mikrofon-Gerät", wrap_inline_layout(microphone_device_row))
        add_setting("App-Audio", wrap_inline_layout(app_audio_row))
        settings_form.addWidget(self.app_audio_hint)
        add_setting("Start-Hotkey", self.start_hotkey_edit)
        add_setting("Stopp-Hotkey", self.stop_hotkey_edit)
        add_setting("Push-to-Transcribe", self.push_to_transcribe_hotkey_edit)
        settings_form.addWidget(hotkey_hint)

        self.save_settings_button = QPushButton("Einstellungen speichern")
        self.save_settings_button.setObjectName("primaryButton")

        notes_group = QGroupBox("Hinweise")
        notes_group.setObjectName("noteCard")
        notes_layout = QVBoxLayout(notes_group)
        notes_layout.setSpacing(10)
        notes_layout.setContentsMargins(18, 44, 18, 18)
        note_one = QLabel(
            "Die Aufnahme nutzt unter Windows WASAPI-Loopback. "
            "Das erste Transkript lädt das gewählte Modell einmalig lokal herunter."
        )
        note_one.setWordWrap(True)
        note_one.setObjectName("noteLabel")
        note_two = QLabel(
            "Importierte Dateien werden direkt transkribiert. "
            "Aufgenommene Audios können nach erfolgreicher Transkription automatisch entfernt werden."
        )
        note_two.setWordWrap(True)
        note_two.setObjectName("noteLabel")
        notes_layout.addWidget(note_one)
        notes_layout.addWidget(note_two)

        log_group = QGroupBox("Log")
        log_group.setObjectName("panelCard")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(18, 44, 18, 18)
        log_layout.setSpacing(10)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Hier erscheinen Laufzeitmeldungen und Fehler.")
        self.log_view.document().setMaximumBlockCount(500)
        self.log_view.setMinimumHeight(130)
        log_layout.addWidget(self.log_view)

        settings_layout.addWidget(settings_group)
        settings_layout.addWidget(self.save_settings_button)
        settings_layout.addWidget(notes_group)
        settings_layout.addWidget(log_group, 1)
        settings_layout.addStretch(1)
        settings_panel.setWidget(settings_content)

        splitter.addWidget(workspace_scroll)
        splitter.addWidget(settings_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([880, 520])

        layout.addWidget(hero)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(root)

        self.duration_timer = QTimer(self)
        self.duration_timer.setInterval(1_000)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #e9eef3;
            }
            QWidget {
                color: #17212b;
                font-family: "Segoe UI", "Noto Sans", sans-serif;
                font-size: 14px;
            }
            QWidget#appRoot {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #f7fafc,
                    stop: 0.52 #eef4f7,
                    stop: 1 #e4edf1
                );
            }
            QFrame#heroCard {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #102a3a,
                    stop: 0.55 #0e5d68,
                    stop: 1 #1f7a6d
                );
                border: 1px solid rgba(255, 255, 255, 0.22);
                border-radius: 26px;
            }
            QLabel#heroTitle {
                color: #f7fbfc;
                font-size: 26px;
                font-weight: 800;
            }
            QLabel#heroAuthor {
                color: rgba(247, 251, 252, 0.78);
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.5px;
            }
            QLabel#heroSubtitle {
                color: #cfe3e8;
                font-size: 15px;
                font-weight: 500;
            }
            QLabel#heroBadge {
                background: rgba(255, 255, 255, 0.14);
                color: #eaf7f8;
                border: 1px solid rgba(255, 255, 255, 0.20);
                border-radius: 12px;
                padding: 6px 10px;
                font-size: 12px;
                font-weight: 700;
            }
            QFrame#heroStatusCard {
                background: rgba(255, 255, 255, 0.13);
                border: 1px solid rgba(255, 255, 255, 0.22);
                border-radius: 18px;
            }
            QLabel#heroStatusCaption {
                color: #ffffff;
                font-size: 16px;
                font-weight: 800;
            }
            QLabel#heroStatusText {
                color: #d7ecf0;
                font-size: 13px;
            }
            QSplitter#mainSplitter::handle {
                background: transparent;
                width: 14px;
            }
            QGroupBox#panelCard,
            QGroupBox#transcriptPanel,
            QGroupBox#noteCard {
                background: #ffffff;
                border: 1px solid #dce6eb;
                border-radius: 18px;
                margin-top: 0;
                padding-top: 0;
                font-weight: 800;
                color: #102a3a;
            }
            QGroupBox::title {
                subcontrol-origin: border;
                subcontrol-position: top left;
                left: 18px;
                top: 14px;
                padding: 0;
                color: #102a3a;
                background: transparent;
                font-size: 14px;
                font-weight: 800;
            }
            QFrame#statusBoard {
                background: #f7fafb;
                border: 1px solid #e3edf1;
                border-radius: 16px;
            }
            QLabel#metricCaption {
                color: #6a7b86;
                font-size: 11px;
                font-weight: 800;
            }
            QLabel#metricValue {
                color: #102a3a;
                font-size: 16px;
                font-weight: 800;
            }
            QLabel#metricValueSmall {
                color: #253846;
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#sectionHint,
            QLabel#mutedLabel {
                color: #647682;
                font-weight: 700;
            }
            QLabel#fieldLabel {
                color: #455966;
                font-size: 12px;
                font-weight: 800;
                padding-top: 4px;
            }
            QScrollArea#sideScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#workspaceScroll {
                background: #eef4f7;
                border: none;
            }
            QWidget#workspaceViewport,
            QWidget#workspacePanel {
                background: #eef4f7;
                border: none;
            }
            QWidget#sidePanel,
            QWidget#inlineField {
                background: transparent;
                border: none;
            }
            QLabel#noteLabel {
                color: #455966;
                background: #f6fafb;
                border: 1px solid #e2ecef;
                border-radius: 14px;
                padding: 10px 12px;
                font-weight: 600;
            }
            QLabel#errorBanner {
                background: #fff4f1;
                color: #a83b22;
                border: 1px solid #f2c4b7;
                border-radius: 14px;
                padding: 10px 12px;
                font-weight: 800;
            }
            QPushButton {
                border: none;
                border-radius: 11px;
                padding: 9px 12px;
                min-height: 28px;
                font-weight: 700;
            }
            QPushButton#primaryButton {
                background: #0f766e;
                color: #ffffff;
            }
            QPushButton#primaryButton:hover {
                background: #0b655f;
            }
            QPushButton#secondaryButton {
                background: #e7f3f4;
                color: #0b5c66;
                border: 1px solid #cbe2e6;
            }
            QPushButton#secondaryButton:hover {
                background: #d9ecef;
                border-color: #b4d7dd;
            }
            QPushButton#dangerButton {
                background: #fff0eb;
                color: #a83b22;
                border: 1px solid #f2c4b7;
            }
            QPushButton#dangerButton:hover {
                background: #ffe5dd;
            }
            QPushButton#ghostButton {
                background: transparent;
                color: #0b5c66;
                border: 1px solid #cfdee4;
            }
            QPushButton#ghostButton:hover {
                background: #eef7f8;
            }
            QPushButton:disabled {
                background: #d9e1e6;
                color: #94a3ad;
                border: 1px solid #d0d9df;
            }
            QLineEdit,
            QComboBox,
            QKeySequenceEdit {
                background: #f9fcfd;
                border: 1px solid #cbdbe2;
                border-radius: 11px;
                padding: 7px 10px;
                selection-background-color: #bfe6eb;
                min-height: 28px;
            }
            QLineEdit:focus,
            QComboBox:focus,
            QKeySequenceEdit:focus,
            QPlainTextEdit:focus {
                border: 1px solid #13a0ad;
                background: #ffffff;
            }
            QComboBox::drop-down {
                border: none;
                width: 28px;
            }
            QCheckBox {
                color: #253846;
                font-weight: 600;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 6px;
                border: 1px solid #b9cbd3;
                background: #f9fcfd;
            }
            QCheckBox::indicator:checked {
                background: #0f766e;
                border: 1px solid #0f766e;
            }
            QPlainTextEdit {
                background: #f9fcfd;
                border: 1px solid #cbdbe2;
                border-radius: 16px;
                padding: 12px 14px;
                selection-background-color: #bfe6eb;
                selection-color: #102a3a;
            }
            QPlainTextEdit#transcriptEditor {
                background: #fbfdfd;
                color: #152631;
                font-family: "Cascadia Mono", "Consolas", monospace;
                font-size: 13px;
                border-radius: 18px;
            }
            QPlainTextEdit#logView {
                background: #10212b;
                color: #d9eef0;
                border: 1px solid #183343;
                font-family: "Cascadia Mono", "Consolas", monospace;
                font-size: 12px;
            }
            QMenu {
                background: #ffffff;
                color: #17212b;
                border: 1px solid #cbdbe2;
                border-radius: 12px;
                padding: 6px;
            }
            QMenu::item {
                background: transparent;
                border-radius: 8px;
                padding: 7px 28px 7px 12px;
                margin: 1px 0;
            }
            QMenu::item:selected {
                background: #e7f3f4;
                color: #0b5c66;
            }
            QMenu::item:disabled {
                color: #9aa9b2;
            }
            QMenu::separator {
                height: 1px;
                background: #e3edf1;
                margin: 6px 8px;
            }
            QProgressBar {
                border: 1px solid #cbdbe2;
                border-radius: 12px;
                text-align: center;
                background: #eaf2f5;
                min-height: 24px;
                color: #102a3a;
                font-weight: 800;
            }
            QProgressBar::chunk {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #0f766e,
                    stop: 1 #17a4b3
                );
                border-radius: 11px;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 12px;
                margin: 4px;
            }
            QScrollBar::handle:vertical {
                background: #b9cbd3;
                border-radius: 6px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #8fa8b4;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0;
            }
            """
        )

    def _install_log_handler(self) -> None:
        if self.log_handler is not None:
            return

        self.log_handler = UiLogHandler()
        self.log_handler.setLevel(logging.INFO)
        self.log_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        self.log_handler.emitter.message.connect(self._append_log_message)
        logging.getLogger().addHandler(self.log_handler)

    def _remove_log_handler(self) -> None:
        if self.log_handler is None:
            return
        logging.getLogger().removeHandler(self.log_handler)
        self.log_handler = None

    def _append_log_message(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()
        self.progress_detail.setText(message)

    def _clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    def _set_push_feedback(
        self,
        status_text: str,
        detail_text: str | None = None,
        *,
        is_error: bool = False,
    ) -> None:
        self.push_to_transcribe_value.setText(status_text)
        if not detail_text:
            return
        if is_error:
            self._show_error(detail_text)
        else:
            self.progress_detail.setText(detail_text)

    @staticmethod
    def _normalise_cleanup_path(path: Path) -> Path:
        try:
            return path.expanduser().resolve()
        except OSError:
            return path.expanduser().absolute()

    def _build_transcription_job(
        self,
        audio_path: Path,
        cleanup_audio_path: Path | None,
    ) -> TranscriptionJobContext:
        normalised_audio_path = self._normalise_cleanup_path(audio_path)
        normalised_cleanup_path = (
            self._normalise_cleanup_path(cleanup_audio_path)
            if cleanup_audio_path is not None
            else None
        )
        delete_after_success = (
            normalised_cleanup_path is not None
            and self.settings.delete_recorded_audio_after_transcription
        )
        job = TranscriptionJobContext(
            job_id=uuid4().hex,
            audio_path=normalised_audio_path,
            cleanup_audio_path=normalised_cleanup_path,
            delete_recorded_audio_after_success=delete_after_success,
        )
        logger.info(
            "Transkriptionsjob erstellt: job=%s audio=%s cleanup=%s delete_after_success=%s",
            job.job_id,
            job.audio_path,
            job.cleanup_audio_path or "<kein Cleanup>",
            job.delete_recorded_audio_after_success,
        )
        return job

    def _cleanup_recorded_audio_after_success(self, result: TranscriptionResult) -> None:
        job = self.active_transcription_job
        if job is None:
            logger.warning("Kein aktiver Transkriptionsjob für Cleanup vorhanden; Audiodatei bleibt erhalten.")
            return

        result_audio_path = self._normalise_cleanup_path(result.audio_path)
        if result_audio_path != job.audio_path:
            logger.warning(
                "Cleanup blockiert: Ergebnis-Audio passt nicht zum aktiven Job. job=%s erwartet=%s erhalten=%s",
                job.job_id,
                job.audio_path,
                result_audio_path,
            )
            return

        cleanup_path = job.cleanup_audio_path
        if cleanup_path is None:
            logger.debug("Keine Cleanup-Aktion für importierten Transkriptionsjob %s", job.job_id)
            return
        if not job.delete_recorded_audio_after_success:
            logger.info("Automatisches Löschen ist für Transkriptionsjob %s deaktiviert: %s", job.job_id, cleanup_path)
            return
        if cleanup_path != job.audio_path:
            logger.warning(
                "Cleanup blockiert: Cleanup-Pfad weicht vom Job-Audio ab. job=%s audio=%s cleanup=%s",
                job.job_id,
                job.audio_path,
                cleanup_path,
            )
            return

        try:
            if not cleanup_path.exists():
                logger.info("Aufgenommene Audiodatei war bereits entfernt: %s", cleanup_path)
                return
            if not cleanup_path.is_file():
                logger.warning("Aufgenommene Audiodatei wurde nicht gelöscht, weil der Pfad keine Datei ist: %s", cleanup_path)
                return
            cleanup_path.unlink()
        except OSError:
            logger.exception("Aufgenommene Audiodatei konnte nach erfolgreicher Transkription nicht gelöscht werden: %s", cleanup_path)
        else:
            logger.info("Aufgenommene Audiodatei nach erfolgreicher Transkription gelöscht: %s", cleanup_path)

    def _connect_signals(self) -> None:
        self.start_button.clicked.connect(self.start_recording)
        self.stop_button.clicked.connect(self.stop_recording)
        self.import_button.clicked.connect(self.import_audio_file)
        self.export_button.clicked.connect(self.export_transcript)
        self.cancel_button.clicked.connect(self.cancel_transcription)
        self.copy_transcript_button.clicked.connect(self.copy_transcript_to_clipboard)
        self.help_button.clicked.connect(self.show_help)
        self.storage_button.clicked.connect(self.choose_output_directory)
        self.recording_source_combo.currentIndexChanged.connect(self._apply_recording_source_to_ui)
        self.timestamps_checkbox.toggled.connect(self._apply_timestamp_display)
        self.transcript_format_combo.currentIndexChanged.connect(self._apply_timestamp_display)
        self.refresh_devices_button.clicked.connect(self.reload_audio_devices)
        self.refresh_microphones_button.clicked.connect(self.reload_microphone_devices)
        self.refresh_app_audio_button.clicked.connect(
            lambda: self.reload_application_audio_sessions(show_feedback=True, force_refresh=True)
        )
        self.save_settings_button.clicked.connect(
            lambda: self._persist_form_settings(show_feedback=True)
        )
        self.duration_timer.timeout.connect(self._update_recording_duration)
        self.transcript_edit.textChanged.connect(self._refresh_actions)
        self.transcript_edit.copy_requested.connect(self.copy_transcript_selection_to_clipboard)

        self.hotkey_manager.start_requested.connect(self._handle_start_hotkey)
        self.hotkey_manager.stop_requested.connect(self._handle_stop_hotkey)
        self.hotkey_manager.toggle_requested.connect(self._handle_toggle_hotkey)
        self.hotkey_manager.push_pressed.connect(self._handle_push_to_transcribe_pressed)
        self.hotkey_manager.push_released.connect(self._handle_push_to_transcribe_released)

    def show_help(self) -> None:
        dialog = QDialog(self)
        dialog.setObjectName("helpDialog")
        dialog.setWindowTitle(f"Hilfe - {APP_NAME}")
        dialog.resize(860, 720)
        dialog.setMinimumSize(680, 520)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        header = QFrame()
        header.setObjectName("helpHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(22, 18, 22, 18)
        header_layout.setSpacing(6)

        title = QLabel(f"{APP_NAME} Hilfe")
        title.setObjectName("helpTitle")
        subtitle = QLabel("Bedienung, Aufnahmequellen, Hotkeys, Transkription und Export auf einen Blick.")
        subtitle.setObjectName("helpSubtitle")
        subtitle.setWordWrap(True)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        layout.addWidget(header)

        help_card = QFrame()
        help_card.setObjectName("helpCard")
        help_card_layout = QVBoxLayout(help_card)
        help_card_layout.setContentsMargins(0, 0, 0, 0)

        help_view = QTextBrowser()
        help_view.setObjectName("helpText")
        help_view.setOpenExternalLinks(False)
        help_view.document().setDocumentMargin(18)
        help_view.document().setDefaultStyleSheet(
            """
            body {
                color: #253846;
                font-family: "Segoe UI", "Noto Sans", sans-serif;
                font-size: 14px;
                line-height: 1.45;
            }
            h1 {
                color: #102a3a;
                font-size: 26px;
                font-weight: 800;
                margin: 0 0 16px 0;
            }
            h2 {
                color: #0b5c66;
                font-size: 18px;
                font-weight: 800;
                margin: 22px 0 8px 0;
            }
            p {
                margin: 0 0 10px 0;
            }
            ul {
                margin: 4px 0 12px 18px;
            }
            li {
                margin-bottom: 7px;
            }
            """
        )
        help_view.setMarkdown(self._build_help_markdown())
        help_card_layout.addWidget(help_view)
        layout.addWidget(help_card, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.setObjectName("helpButtons")
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.setText("Schließen")
            close_button.setObjectName("helpCloseButton")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.setStyleSheet(
            """
            QDialog#helpDialog {
                background: #eef4f7;
            }
            QFrame#helpHeader {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #102a3a,
                    stop: 0.58 #0e5d68,
                    stop: 1 #1f7a6d
                );
                border: 1px solid rgba(255, 255, 255, 0.18);
                border-radius: 22px;
            }
            QLabel#helpTitle {
                color: #f7fbfc;
                font-size: 26px;
                font-weight: 800;
            }
            QLabel#helpSubtitle {
                color: #cfe3e8;
                font-size: 14px;
                font-weight: 600;
            }
            QFrame#helpCard {
                background: #ffffff;
                border: 1px solid #dce6eb;
                border-radius: 18px;
            }
            QTextBrowser#helpText {
                background: #ffffff;
                color: #253846;
                border: none;
                border-radius: 18px;
                padding: 4px;
                selection-background-color: #bfe6eb;
                selection-color: #102a3a;
            }
            QDialogButtonBox#helpButtons {
                background: transparent;
            }
            QPushButton#helpCloseButton {
                background: #0f766e;
                color: #ffffff;
                border: none;
                border-radius: 11px;
                padding: 9px 18px;
                min-height: 28px;
                font-weight: 700;
            }
            QPushButton#helpCloseButton:hover {
                background: #0b655f;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 12px;
                margin: 4px;
            }
            QScrollBar::handle:vertical {
                background: #b9cbd3;
                border-radius: 6px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #8fa8b4;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0;
            }
            """
        )
        dialog.exec()

    def _build_help_markdown(self) -> str:
        languages = ", ".join(LANGUAGE_OPTIONS.keys())
        models = ", ".join(MODEL_OPTIONS)
        performance_profiles = ", ".join(TRANSCRIPTION_PROFILE_OPTIONS.keys())
        sources = ", ".join(RECORDING_SOURCE_OPTIONS.keys())
        start_hotkey = keyboard_to_qt_hotkey(self.settings.start_hotkey) or "nicht gesetzt"
        stop_hotkey = keyboard_to_qt_hotkey(self.settings.stop_hotkey) or "nicht gesetzt"
        push_hotkey = keyboard_to_qt_hotkey(self.settings.push_to_transcribe_hotkey) or "nicht gesetzt"
        timestamp_mode = "aktiv" if self.timestamps_checkbox.isChecked() else "aus"
        transcript_format = self.transcript_format_combo.currentText() or "Kompakt"
        cleanup_mode = "aktiv" if self.cleanup_recordings_checkbox.isChecked() else "aus"

        return f"""# {APP_NAME} Hilfe

## Zweck
{APP_NAME} nimmt Audio auf und transkribiert es lokal mit faster-whisper. Die App ist für Calls, Meetings, Systemaudio und importierte Audiodateien gedacht.

## Aufnahmequellen
- Verfügbare Aufnahmearten: {sources}.
- Systemaudio nutzt unter Windows WASAPI-Loopback und nimmt Wiedergabeaudio auf.
- Mikrofon nutzt normale Eingabegeräte und wird getrennt von Loopback-Geräten behandelt.
- Systemaudio + Mikrofon nimmt beide Quellen parallel auf und transkribiert danach eine gemeinsame gemischte WAV-Datei.
- App-Audio ist ein eigener Windows-Modus für erkannte Audio-Sessions laufender Apps. Die Option wird nur nutzbar, wenn der native Helper und Windows Process Loopback verfügbar sind.
- App-Audio + Mikrofon ist in dieser Version bewusst kein eigener Modus.
- Die passenden Listen können mit "Loopback aktualisieren", "Mikrofone aktualisieren" und "Apps aktualisieren" neu geladen werden.

## Aufnahme und Transkription
- "Aufnahme starten" beginnt eine WAV-Aufnahme mit der aktuell gewählten Quelle und dem gewählten Gerät.
- "Aufnahme stoppen" beendet die Aufnahme und startet danach automatisch die Transkription im Hintergrund.
- Aufgenommene WAV-Dateien werden im Unterordner "recordings" des Speicherpfads abgelegt.
- Wenn automatisches Löschen aktiv ist, werden app-eigene WAV-Aufnahmen erst nach erfolgreicher Transkription entfernt. Importierte Dateien werden nie automatisch gelöscht.
- Transkripte werden automatisch als TXT im Unterordner "transcripts" gespeichert.
- Es kann immer nur eine Aufnahme oder Transkription gleichzeitig laufen.

## Import
- "Audio-Datei importieren" akzeptiert WAV, MP3, M4A, FLAC, OGG und AAC.
- Importierte Dateien werden nicht neu aufgenommen, sondern direkt transkribiert.

## Hotkeys
- Start-Hotkey aktuell: {start_hotkey}.
- Stopp-Hotkey aktuell: {stop_hotkey}.
- Push-to-Transcribe aktuell: {push_hotkey}.
- Wenn Start- und Stopp-Hotkey identisch sind, arbeitet die App als Toggle.
- Push-to-Transcribe startet beim Drücken, stoppt beim Loslassen, transkribiert automatisch und kopiert den fertigen Text in die Zwischenablage.
- Globale Hotkeys können von Windows oder anderen Programmen blockiert werden; in diesem Fall zeigt die App einen Hotkey-Fehler an.

## Fortschritt, Status und Abbruch
- Der Bereich "Status" zeigt Aufnahme, Transkription, Fertig- und Fehlerzustand.
- "Push-to-Transcribe" zeigt den Hold-to-Record-Zustand: bereit, Aufnahme, Transkription oder kopiert.
- Die Fortschrittsleiste zeigt den Transkriptionsfortschritt.
- "Transkription abbrechen" fordert einen Abbruch an; der aktuelle Whisper-Chunk kann noch beendet werden.
- Fehler erscheinen im Fehlerbanner und im Log.

## Transkript und Zwischenablage
- Das Transkriptfeld zeigt den erkannten Text und kann bearbeitet werden.
- Textformat aktuell: {transcript_format}. Kompakt verbindet Segmente zu natürlicheren Absätzen, ohne Text umzuschreiben.
- "Transkript kopieren" kopiert den kompletten sichtbaren Transkripttext in die Zwischenablage.
- Markierter Text kann ebenfalls kopiert werden.
- Push-to-Transcribe kopiert nach erfolgreicher Transkription den finalen Text automatisch in die Zwischenablage.
- Bei Fehlern oder leerem Text wird die Zwischenablage nicht überschrieben.

## Export
- "Transkript exportieren" unterstützt TXT, SRT und VTT.
- TXT folgt der Einstellung "Timestamps anzeigen".
- SRT und VTT werden aus den gespeicherten Segmentzeiten erzeugt, weil Untertiteldateien Zeitinformationen benötigen.

## Einstellungen
- Speicherpfad: legt fest, wo "recordings" und "transcripts" angelegt werden.
- Sprache: {languages}.
- Whisper-Modell: {models}.
- Performance: {performance_profiles}. "Schnell" ist für niedrige CPU-Last optimiert, "Qualität" kann genauer, aber langsamer sein.
- Timestamps anzeigen: derzeit {timestamp_mode}.
- Aufgenommene Audiodateien automatisch löschen: derzeit {cleanup_mode}.
- Aufnahmequelle, Systemaudio-Gerät und Mikrofon-Gerät werden persistent gespeichert.
- Start-, Stopp- und Push-to-Transcribe-Hotkeys werden ebenfalls gespeichert.
- "Einstellungen speichern" schreibt die Werte dauerhaft und registriert die Hotkeys neu.

## Hinweise und typische Probleme
- WASAPI-Loopback ist in dieser App ein Windows-Pfad. Wenn kein Wiedergabegerät aktiv ist, kann die Loopback-Liste leer sein.
- Wenn ein gespeichertes Audio- oder Mikrofongerät nicht mehr existiert, bitte die Geräteliste aktualisieren und neu wählen.
- Das erste Transkript mit einem Modell kann länger dauern, weil das Modell lokal geladen oder heruntergeladen wird.
- Schreibfehler können auftreten, wenn Zielordner fehlen, Rechte fehlen oder eine Exportdatei geöffnet/gesperrt ist.
- Mikrofonzugriff kann fehlschlagen, wenn das Gerät nicht verfügbar ist oder von einer anderen App blockiert wird.
"""

    def reload_audio_devices(self, show_feedback: bool = True) -> None:
        previous_selection = str(self.audio_device_combo.currentData() or self.settings.audio_device_id)
        self.audio_device_combo.clear()
        self.audio_device_combo.addItem("Standard-Wiedergabegerät", "")

        try:
            self.available_audio_devices = self.audio_service.list_loopback_devices()
        except AudioCaptureError as exc:
            logger.exception("Loopback-Geräteliste konnte nicht geladen werden")
            self.available_audio_devices = []
            self._show_error(str(exc))
            if show_feedback:
                QMessageBox.warning(self, "Audiogeräte", str(exc))
            return

        for device in self.available_audio_devices:
            self.audio_device_combo.addItem(device.display_name, device.id)

        self._set_combo_data(self.audio_device_combo, previous_selection)
        if self.audio_device_combo.currentIndex() < 0:
            self.audio_device_combo.setCurrentIndex(0)

        self._clear_error()
        logger.info("Loopback-Geräteliste aktualisiert")
        if show_feedback:
            self.progress_detail.setText("Loopback-Geräteliste aktualisiert.")

    def reload_microphone_devices(self, show_feedback: bool = True) -> None:
        previous_selection = str(
            self.microphone_device_combo.currentData() or self.settings.microphone_device_id
        )
        self.microphone_device_combo.clear()
        self.microphone_device_combo.addItem("Standard-Mikrofon", "")

        try:
            self.available_microphone_devices = self.audio_service.list_microphone_devices()
        except AudioCaptureError as exc:
            logger.exception("Mikrofonliste konnte nicht geladen werden")
            self.available_microphone_devices = []
            self._show_error(str(exc))
            if show_feedback:
                QMessageBox.warning(self, "Mikrofone", str(exc))
            return

        for device in self.available_microphone_devices:
            self.microphone_device_combo.addItem(device.display_name, device.id)

        self._set_combo_data(self.microphone_device_combo, previous_selection)
        if self.microphone_device_combo.currentIndex() < 0:
            self.microphone_device_combo.setCurrentIndex(0)

        self._clear_error()
        logger.info("Mikrofonliste aktualisiert")
        if show_feedback:
            self.progress_detail.setText("Mikrofonliste aktualisiert.")

    def reload_application_audio_sessions(
        self,
        show_feedback: bool = True,
        force_refresh: bool = False,
    ) -> None:
        previous_selection = str(
            self.app_audio_session_combo.currentData() or self.settings.application_audio_session_id
        )
        self.app_audio_session_combo.clear()
        self.app_audio_session_combo.addItem("Keine App-Audio-Session ausgewählt", "")

        self.app_audio_capability = self.audio_service.application_audio_capability(
            force_refresh=force_refresh
        )
        if not self.app_audio_capability.supported:
            self.available_app_audio_sessions = []
            self.app_audio_hint.setText(
                f"App-Audio ist nicht verfügbar: {self.app_audio_capability.reason}"
            )
            self._set_recording_source_item_enabled(RECORDING_SOURCE_APPLICATION_AUDIO, False)
            if self._current_recording_source() == RECORDING_SOURCE_APPLICATION_AUDIO:
                self._set_combo_data(self.recording_source_combo, RECORDING_SOURCE_SYSTEM_AUDIO)
            logger.warning("App-Audio deaktiviert: %s", self.app_audio_capability.reason)
            if show_feedback:
                self.progress_detail.setText("App-Audio ist auf diesem System nicht verfügbar.")
            return

        self._set_recording_source_item_enabled(RECORDING_SOURCE_APPLICATION_AUDIO, True)
        try:
            self.available_app_audio_sessions = self.audio_service.list_application_audio_sessions(
                force_refresh=force_refresh
            )
        except AudioCaptureError as exc:
            logger.exception("App-Audio-Sessions konnten nicht geladen werden")
            self.available_app_audio_sessions = []
            self.app_audio_hint.setText(f"App-Audio-Sessions konnten nicht gelesen werden: {exc}")
            self._show_error(str(exc))
            if show_feedback:
                QMessageBox.warning(self, "App-Audio", str(exc))
            return

        for session in self.available_app_audio_sessions:
            self.app_audio_session_combo.addItem(session.display_label, session.id)

        self._set_combo_data(self.app_audio_session_combo, previous_selection)
        if self.app_audio_session_combo.currentIndex() < 0:
            self.app_audio_session_combo.setCurrentIndex(0)

        if self.available_app_audio_sessions:
            self.app_audio_hint.setText(
                "App-Audio nutzt Windows Process Loopback und nimmt nur die gewählte Audio-App auf."
            )
        else:
            self.app_audio_hint.setText(
                "Keine laufende App-Audio-Session erkannt. Starte Audio in der Ziel-App und aktualisiere die Liste."
            )
        logger.info("App-Audio-Sessions aktualisiert")
        if show_feedback:
            self.progress_detail.setText("App-Audio-Sessions aktualisiert.")

    def _apply_recording_source_to_ui(self, *_args) -> None:
        current_source = str(self.recording_source_combo.currentData() or RECORDING_SOURCE_SYSTEM_AUDIO)
        uses_systemaudio = current_source in {
            RECORDING_SOURCE_SYSTEM_AUDIO,
            RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE,
        }
        uses_microphone = current_source in {
            RECORDING_SOURCE_MICROPHONE,
            RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE,
        }
        uses_app_audio = current_source == RECORDING_SOURCE_APPLICATION_AUDIO
        can_change_devices = not self.audio_service.is_recording and not self._is_transcribing()
        self.audio_device_combo.setEnabled(uses_systemaudio)
        self.refresh_devices_button.setEnabled(uses_systemaudio and can_change_devices)
        self.microphone_device_combo.setEnabled(uses_microphone)
        self.refresh_microphones_button.setEnabled(
            uses_microphone and can_change_devices
        )
        app_audio_available = self.app_audio_capability.supported
        self.app_audio_session_combo.setEnabled(uses_app_audio and app_audio_available)
        self.refresh_app_audio_button.setEnabled(can_change_devices)
        self.app_audio_hint.setVisible(uses_app_audio or not app_audio_available)

    def _warn_if_saved_device_missing(self) -> None:
        selected_source = self.settings.recording_source or RECORDING_SOURCE_SYSTEM_AUDIO
        needs_systemaudio = selected_source in {
            RECORDING_SOURCE_SYSTEM_AUDIO,
            RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE,
        }
        needs_microphone = selected_source in {
            RECORDING_SOURCE_MICROPHONE,
            RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE,
        }
        needs_app_audio = selected_source == RECORDING_SOURCE_APPLICATION_AUDIO
        if (
            needs_systemaudio
            and self.settings.audio_device_id
            and self.audio_device_combo.findData(self.settings.audio_device_id) < 0
        ):
            self._show_error(
                "Das zuletzt gespeicherte WASAPI-Loopback-Gerät ist nicht verfügbar. Bitte ein anderes Gerät wählen."
            )
            return

        if (
            needs_microphone
            and self.settings.microphone_device_id
            and self.microphone_device_combo.findData(self.settings.microphone_device_id) < 0
        ):
            self._show_error(
                "Das zuletzt gespeicherte Mikrofon ist nicht verfügbar. Bitte ein anderes Gerät wählen."
            )
            return

        if (
            needs_app_audio
            and self.settings.application_audio_session_id
            and self.app_audio_session_combo.findData(self.settings.application_audio_session_id) < 0
        ):
            self._show_error(
                "Die zuletzt gespeicherte App-Audio-Session ist nicht verfügbar. Bitte die App-Liste aktualisieren."
            )

    def _load_settings_into_form(self) -> None:
        self.storage_input.setText(str(self.settings.output_path()))
        self._set_combo_data(self.language_combo, self.settings.language)
        self._set_combo_data(self.model_combo, self.settings.model_size)
        self._set_combo_data(
            self.performance_profile_combo,
            getattr(self.settings, "transcription_profile", TRANSCRIPTION_PROFILE_FAST),
        )
        self.timestamps_checkbox.setChecked(self.settings.show_timestamps)
        self._set_combo_data(
            self.transcript_format_combo,
            getattr(self.settings, "transcript_format", TRANSCRIPT_FORMAT_COMPACT),
        )
        self.cleanup_recordings_checkbox.setChecked(
            self.settings.delete_recorded_audio_after_transcription
        )
        saved_source = self.settings.recording_source
        if saved_source == RECORDING_SOURCE_APPLICATION_AUDIO and not self.app_audio_capability.supported:
            saved_source = RECORDING_SOURCE_SYSTEM_AUDIO
            self._show_error(
                f"App-Audio ist nicht verfügbar: {self.app_audio_capability.reason}"
            )
        self._set_combo_data(self.recording_source_combo, saved_source)
        self._set_combo_data(self.audio_device_combo, self.settings.audio_device_id)
        if self.settings.audio_device_id and self.audio_device_combo.findData(self.settings.audio_device_id) < 0:
            self.audio_device_combo.setCurrentIndex(0)
        self._set_combo_data(self.microphone_device_combo, self.settings.microphone_device_id)
        if (
            self.settings.microphone_device_id
            and self.microphone_device_combo.findData(self.settings.microphone_device_id) < 0
        ):
            self.microphone_device_combo.setCurrentIndex(0)
        self._set_combo_data(self.app_audio_session_combo, self.settings.application_audio_session_id)
        if (
            self.settings.application_audio_session_id
            and self.app_audio_session_combo.findData(self.settings.application_audio_session_id) < 0
        ):
            self.app_audio_session_combo.setCurrentIndex(0)
        self.start_hotkey_edit.setKeySequence(
            QKeySequence.fromString(
                keyboard_to_qt_hotkey(self.settings.start_hotkey),
                QKeySequence.SequenceFormat.PortableText,
            )
        )
        self.stop_hotkey_edit.setKeySequence(
            QKeySequence.fromString(
                keyboard_to_qt_hotkey(self.settings.stop_hotkey),
                QKeySequence.SequenceFormat.PortableText,
            )
        )
        self.push_to_transcribe_hotkey_edit.setKeySequence(
            QKeySequence.fromString(
                keyboard_to_qt_hotkey(self.settings.push_to_transcribe_hotkey),
                QKeySequence.SequenceFormat.PortableText,
            )
        )

    def _register_hotkeys(self, notify_on_error: bool) -> bool:
        try:
            self.hotkey_manager.update_hotkeys(
                self.settings.start_hotkey,
                self.settings.stop_hotkey,
                self.settings.push_to_transcribe_hotkey,
            )
        except HotkeyRegistrationError as exc:
            logger.exception("Hotkey-Registrierung fehlgeschlagen")
            self._set_push_feedback("Push: Hotkey-Fehler", str(exc), is_error=True)
            if notify_on_error:
                QMessageBox.warning(self, "Hotkey-Fehler", str(exc))
            return False
        self._clear_error()
        if self.push_to_transcribe_state == PushToTranscribeState.IDLE:
            self._set_push_feedback("Push: bereit")
        logger.info(
            "Hotkeys aktiv: start=%s stop=%s push=%s",
            self.settings.start_hotkey,
            self.settings.stop_hotkey,
            self.settings.push_to_transcribe_hotkey,
        )
        return True

    def _persist_form_settings(self, show_feedback: bool) -> bool:
        try:
            settings = self._collect_settings_from_form()
        except ValueError as exc:
            logger.warning("Ungueltige Einstellungen: %s", exc)
            self._show_error(str(exc))
            QMessageBox.warning(self, "Ungültige Einstellungen", str(exc))
            return False

        previous_settings = self.settings
        self.settings = settings
        if not self._register_hotkeys(notify_on_error=show_feedback):
            self.settings = previous_settings
            try:
                self.hotkey_manager.update_hotkeys(
                    previous_settings.start_hotkey,
                    previous_settings.stop_hotkey,
                    previous_settings.push_to_transcribe_hotkey,
                )
                logger.info("Vorherige Hotkeys nach Registrierungsfehler wiederhergestellt")
            except HotkeyRegistrationError:
                logger.exception("Vorherige Hotkeys konnten nicht wiederhergestellt werden")
            return False

        self.settings_store.save(self.settings)
        self._clear_error()
        logger.info("Einstellungen gespeichert")
        if show_feedback:
            self.progress_detail.setText("Einstellungen gespeichert und Hotkeys aktualisiert.")
        return True

    def _collect_settings_from_form(self) -> AppSettings:
        output_directory = self.storage_input.text().strip()
        if not output_directory:
            raise ValueError("Bitte einen Speicherpfad auswählen.")

        start_hotkey = normalize_hotkey(self._sequence_to_text(self.start_hotkey_edit))
        stop_hotkey = normalize_hotkey(self._sequence_to_text(self.stop_hotkey_edit))
        push_to_transcribe_hotkey = normalize_hotkey(
            self._sequence_to_text(self.push_to_transcribe_hotkey_edit)
        )
        if not start_hotkey and not stop_hotkey:
            raise ValueError("Mindestens ein globaler Hotkey muss gesetzt sein.")
        if not push_to_transcribe_hotkey:
            raise ValueError("Bitte einen Push-to-Transcribe-Hotkey setzen.")

        if not start_hotkey:
            start_hotkey = stop_hotkey
        if not stop_hotkey:
            stop_hotkey = start_hotkey
        if push_to_transcribe_hotkey in {start_hotkey, stop_hotkey}:
            raise ValueError(
                "Der Push-to-Transcribe-Hotkey muss sich von Start- und Stopp-Hotkey unterscheiden."
            )

        selected_recording_source = str(self.recording_source_combo.currentData() or RECORDING_SOURCE_SYSTEM_AUDIO)
        selected_app_audio_session_id = str(self.app_audio_session_combo.currentData() or "")
        selected_app_audio_session_label = self.app_audio_session_combo.currentText() or ""
        if selected_recording_source == RECORDING_SOURCE_APPLICATION_AUDIO:
            if not self.app_audio_capability.supported:
                raise ValueError(f"App-Audio ist nicht verfügbar: {self.app_audio_capability.reason}")
            if not selected_app_audio_session_id:
                raise ValueError("Bitte eine App-Audio-Session auswählen oder die App-Liste aktualisieren.")

        settings = AppSettings(
            output_directory=output_directory,
            start_hotkey=start_hotkey,
            stop_hotkey=stop_hotkey,
            push_to_transcribe_hotkey=push_to_transcribe_hotkey,
            language=str(self.language_combo.currentData()),
            model_size=str(self.model_combo.currentData()),
            transcription_profile=str(
                self.performance_profile_combo.currentData() or TRANSCRIPTION_PROFILE_FAST
            ),
            transcript_format=str(
                self.transcript_format_combo.currentData() or TRANSCRIPT_FORMAT_COMPACT
            ),
            show_timestamps=self.timestamps_checkbox.isChecked(),
            delete_recorded_audio_after_transcription=self.cleanup_recordings_checkbox.isChecked(),
            recording_source=selected_recording_source,
            audio_device_id=str(self.audio_device_combo.currentData() or ""),
            microphone_device_id=str(self.microphone_device_combo.currentData() or ""),
            application_audio_session_id=selected_app_audio_session_id,
            application_audio_session_label=selected_app_audio_session_label,
        )
        settings.ensure_directories()
        return settings

    @staticmethod
    def _sequence_to_text(editor: QKeySequenceEdit) -> str:
        return editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _set_recording_source_item_enabled(self, source_value: str, enabled: bool) -> None:
        index = self.recording_source_combo.findData(source_value)
        if index < 0:
            return
        item = self.recording_source_combo.model().item(index)
        if item is not None:
            item.setEnabled(enabled)

    def _current_recording_source(self) -> str:
        return str(self.recording_source_combo.currentData() or RECORDING_SOURCE_SYSTEM_AUDIO)

    def _current_recording_source_label(self) -> str:
        return self.recording_source_combo.currentText() or "Systemaudio"

    def _current_selected_device_label(self) -> str:
        current_source = self._current_recording_source()
        if current_source == RECORDING_SOURCE_MICROPHONE:
            return self.microphone_device_combo.currentText() or "Standard-Mikrofon"
        if current_source == RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE:
            system_label = self.audio_device_combo.currentText() or "Standard-Wiedergabegerät"
            microphone_label = self.microphone_device_combo.currentText() or "Standard-Mikrofon"
            return f"Systemaudio: {system_label}\nMikrofon: {microphone_label}"
        if current_source == RECORDING_SOURCE_APPLICATION_AUDIO:
            return self.app_audio_session_combo.currentText() or "Keine App-Audio-Session ausgewählt"
        return self.audio_device_combo.currentText() or "Standard-Wiedergabegerät"

    def _apply_timestamp_display(self) -> None:
        if not self.current_timed_transcript_text:
            self.transcript_format_combo.setEnabled(not self.timestamps_checkbox.isChecked())
            return
        if self.timestamps_checkbox.isChecked():
            display_text = self.current_timed_transcript_text
        else:
            display_text = format_transcript_text(
                self.current_timed_transcript_text,
                include_timestamps=False,
                transcript_format=str(
                    self.transcript_format_combo.currentData() or TRANSCRIPT_FORMAT_COMPACT
                ),
            )
        self.transcript_format_combo.setEnabled(not self.timestamps_checkbox.isChecked())
        self.transcript_edit.setPlainText(display_text)
        self._refresh_actions()

    def choose_output_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Speicherpfad auswählen",
            self.storage_input.text() or str(self.settings.output_path()),
        )
        if selected:
            self.storage_input.setText(selected)

    def start_recording(self) -> None:
        self._start_recording(persist_settings=True, show_dialogs=True, initiated_by_push=False)

    def _start_recording(
        self,
        *,
        persist_settings: bool,
        show_dialogs: bool,
        initiated_by_push: bool,
    ) -> None:
        if self._is_transcribing():
            logger.warning("Aufnahme waehrend laufender Transkription blockiert")
            self._show_error("Während einer laufenden Transkription kann keine neue Aufnahme gestartet werden.")
            QMessageBox.information(
                self,
                "Bitte warten",
                "Während einer laufenden Transkription kann keine neue Aufnahme gestartet werden.",
            )
            return

        if persist_settings and not self._persist_form_settings(show_feedback=False):
            return

        selected_source_label = self._current_recording_source_label()
        selected_device_label = self._current_selected_device_label()
        try:
            output_path = self.audio_service.start_recording(self.settings)
        except AudioCaptureError as exc:
            logger.exception("Aufnahme konnte nicht gestartet werden")
            self._show_error(str(exc))
            QMessageBox.critical(self, "Aufnahme fehlgeschlagen", str(exc))
            return

        self._clear_error()
        logger.info("Aufnahme gestartet: %s", output_path)
        self.current_audio_path = output_path
        self.current_transcript_path = None
        self.current_timed_transcript_text = ""
        self.transcript_edit.clear()
        self.progress_bar.setValue(0)
        self.progress_detail.setText(f"{selected_source_label} wird aufgenommen ...")
        self.status_value.setText(f"Aufnahme läuft ({selected_source_label})")
        self.source_value.setText(f"{selected_source_label}\n{selected_device_label}\n{output_path}")
        self.autosave_value.setText("Die WAV-Datei wird bereits geschrieben.")
        self.recording_started_at = datetime.now()
        self.recording_value.setText("00:00")
        self.duration_timer.start()
        self._refresh_actions()

    def stop_recording(self) -> None:
        if self.push_to_transcribe_state == PushToTranscribeState.RECORDING:
            logger.info("Manueller Stopp beendet aktive Push-to-Transcribe-Aufnahme")
            self._set_push_feedback(
                "Push: manueller Stopp",
                "Aktive Push-to-Transcribe-Aufnahme wird gestoppt ...",
            )
            if not self._stop_push_to_transcribe_recording():
                self._refresh_actions()
            return

        try:
            result = self.audio_service.stop_recording()
        except AudioCaptureError as exc:
            logger.exception("Aufnahme konnte nicht gestoppt werden")
            self._show_error(str(exc))
            QMessageBox.warning(self, "Aufnahme stoppen", str(exc))
            return

        self._clear_error()
        logger.info(
            "Aufnahme gestoppt, automatische Transkription startet für %s über %s",
            result.file_path,
            result.capture_device,
        )
        self.duration_timer.stop()
        self.recording_started_at = None
        self.current_audio_path = result.file_path
        self.status_value.setText("Aufnahme gespeichert")
        self.source_value.setText(f"{result.capture_device}\n{result.file_path}")
        self.recording_value.setText(self._format_duration(result.duration_seconds))
        self.progress_detail.setText(
            f"Aufnahme abgeschlossen ({result.capture_device}). Automatische Transkription startet ..."
        )
        self.autosave_value.setText(str(result.file_path))
        self._refresh_actions()
        self._start_transcription(result.file_path, cleanup_audio_path=result.file_path)

    def _transition_push_to_transcribe(
        self,
        next_state: PushToTranscribeState,
        status_text: str,
    ) -> bool:
        if next_state == self.push_to_transcribe_state:
            self._set_push_feedback(status_text)
            return True

        allowed_states = PUSH_TO_TRANSCRIBE_TRANSITIONS.get(self.push_to_transcribe_state, set())
        if next_state not in allowed_states:
            logger.warning(
                "Illegale Push-to-Transcribe State-Transition blockiert: %s -> %s",
                self.push_to_transcribe_state.value,
                next_state.value,
            )
            self._set_push_feedback(
                "Push: Zustand blockiert",
                "Push-to-Transcribe ist gerade nicht in einem gültigen Zustand für diese Aktion.",
                is_error=True,
            )
            return False

        logger.info(
            "Push-to-Transcribe State: %s -> %s",
            self.push_to_transcribe_state.value,
            next_state.value,
        )
        self.push_to_transcribe_state = next_state
        self._set_push_feedback(status_text)
        return True

    def _reset_push_to_transcribe_state(self, status_text: str) -> None:
        if self.push_to_transcribe_state != PushToTranscribeState.IDLE:
            logger.info(
                "Push-to-Transcribe State: %s -> idle",
                self.push_to_transcribe_state.value,
            )
        self.push_to_transcribe_state = PushToTranscribeState.IDLE
        self.hotkey_manager.reset_push_state()
        self._set_push_feedback(status_text)

    def _start_push_to_transcribe_recording(self) -> bool:
        if self.push_to_transcribe_state != PushToTranscribeState.IDLE:
            logger.warning(
                "Push-to-Transcribe ignoriert: Zustand=%s",
                self.push_to_transcribe_state.value,
            )
            self._set_push_feedback(
                "Push: ignoriert",
                "Push-to-Transcribe ist bereits beschäftigt.",
            )
            return False
        if self._is_transcribing():
            logger.warning("Push-to-Transcribe ignoriert: Transkription läuft bereits")
            self._set_push_feedback(
                "Push: ignoriert",
                "Push-to-Transcribe ist blockiert, solange eine Transkription läuft.",
                is_error=True,
            )
            return False
        if self.audio_service.is_recording:
            logger.warning("Push-to-Transcribe ignoriert: Aufnahme läuft bereits")
            self._set_push_feedback(
                "Push: ignoriert",
                "Push-to-Transcribe ist blockiert, solange eine andere Aufnahme läuft.",
                is_error=True,
            )
            return False

        source_label = next(
            (
                label
                for label, source_value in RECORDING_SOURCE_OPTIONS.items()
                if source_value == self.settings.recording_source
            ),
            "Systemaudio",
        )
        try:
            output_path = self.audio_service.start_recording(self.settings)
        except AudioCaptureError as exc:
            logger.exception("Push-to-Transcribe Aufnahme konnte nicht gestartet werden")
            self._set_push_feedback("Fehler: Push-Aufnahme nicht gestartet", str(exc), is_error=True)
            return False

        self._clear_error()
        logger.info("Push-to-Transcribe Aufnahme gestartet: %s", output_path)
        self.current_audio_path = output_path
        self.current_transcript_path = None
        self.current_timed_transcript_text = ""
        self.transcript_edit.clear()
        self.progress_bar.setValue(0)
        self.progress_detail.setText(f"Push-to-Transcribe nimmt {source_label} auf ...")
        self.status_value.setText(f"Push-Aufnahme läuft ({source_label})")
        self.source_value.setText(f"Push-to-Transcribe: {source_label}\n{output_path}")
        self.autosave_value.setText("Die WAV-Datei wird bereits geschrieben.")
        self.recording_started_at = datetime.now()
        self.recording_value.setText("00:00")
        self.duration_timer.start()
        self._transition_push_to_transcribe(
            PushToTranscribeState.RECORDING,
            "Push: Aufnahme läuft",
        )
        self._refresh_actions()
        return True

    def _stop_push_to_transcribe_recording(self) -> bool:
        if self.push_to_transcribe_state != PushToTranscribeState.RECORDING:
            logger.warning(
                "Push-to-Transcribe Release ignoriert: Zustand=%s",
                self.push_to_transcribe_state.value,
            )
            return False
        if not self.audio_service.is_recording:
            logger.warning("Push-to-Transcribe Release ignoriert: keine Aufnahme aktiv")
            self._reset_push_to_transcribe_state("Push: keine Aufnahme aktiv")
            return False

        try:
            result = self.audio_service.stop_recording()
        except AudioCaptureError as exc:
            logger.exception("Push-to-Transcribe Aufnahme konnte nicht gestoppt werden")
            if self.audio_service.is_recording:
                reset_status = "Fehler: Aufnahme läuft noch"
                self._set_push_feedback(
                    reset_status,
                    (
                        f"{exc} Bitte nutze den Stopp-Button erneut oder prüfe das Audiogerät, "
                        "falls die Aufnahme nicht endet."
                    ),
                    is_error=True,
                )
            else:
                reset_status = "Fehler: Push-Aufnahme nicht gestoppt"
                self._set_push_feedback(
                    reset_status,
                    str(exc),
                    is_error=True,
                )
            self._reset_push_to_transcribe_state(reset_status)
            return False

        self._clear_error()
        logger.info(
            "Push-to-Transcribe Aufnahme gestoppt, Transkription startet für %s",
            result.file_path,
        )
        self.duration_timer.stop()
        self.recording_started_at = None
        self.current_audio_path = result.file_path
        self.status_value.setText("Push-Aufnahme gespeichert")
        self.source_value.setText(f"{result.capture_device}\n{result.file_path}")
        self.recording_value.setText(self._format_duration(result.duration_seconds))
        self.progress_detail.setText("Push-Aufnahme gestoppt. Transkription startet ...")
        self.autosave_value.setText(str(result.file_path))
        if not self._transition_push_to_transcribe(
            PushToTranscribeState.TRANSCRIBING,
            "Push: Transkription startet",
        ):
            return False
        self._refresh_actions()
        self._start_transcription(
            result.file_path,
            auto_copy_to_clipboard=True,
            cleanup_audio_path=result.file_path,
        )
        return True

    def import_audio_file(self) -> None:
        if self.audio_service.is_recording:
            logger.warning("Import waehrend laufender Aufnahme blockiert")
            self._show_error("Bitte die laufende Aufnahme zuerst stoppen.")
            QMessageBox.information(
                self,
                "Aufnahme aktiv",
                "Bitte die laufende Aufnahme zuerst stoppen.",
            )
            return

        if self._is_transcribing():
            logger.warning("Import waehrend laufender Transkription blockiert")
            self._show_error("Es läuft bereits eine Transkription.")
            QMessageBox.information(
                self,
                "Bitte warten",
                "Es läuft bereits eine Transkription.",
            )
            return

        if not self._persist_form_settings(show_feedback=False):
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Audiodatei auswählen",
            str(self.settings.output_path()),
            "Audio-Dateien (*.wav *.mp3 *.m4a *.flac *.ogg *.aac);;Alle Dateien (*.*)",
        )
        if not file_path:
            return

        self._clear_error()
        logger.info("Importiere Audiodatei %s", file_path)
        self.current_audio_path = Path(file_path)
        self.current_timed_transcript_text = ""
        self.source_value.setText(file_path)
        self.status_value.setText("Datei importiert")
        self.progress_detail.setText("Importierte Datei wird transkribiert ...")
        self.recording_value.setText("Dateiimport")
        self._refresh_actions()
        self._start_transcription(Path(file_path))

    def cancel_transcription(self) -> None:
        if not self._is_transcribing() or self.transcription_worker is None:
            return

        logger.info("Abbruch für laufende Transkription angefordert")
        self.transcription_cancel_requested = True
        self.cancel_button.setEnabled(False)
        self.status_value.setText("Abbruch angefordert")
        self.progress_bar.setValue(0)
        self.progress_detail.setText("Abbruch angefordert. Aktueller Whisper-Chunk wird beendet ...")
        self.transcription_worker.cancel()

    def _start_transcription(
        self,
        audio_path: Path,
        auto_copy_to_clipboard: bool = False,
        cleanup_audio_path: Path | None = None,
    ) -> None:
        if self._is_transcribing():
            logger.warning("Transkription für %s übersprungen, da bereits ein Job läuft", audio_path)
            if auto_copy_to_clipboard:
                self._set_push_feedback(
                    "Push: ignoriert",
                    "Push-to-Transcribe ignoriert: Es läuft bereits eine Transkription.",
                    is_error=True,
                )
                self._reset_push_to_transcribe_state("Push: blockiert, Transkription läuft")
            return

        logger.info("Plane Transkription für %s", audio_path)
        self._clear_error()
        self.transcription_cancel_requested = False
        self.transcription_auto_clipboard_requested = auto_copy_to_clipboard
        job = self._build_transcription_job(audio_path, cleanup_audio_path)
        self.active_transcription_job = job
        request = TranscriptionRequest(
            audio_path=job.audio_path,
            output_directory=self.settings.output_path(),
            language=self.settings.language,
            model_size=self.settings.model_size,
            performance_profile=self.settings.transcription_profile,
            transcript_format=getattr(self.settings, "transcript_format", TRANSCRIPT_FORMAT_COMPACT),
            show_timestamps=self.settings.show_timestamps,
        )

        self.transcription_thread = QThread(self)
        self.transcription_worker = TranscriptionWorker(self.transcription_service, request)
        self.transcription_worker.moveToThread(self.transcription_thread)

        self.transcription_thread.started.connect(self.transcription_worker.run)
        self.transcription_worker.progress.connect(self._on_transcription_progress)
        self.transcription_worker.finished.connect(self._on_transcription_finished)
        self.transcription_worker.finished.connect(self.transcription_thread.quit)
        self.transcription_worker.cancelled.connect(self._on_transcription_cancelled)
        self.transcription_worker.cancelled.connect(self.transcription_thread.quit)
        self.transcription_worker.failed.connect(self._on_transcription_failed)
        self.transcription_worker.failed.connect(self.transcription_thread.quit)
        self.transcription_thread.finished.connect(self.transcription_worker.deleteLater)
        self.transcription_thread.finished.connect(self.transcription_thread.deleteLater)
        self.transcription_thread.finished.connect(self._clear_transcription_refs)

        self.progress_bar.setValue(1)
        if auto_copy_to_clipboard:
            self._set_push_feedback(
                "Push: Transkription läuft",
                "Push-to-Transcribe wird im Hintergrund transkribiert ...",
            )
        else:
            self.progress_detail.setText("Transkription wird im Hintergrund vorbereitet ...")
        self.status_value.setText("Transkription läuft")
        self.transcription_thread.start()
        self._refresh_actions()

    def _on_transcription_progress(self, value: int, message: str) -> None:
        if self.transcription_cancel_requested and "Abbruch" not in message:
            return
        self.progress_bar.setValue(value)
        self.progress_detail.setText(message)

    def _on_transcription_finished(self, result: TranscriptionResult) -> None:
        logger.info("Transkription fertig: %s", result.transcript_path)
        should_copy_to_clipboard = self.transcription_auto_clipboard_requested
        self._clear_error()
        self.transcription_cancel_requested = False
        self.transcription_auto_clipboard_requested = False
        self.current_transcript_path = result.transcript_path
        self.current_timed_transcript_text = result.timed_transcript_text
        if self.timestamps_checkbox.isChecked():
            self.transcript_edit.setPlainText(result.timed_transcript_text)
        else:
            self.transcript_edit.setPlainText(
                format_transcript_text(
                    result.timed_transcript_text,
                    include_timestamps=False,
                    transcript_format=str(
                        self.transcript_format_combo.currentData() or TRANSCRIPT_FORMAT_COMPACT
                    ),
                )
            )
        self.status_value.setText(f"Fertig ({result.detected_language})")
        self.autosave_value.setText(str(result.transcript_path))
        self.progress_bar.setValue(100)
        self.progress_detail.setText(
            f"Transkription abgeschlossen. Automatisch gespeichert unter {result.transcript_path.name}."
        )
        if should_copy_to_clipboard:
            clipboard_text = format_transcript_text(
                result.timed_transcript_text,
                include_timestamps=False,
                transcript_format=getattr(self.settings, "transcript_format", TRANSCRIPT_FORMAT_COMPACT),
            )
            if self._copy_text_to_clipboard(
                clipboard_text,
                success_message="Push-to-Transcribe fertig: Text ist in der Zwischenablage.",
                log_label="Push-to-Transcribe Transkript",
            ):
                self._reset_push_to_transcribe_state("Push: kopiert")
            else:
                self._reset_push_to_transcribe_state("Fehler: Clipboard")
        self._cleanup_recorded_audio_after_success(result)
        self._refresh_actions()

    def _on_transcription_cancelled(self, message: str) -> None:
        logger.info("Transkription wurde abgebrochen: %s", message)
        was_push_to_transcribe = self.transcription_auto_clipboard_requested
        if self.active_transcription_job and self.active_transcription_job.cleanup_audio_path:
            logger.info(
                "Aufgenommene Audiodatei bleibt nach Abbruch erhalten: %s",
                self.active_transcription_job.cleanup_audio_path,
            )
        self.transcription_cancel_requested = False
        self.transcription_auto_clipboard_requested = False
        self._clear_error()
        self.status_value.setText("Abgebrochen")
        if was_push_to_transcribe:
            self._reset_push_to_transcribe_state("Push: abgebrochen")
        self.progress_detail.setText(message)
        self._refresh_actions()

    def _on_transcription_failed(self, message: str) -> None:
        logger.error("Transkription fehlgeschlagen: %s", message)
        was_push_to_transcribe = self.transcription_auto_clipboard_requested
        if self.active_transcription_job and self.active_transcription_job.cleanup_audio_path:
            logger.info(
                "Aufgenommene Audiodatei bleibt nach Transkriptionsfehler erhalten: %s",
                self.active_transcription_job.cleanup_audio_path,
            )
        self.transcription_cancel_requested = False
        self.transcription_auto_clipboard_requested = False
        self.status_value.setText("Fehler")
        if was_push_to_transcribe:
            self._reset_push_to_transcribe_state("Push: Fehler")
        self.progress_bar.setValue(0)
        self._show_error(message)
        QMessageBox.critical(self, "Transkription fehlgeschlagen", message)
        self._refresh_actions()

    def _clear_transcription_refs(self) -> None:
        self.transcription_thread = None
        self.transcription_worker = None
        self.active_transcription_job = None
        self.transcription_cancel_requested = False
        self._refresh_actions()

    def _native_clipboard_hwnd(self) -> int | None:
        try:
            return int(self.winId())
        except Exception:
            return None

    def _copy_text_to_clipboard(self, text: str, *, success_message: str, log_label: str) -> bool:
        if QThread.currentThread() != self.thread():
            logger.error("%s nicht kopiert: Clipboard-Zugriff außerhalb des UI-Threads blockiert", log_label)
            return False

        if not text.strip():
            message = "Es gibt aktuell keinen Text zum Kopieren."
            logger.warning("%s nicht kopiert: leerer Text", log_label)
            self._show_error(message)
            return False

        if sys.platform == "win32":
            hwnd = self._native_clipboard_hwnd()
            native_write_result = write_native_windows_clipboard_text(text, hwnd)
            native_snapshot = read_native_windows_clipboard_text(hwnd)
            native_text_matches = native_snapshot.text == text
            native_ok = bool(
                native_write_result.ok
                and native_snapshot.ok
                and native_snapshot.has_unicode_text
                and native_text_matches
            )
            if not native_ok:
                message = "Der Text konnte nicht über den nativen Windows-Clipboard-Pfad kopiert werden."
                logger.error(
                    "%s native Clipboard-Kopie fehlgeschlagen: write_ok=%s write_error=%r "
                    "read_ok=%s has_unicode=%s text_matches=%s formats=%s read_error=%r",
                    log_label,
                    native_write_result.ok,
                    native_write_result.error,
                    native_snapshot.ok,
                    native_snapshot.has_unicode_text,
                    native_text_matches,
                    native_snapshot.formats or [],
                    native_snapshot.error,
                )
                self._show_error(message)
                return False

            logger.info(
                "%s in Windows-Zwischenablage kopiert (%s Zeichen)",
                log_label,
                len(text),
            )
        else:
            try:
                clipboard = QApplication.clipboard()
                if clipboard is None:
                    raise RuntimeError("Qt-Clipboard ist nicht verfügbar.")
                mime_data = QMimeData()
                mime_data.setText(text)
                clipboard.setMimeData(mime_data, QClipboard.Clipboard)
            except Exception:
                message = "Der Text konnte nicht in die Zwischenablage kopiert werden."
                logger.exception("%s konnte nicht in die Zwischenablage kopiert werden", log_label)
                self._show_error(message)
                return False

            logger.info(
                "%s in Zwischenablage kopiert (%s Zeichen)",
                log_label,
                len(text),
            )

        self._clear_error()
        self.status_value.setText("Transkript kopiert")
        self.progress_detail.setText(f"{success_message} ({len(text)} Zeichen)")
        return True

    def copy_transcript_selection_to_clipboard(self, selected_text: str) -> None:
        self._copy_text_to_clipboard(
            selected_text,
            success_message="Markierter Transkripttext wurde in die Zwischenablage kopiert.",
            log_label="Transkript-Auswahl",
        )

    def copy_transcript_to_clipboard(self) -> None:
        transcript_text = self.transcript_edit.toPlainText()
        self._copy_text_to_clipboard(
            transcript_text,
            success_message="Transkript wurde in die Zwischenablage kopiert.",
            log_label="Transkript",
        )

    def export_transcript(self) -> None:
        transcript_text = self.transcript_edit.toPlainText().strip()
        if not transcript_text:
            logger.warning("Export ohne vorhandenes Transkript blockiert")
            self._show_error("Es gibt aktuell keinen Text zum Exportieren.")
            QMessageBox.information(
                self,
                "Kein Transkript",
                "Es gibt aktuell keinen Text zum Exportieren.",
            )
            return

        base_name = "transcript.txt"
        if self.current_audio_path is not None:
            base_name = f"{self.current_audio_path.stem}.txt"

        save_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Transkript exportieren",
            str(self.settings.output_path() / base_name),
            EXPORT_FILTER,
        )
        if not save_path:
            return

        target_path = Path(save_path)
        suffix = target_path.suffix.lower()
        if not suffix:
            if "SubRip" in selected_filter:
                suffix = ".srt"
            elif "WebVTT" in selected_filter:
                suffix = ".vtt"
            else:
                suffix = ".txt"
            target_path = target_path.with_suffix(suffix)

        try:
            export_content = build_export_content(
                transcript_text,
                target_path.suffix,
                timed_transcript_text=self.current_timed_transcript_text,
                include_timestamps=self.timestamps_checkbox.isChecked(),
                transcript_format=str(
                    self.transcript_format_combo.currentData() or TRANSCRIPT_FORMAT_COMPACT
                ),
            )
            target_path.write_text(export_content, encoding="utf-8")
        except ValueError as exc:
            logger.warning("Exportformat konnte nicht erzeugt werden: %s", exc)
            self._show_error(str(exc))
            QMessageBox.warning(self, "Export fehlgeschlagen", str(exc))
            return
        except OSError:
            message = (
                "Die Exportdatei konnte nicht geschrieben werden. "
                f"Bitte prüfe Schreibrechte oder ob die Datei geöffnet ist: {target_path}"
            )
            logger.exception("Export nach %s fehlgeschlagen", target_path)
            self._show_error(message)
            QMessageBox.critical(self, "Export fehlgeschlagen", message)
            return

        logger.info("Transkript exportiert nach %s", target_path)
        self._clear_error()
        self.current_transcript_path = target_path
        self.autosave_value.setText(str(target_path))
        self.progress_detail.setText(f"Transkript exportiert als {target_path.suffix}.")
        self._refresh_actions()

    def _update_recording_duration(self) -> None:
        if self.recording_started_at is None:
            self.recording_value.setText("00:00")
            return
        elapsed = (datetime.now() - self.recording_started_at).total_seconds()
        self.recording_value.setText(self._format_duration(elapsed))

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = max(int(seconds), 0)
        minutes, secs = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _handle_start_hotkey(self) -> None:
        if not self.audio_service.is_recording:
            logger.info("Globaler Start-Hotkey ausgelost")
            self.start_recording()

    def _handle_stop_hotkey(self) -> None:
        if self.audio_service.is_recording:
            logger.info("Globaler Stopp-Hotkey ausgelost")
            self.stop_recording()

    def _handle_toggle_hotkey(self) -> None:
        if self.audio_service.is_recording:
            logger.info("Globaler Toggle-Hotkey stoppt Aufnahme")
            self.stop_recording()
        else:
            logger.info("Globaler Toggle-Hotkey startet Aufnahme")
            self.start_recording()

    def _handle_push_to_transcribe_pressed(self) -> None:
        if self.push_to_transcribe_state != PushToTranscribeState.IDLE:
            if self.push_to_transcribe_state == PushToTranscribeState.RECORDING:
                logger.debug("Wiederholtes Push-to-Transcribe KeyDown waehrend Aufnahme ignoriert")
            else:
                logger.warning(
                    "Push-to-Transcribe KeyDown ignoriert: Zustand=%s",
                    self.push_to_transcribe_state.value,
                )
                self._set_push_feedback(
                    "Push: ignoriert",
                    "Push-to-Transcribe ignoriert: Es läuft bereits eine Transkription.",
                )
            return

        logger.info("Push-to-Transcribe Hotkey gedrückt")
        self._set_push_feedback("Push: Aufnahme startet", "Push-to-Transcribe Aufnahme startet ...")
        self._start_push_to_transcribe_recording()

    def _handle_push_to_transcribe_released(self) -> None:
        if self.push_to_transcribe_state != PushToTranscribeState.RECORDING:
            if self.push_to_transcribe_state == PushToTranscribeState.IDLE:
                logger.debug("Push-to-Transcribe KeyUp ohne aktive Aufnahme ignoriert")
            else:
                logger.debug(
                    "Push-to-Transcribe KeyUp waehrend %s ignoriert",
                    self.push_to_transcribe_state.value,
                )
            return

        logger.info("Push-to-Transcribe Hotkey losgelassen")
        self._set_push_feedback("Push: Aufnahme stoppt", "Push-to-Transcribe Aufnahme wird gestoppt ...")
        if not self._stop_push_to_transcribe_recording():
            self._refresh_actions()

    def _is_transcribing(self) -> bool:
        return self.transcription_thread is not None and self.transcription_thread.isRunning()

    def _refresh_actions(self) -> None:
        has_transcript = bool(self.transcript_edit.toPlainText().strip())
        is_recording = self.audio_service.is_recording
        is_transcribing = self._is_transcribing()

        self.start_button.setEnabled(not is_recording and not is_transcribing)
        self.stop_button.setEnabled(is_recording)
        self.import_button.setEnabled(not is_recording and not is_transcribing)
        self.export_button.setEnabled(has_transcript and not is_recording)
        self.copy_transcript_button.setEnabled(has_transcript)
        self.cancel_button.setEnabled(is_transcribing and not self.transcription_cancel_requested)
        self.recording_source_combo.setEnabled(not is_recording and not is_transcribing)
        self.performance_profile_combo.setEnabled(not is_recording and not is_transcribing)
        self.transcript_format_combo.setEnabled(
            not is_recording and not is_transcribing and not self.timestamps_checkbox.isChecked()
        )
        self.cleanup_recordings_checkbox.setEnabled(not is_recording and not is_transcribing)
        self.save_settings_button.setEnabled(not is_recording and not is_transcribing)
        self._apply_recording_source_to_ui()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.audio_service.is_recording:
            QMessageBox.information(
                self,
                "Aufnahme aktiv",
                "Bitte die laufende Aufnahme zuerst stoppen.",
            )
            event.ignore()
            return

        if self._is_transcribing():
            if self.active_transcription_job is not None:
                logger.info(
                    "Schließen blockiert: Transkriptionsjob %s läuft noch für %s",
                    self.active_transcription_job.job_id,
                    self.active_transcription_job.audio_path,
                )
            QMessageBox.information(
                self,
                "Transkription aktiv",
                "Bitte warte, bis die laufende Transkription abgeschlossen ist.",
            )
            event.ignore()
            return

        logger.info("Anwendung wird geschlossen")
        self.hotkey_manager.shutdown()
        self._remove_log_handler()
        super().closeEvent(event)
