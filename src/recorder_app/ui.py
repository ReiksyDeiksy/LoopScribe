from __future__ import annotations

import ctypes
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
import logging
import math
from pathlib import Path
import sys
import time
from ctypes import wintypes
from uuid import uuid4

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QRectF, QVariantAnimation, QEvent, QMimeData, QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QIcon, QColor, QCursor, QPainter, QPen, QClipboard, QCloseEvent, QContextMenuEvent, QKeyEvent, QKeySequence, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
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
    QStackedWidget,
    QLayout,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QMenu,
    QStyle,
    QStyleOptionButton,
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
from .dictation import WindowsDictationInput
from .i18n import UI_LANGUAGES, UiText, normalize_ui_language, translate
from .help_text import HELP_SECTIONS
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
    **{name: code for code, name in UI_LANGUAGES.items()},
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
    kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
    kernel32.GlobalSize.restype = ctypes.c_size_t
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


def snapshot_native_windows_clipboard(hwnd: int | None = None) -> dict[int, bytes] | None:
    """Copy transferable HGLOBAL formats, including text, HTML, DIB and PNG."""
    if sys.platform != "win32":
        return None
    user32, kernel32 = _win32_clipboard_api()
    opened = False
    try:
        _open_windows_clipboard(user32, hwnd)
        opened = True
        data = {}
        format_id = 0
        total_bytes = 0
        while True:
            format_id = user32.EnumClipboardFormats(format_id)
            if not format_id:
                break
            # GDI handles and OLE object descriptors cannot be copied as bytes.
            if format_id in {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}:
                continue
            name = _windows_clipboard_format_name(user32, format_id)
            if name in {"Ole Private Data", "DataObject", "Object Descriptor", "Link Source Descriptor",
                        "Embed Source", "Embedded Object", "Link Source"}:
                continue
            handle = user32.GetClipboardData(format_id)
            size = kernel32.GlobalSize(handle) if handle else 0
            if not size:
                continue
            total_bytes += size
            if total_bytes > 128 * 1024 * 1024:
                return None
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                continue
            try:
                data[format_id] = ctypes.string_at(pointer, size)
            finally:
                kernel32.GlobalUnlock(handle)
        return data if data or not user32.EnumClipboardFormats(0) else None
    except Exception:
        logger.warning("Zwischenablage konnte nicht für die Wiederherstellung gesichert werden", exc_info=True)
        return None
    finally:
        if opened:
            user32.CloseClipboard()


def restore_native_windows_clipboard(data: dict[int, bytes], hwnd: int | None = None) -> bool:
    user32, kernel32 = _win32_clipboard_api()
    handles = {}
    opened = False
    try:
        # Allocate before EmptyClipboard, so allocation failure preserves the clipboard.
        for format_id, payload in data.items():
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
            if not handle:
                raise RuntimeError(_win32_error("GlobalAlloc"))
            handles[format_id] = handle
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise RuntimeError(_win32_error("GlobalLock"))
            try:
                ctypes.memmove(pointer, payload, len(payload))
            finally:
                kernel32.GlobalUnlock(handle)
        _open_windows_clipboard(user32, hwnd)
        opened = True
        if not user32.EmptyClipboard():
            raise RuntimeError(_win32_error("EmptyClipboard"))
        for format_id, handle in list(handles.items()):
            if not user32.SetClipboardData(format_id, handle):
                raise RuntimeError(_win32_error("SetClipboardData"))
            del handles[format_id]  # Windows now owns this handle.
        return True
    except Exception:
        logger.warning("Zwischenablage konnte nicht wiederhergestellt werden", exc_info=True)
        return False
    finally:
        if opened:
            user32.CloseClipboard()
        for handle in handles.values():
            kernel32.GlobalFree(handle)


class PushToTranscribeState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    INSERTING = "inserting"


@dataclass(slots=True)
class TranscriptionJobContext:
    job_id: str
    audio_path: Path
    cleanup_audio_path: Path | None
    delete_recorded_audio_after_transcription: bool


PUSH_TO_TRANSCRIBE_TRANSITIONS = {
    PushToTranscribeState.IDLE: {PushToTranscribeState.RECORDING},
    PushToTranscribeState.RECORDING: {
        PushToTranscribeState.IDLE,
        PushToTranscribeState.TRANSCRIBING,
    },
    PushToTranscribeState.TRANSCRIBING: {PushToTranscribeState.IDLE, PushToTranscribeState.INSERTING},
    PushToTranscribeState.INSERTING: {PushToTranscribeState.IDLE},
}


class AudioStatusIndicator(QWidget):
    """Small status visualization; recording bars always represent measured audio."""

    COLORS = {"ready": "#62d6be", "recording": "#ff777f",
              "transcribing": "#c2a0ff", "finished": "#83e6b0"}
    LABELS = {"ready": "Bereit zur Aufnahme", "recording": "Aufnahme läuft",
              "transcribing": "Wird transkribiert …", "finished": "Transkript fertig"}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(220, 46)
        self._state = "ready"
        self._level = 0.0
        self._color = QColor(self.COLORS["ready"])
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(180)
        self._fade.valueChanged.connect(self._set_color)
        self.setAccessibleName(self.LABELS["ready"])

    def _set_color(self, color) -> None:
        self._color = color
        self.update()

    def set_status(self, state: str, level: float = 0.0) -> None:
        if state != self._state:
            self._state = state
            self._fade.stop()
            self._fade.setStartValue(self._color)
            self._fade.setEndValue(QColor(self.COLORS[state]))
            self._fade.start()
            self.setAccessibleName(self.LABELS[state])
        target = math.sqrt(max(0.0, min(1.0, level))) if state == "recording" else 0.0
        self._level += (target - self._level) * (0.65 if target > self._level else 0.25)
        if state != "ready" or self._level > 0.001:
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        if self._state == "finished":
            painter.setPen(QPen(self._color, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(12, 23, 20, 30)
            painter.drawLine(20, 30, 34, 15)
        else:
            for index, scale in enumerate((0.45, 0.75, 1.0, 0.75, 0.45)):
                if self._state == "transcribing":
                    height = 5 + 15 * (0.5 + 0.5 * math.sin(time.monotonic() * 4 - index * 0.65))
                else:
                    height = 5 + 25 * self._level * scale
                painter.drawRoundedRect(QRectF(5 + index * 8, (46 - height) / 2, 4, height), 2, 2)
        painter.setPen(self._color)
        alignment = Qt.AlignmentFlag.AlignVCenter | (
            Qt.AlignmentFlag.AlignRight if self.layoutDirection() == Qt.LayoutDirection.RightToLeft
            else Qt.AlignmentFlag.AlignLeft
        )
        painter.drawText(QRectF(55, 0, self.width() - 55, 46), alignment, self.LABELS[self._state])


class DictationBar(QFrame):
    """A small, click-through status window that leaves the text field focused."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setObjectName("dictationBar")
        self.setStyleSheet("""
            QFrame#dictationBar { background: #171d23; border: 1px solid #46515c; border-radius: 12px; }
            QLabel { color: #a7b3c0; font-family: 'Segoe UI'; font-size: 12px; }
        """)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 2, 16, 2)
        row.setSpacing(16)
        self.indicator = AudioStatusIndicator(self)
        self.indicator.setFixedWidth(250)
        self.shortcut = QLabel()
        self.shortcut.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.indicator)
        row.addWidget(self.shortcut)
        self.adjustSize()

    def show_near_cursor_screen(self) -> None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.adjustSize()
            self.move(area.center().x() - self.width() // 2, area.bottom() - self.height() - 18)
        self.show()


class FadingSourceButton(QPushButton):
    """Paint state colors without delaying selection or rebuilding stylesheets."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self._keyboard_focus = False
        self._colors = tuple(QColor(c) for c in ("#1a1f25", "#46515c", "#e7edf2"))
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(140)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._blend_colors)
        self._from_colors = self._colors
        self._to_colors = self._colors
        self.toggled.connect(self._transition_colors)
        self.pressed.connect(self._transition_colors)
        self.released.connect(self._transition_colors)

    def focusInEvent(self, event) -> None:
        self._keyboard_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        super().focusInEvent(event)
        self.update()

    def mousePressEvent(self, event) -> None:
        self._keyboard_focus = False
        super().mousePressEvent(event)
        self.update()

    def _transition_colors(self, *_args) -> None:
        if not self.isEnabled():
            values = ("#171c21", "#303941", "#74808b")
        elif self.isChecked():
            values = ("#24403f", "#62d6be", "#78e0ce")
        elif self.isDown():
            values = ("#35454c", "#82919e", "#e7edf2")
        elif self.underMouse():
            values = ("#29343c", "#82919e", "#e7edf2")
        else:
            values = ("#1a1f25", "#46515c", "#e7edf2")
        target = tuple(QColor(c) for c in values)
        if target == self._to_colors:
            return
        self._fade.stop()
        self._from_colors = self._colors
        self._to_colors = target
        if not self.isVisible():
            self._colors = target
            self.update()
            return
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _blend_colors(self, value) -> None:
        self._colors = tuple(
            QColor.fromRgbF(*(a + (b - a) * float(value)
                              for a, b in zip(start.getRgbF(), end.getRgbF())))
            for start, end in zip(self._from_colors, self._to_colors)
        )
        self.update()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._transition_colors()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._transition_colors()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange and hasattr(self, "_fade"):
            self._transition_colors()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background, border, text = self._colors
        painter.setBrush(background)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        painter.setPen(text)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
        if self.hasFocus() and self._keyboard_focus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#b9c6d2"), 1))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(4, 4, -4, -4), 4, 4)


class ToggleCheckBox(QCheckBox):
    """Animate the thumb while preserving native checkbox input and semantics."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._thumb_position = float(self.isChecked())
        self._slide = QVariantAnimation(self)
        self._slide.setDuration(160)
        self._slide.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._slide.valueChanged.connect(self._update_thumb)
        self.toggled.connect(self._animate_toggle)

    def _update_thumb(self, value) -> None:
        self._thumb_position = float(value)
        self.update()

    def _animate_toggle(self, checked: bool) -> None:
        self._slide.stop()
        target = float(checked)
        if not self.isVisible():
            self._update_thumb(target)
            return
        self._slide.setStartValue(self._thumb_position)
        self._slide.setEndValue(target)
        self._slide.start()

    def _thumb_rect(self) -> QRectF:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        position = self._thumb_position
        if self.layoutDirection() == Qt.LayoutDirection.RightToLeft:
            position = 1.0 - position
        return QRectF(indicator.left() + 3 + 14 * position,
                      indicator.top() + (indicator.height() - 14) / 2, 14, 14)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._slide.state() != QAbstractAnimation.State.Running:
            # Settings may be synchronized with checkbox signals temporarily blocked.
            self._thumb_position = float(self.isChecked())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f0fffb" if self.isEnabled() else "#89969f"))
        painter.drawEllipse(self._thumb_rect())


class SmoothWheelScroll(QObject):
    """Animate wheel notches; leave native touchpad pixel scrolling intact."""

    def __init__(self, area: QAbstractScrollArea) -> None:
        super().__init__(area)
        self.area = area
        self.animations = {}
        for bar in (area.verticalScrollBar(), area.horizontalScrollBar()):
            animation = QVariantAnimation(self)
            animation.setDuration(160)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            animation.valueChanged.connect(lambda value, b=bar: b.setValue(round(value)))
            bar.sliderPressed.connect(animation.stop)
            bar.actionTriggered.connect(lambda _action, a=animation: a.stop())
            bar.rangeChanged.connect(lambda _low, _high, a=animation: a.stop())
            bar.installEventFilter(self)
            self.animations[bar] = animation
        area.installEventFilter(self)
        area.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.Hide):
            for animation in self.animations.values():
                animation.stop()
        if event.type() != QEvent.Type.Wheel:
            return False
        if not event.pixelDelta().isNull() or event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            for animation in self.animations.values():
                animation.stop()
            return False
        delta = event.angleDelta()
        horizontal = bool(delta.x()) or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        bar = self.area.horizontalScrollBar() if horizontal else self.area.verticalScrollBar()
        if watched in self.animations:
            bar = watched
        amount = delta.x() if delta.x() else delta.y()
        if not amount or not bar.isEnabled():
            return False
        animation = self.animations[bar]
        current = bar.value()
        target = float(animation.endValue()) if animation.state() == QAbstractAnimation.State.Running else current
        distance = -amount / 120 * QApplication.wheelScrollLines() * bar.singleStep()
        # A reversal responds from the visible position instead of finishing queued motion.
        if (target - current) * distance < 0:
            target = current
        target = max(bar.minimum(), min(bar.maximum(), target + distance))
        animation.stop()
        if target != current:
            animation.setStartValue(float(current))
            animation.setEndValue(float(target))
            animation.start()
        event.accept()
        return True


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

    @staticmethod
    def translate_ui(source: str) -> str:
        return source

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
        copy_action = menu.addAction(self.translate_ui("Kopieren"))
        copy_action.setEnabled(self.textCursor().hasSelection())
        copy_action.triggered.connect(self.copy)
        menu.addSeparator()
        select_all_action = menu.addAction(self.translate_ui("Alles auswählen"))
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
        self.ui_language = normalize_ui_language(self.settings.ui_language)
        self._ui_text_bindings = {}

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
        self.dictation_input = WindowsDictationInput()
        self._dictation_target = None
        self._pending_dictation_text: str | None = None
        self._dictation_insert_deadline = 0.0
        self._dictation_feedback = "Push: bereit"
        self._dictation_feedback_until = 0.0
        self._clipboard_restore_data = None
        self._clipboard_restore_sequence = 0
        self.dictation_insert_timer = QTimer(self)
        self.dictation_insert_timer.setInterval(50)
        self.dictation_insert_timer.timeout.connect(self._try_insert_dictation)
        self.clipboard_restore_timer = QTimer(self)
        self.clipboard_restore_timer.setSingleShot(True)
        self.clipboard_restore_timer.timeout.connect(self._restore_dictation_clipboard)
        self.dictation_bar = DictationBar()
        self.available_audio_devices: list[LoopbackDeviceInfo] = []
        self.available_microphone_devices: list[MicrophoneDeviceInfo] = []
        self.available_app_audio_sessions: list[ApplicationAudioSessionInfo] = []
        self.app_audio_capability = ApplicationAudioCapability(
            supported=False,
            reason="App-Audio wurde noch nicht geprüft.",
        )

        self._build_ui()
        self._apply_style()
        for area in self.findChildren(QAbstractScrollArea):
            if isinstance(area, (QScrollArea, QPlainTextEdit, QTextBrowser)):
                SmoothWheelScroll(area)
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
        self._apply_ui_language()
        self.dictation_bar.show_near_cursor_screen()

    def _tr(self, source: str, **values: object) -> str:
        return translate(self.ui_language, source, **values)

    def _set_ui_text(self, widget, source: str, setter: str = "setText", **values: object) -> None:
        """Remember UI templates so language changes never rebuild the workspace."""
        self._ui_text_bindings[(widget, setter)] = (source, values)
        getattr(widget, setter)(self._tr(source, **values))

    def _text_widget(self, widget_type, source: str):
        widget = widget_type("")
        self._set_ui_text(widget, source)
        return widget

    def _apply_ui_language(self, *_args) -> None:
        self.ui_language = normalize_ui_language(str(self.ui_language_combo.currentData()))
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft if self.ui_language == "ar"
                                else Qt.LayoutDirection.LeftToRight)
        self.dictation_bar.setLayoutDirection(self.layoutDirection())
        self.dictation_bar.shortcut.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        # Paths and keyboard sequences keep their natural, unambiguous direction.
        for widget in (self.storage_input, self.start_hotkey_edit, self.stop_hotkey_edit,
                       self.push_to_transcribe_hotkey_edit, self.recording_value):
            widget.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        for (widget, setter), (source, values) in self._ui_text_bindings.items():
            getattr(widget, setter)(self._tr(source, **values))
        for combo, options in (
            (self.language_combo, LANGUAGE_OPTIONS),
            (self.recording_source_combo, RECORDING_SOURCE_OPTIONS),
            (self.performance_profile_combo, TRANSCRIPTION_PROFILE_OPTIONS),
            (self.transcript_format_combo, TRANSCRIPT_FORMAT_OPTIONS),
            (self.audio_device_combo, {"Standard-Wiedergabegerät": ""}),
            (self.microphone_device_combo, {"Standard-Mikrofon": ""}),
            (self.app_audio_session_combo, {"Keine App-Audio-Session ausgewählt": ""}),
        ):
            blocked = combo.blockSignals(True)
            try:
                for source, code in options.items():
                    index = combo.findData(code)
                    if index >= 0:
                        combo.setItemText(index, self._tr(source))
            finally:
                combo.blockSignals(blocked)
        self.audio_status.LABELS = {
            state: self._tr(source) for state, source in AudioStatusIndicator.LABELS.items()
        }
        self.audio_status.setAccessibleName(self.audio_status.LABELS[self.audio_status._state])
        self.audio_status.setFixedWidth(max(
            220, 65 + max(self.audio_status.fontMetrics().horizontalAdvance(text)
                          for text in self.audio_status.LABELS.values())
        ))
        self.audio_status.update()
        self._update_settings_summary()
        self._refresh_actions()

    def _build_ui(self) -> None:
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "scribe.ico")))
        self.resize(1440, 960)
        self.setMinimumSize(1000, 720)
        root = QWidget()
        root.setObjectName("appRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 12, 28, 16)
        layout.setSpacing(18)
        navigation = QHBoxLayout()
        brand_icon = QLabel()
        brand_icon.setPixmap(self.windowIcon().pixmap(32, 32))
        self._set_ui_text(brand_icon, 'scribe: Füllerfeder mit Audiospur', setter='setAccessibleName')
        navigation.addWidget(brand_icon)
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        navigation.addWidget(brand)
        navigation.addSpacing(24)
        self.workspace_button = self._text_widget(QPushButton, 'Arbeitsbereich')
        self.settings_button = self._text_widget(QPushButton, 'Einstellungen')
        self.help_button = self._text_widget(QPushButton, 'Hilfe')
        for button in (self.workspace_button, self.settings_button):
            button.setCheckable(True)
            button.setObjectName("navigationButton")
            navigation.addWidget(button)
        self.workspace_button.setChecked(True)
        navigation.addWidget(self.help_button)
        navigation.addStretch()
        self.audio_status = AudioStatusIndicator()
        navigation.addWidget(self.audio_status)
        layout.addLayout(navigation)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        workspace = QWidget()
        work = QVBoxLayout(workspace)
        work.setContentsMargins(0, 0, 0, 0)
        work.setSpacing(16)
        work.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        title = self._text_widget(QLabel, 'Neue Aufnahme')
        title.setObjectName("pageTitle")
        work.addWidget(title)
        self.source_description = self._text_widget(QLabel, 'Audio aufnehmen und lokal transkribieren')
        self.source_description.setObjectName("mutedLabel")
        work.addWidget(self.source_description)
        settings_panel = QScrollArea()
        settings_panel.setWidgetResizable(True)
        settings_panel.setFrameShape(QFrame.Shape.NoFrame)
        settings_content = QWidget()
        settings_form = QVBoxLayout(settings_content)
        settings_form.setContentsMargins(24, 12, 24, 24)
        settings_form.setSpacing(12)
        settings_title = self._text_widget(QLabel, 'Einstellungen')
        settings_title.setObjectName("pageTitle")
        settings_form.addWidget(settings_title)

        def add_setting(label_text: str, widget: QWidget) -> None:
            settings_form.addWidget(self._text_widget(QLabel, label_text))
            settings_form.addWidget(widget)

        def wrap_inline_layout(inline_layout: QHBoxLayout) -> QWidget:
            wrapper = QWidget()
            inline_layout.setContentsMargins(0, 0, 0, 0)
            wrapper.setLayout(inline_layout)
            return wrapper

        storage_row = QHBoxLayout()
        storage_row.setSpacing(8)
        self.storage_input = QLineEdit()
        self.storage_input.setClearButtonEnabled(True)
        self.storage_button = self._text_widget(QPushButton, 'Ordner wählen')
        self.storage_button.setObjectName("secondaryButton")
        self.storage_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        storage_row.addWidget(self.storage_input, 1)
        storage_row.addWidget(self.storage_button)

        self.language_combo = WheelGuardComboBox()
        for label, code in LANGUAGE_OPTIONS.items():
            self.language_combo.addItem(label, code)

        self.ui_language_combo = WheelGuardComboBox()
        for code, label in UI_LANGUAGES.items():
            self.ui_language_combo.addItem(label, code)
        self._set_combo_data(self.ui_language_combo, self.ui_language)

        self.model_combo = WheelGuardComboBox()
        for model in MODEL_OPTIONS:
            self.model_combo.addItem(model, model)

        self.performance_profile_combo = WheelGuardComboBox()
        for label, profile_key in TRANSCRIPTION_PROFILE_OPTIONS.items():
            self.performance_profile_combo.addItem(label, profile_key)
        self._set_ui_text(self.performance_profile_combo, 'Schnell reduziert CPU-Last. Ausgewogen und Qualität nutzen mehr Suchbreite und können länger dauern.', setter='setToolTip')

        self.timestamps_checkbox = self._text_widget(ToggleCheckBox, 'Timestamps anzeigen')
        self.timestamps_checkbox.setChecked(True)
        self.transcript_format_combo = WheelGuardComboBox()
        for label, format_key in TRANSCRIPT_FORMAT_OPTIONS.items():
            self.transcript_format_combo.addItem(label, format_key)
        self._set_ui_text(self.transcript_format_combo, 'Kompakt verbindet Whisper-Segmente ohne AI-Umschreibung zu natürlicheren Absätzen.', setter='setToolTip')
        self.cleanup_recordings_checkbox = self._text_widget(ToggleCheckBox, 'Aufnahmen nach Transkription löschen')
        self.cleanup_recordings_checkbox.setChecked(True)
        self._set_ui_text(self.cleanup_recordings_checkbox, 'Löscht app-eigene WAV-Aufnahmen auch bei Abbruch oder Fehlern der Transkription. Importierte Dateien bleiben erhalten.', setter='setToolTip')

        self.recording_source_combo = WheelGuardComboBox()
        for label, value in RECORDING_SOURCE_OPTIONS.items():
            self.recording_source_combo.addItem(label, value)

        audio_device_row = QHBoxLayout()
        audio_device_row.setSpacing(8)
        self.audio_device_combo = WheelGuardComboBox()
        self.refresh_devices_button = self._text_widget(QPushButton, 'Loopback aktualisieren')
        self.refresh_devices_button.setObjectName("secondaryButton")
        self.refresh_devices_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        audio_device_row.addWidget(self.audio_device_combo, 1)
        audio_device_row.addWidget(self.refresh_devices_button)

        microphone_device_row = QHBoxLayout()
        microphone_device_row.setSpacing(8)
        self.microphone_device_combo = WheelGuardComboBox()
        self.refresh_microphones_button = self._text_widget(QPushButton, 'Mikrofone aktualisieren')
        self.refresh_microphones_button.setObjectName("secondaryButton")
        self.refresh_microphones_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        microphone_device_row.addWidget(self.microphone_device_combo, 1)
        microphone_device_row.addWidget(self.refresh_microphones_button)

        app_audio_row = QHBoxLayout()
        app_audio_row.setSpacing(8)
        self.app_audio_session_combo = WheelGuardComboBox()
        self.refresh_app_audio_button = self._text_widget(QPushButton, 'Apps aktualisieren')
        self.refresh_app_audio_button.setObjectName("secondaryButton")
        self.refresh_app_audio_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        app_audio_row.addWidget(self.app_audio_session_combo, 1)
        app_audio_row.addWidget(self.refresh_app_audio_button)
        self.app_audio_hint = self._text_widget(QLabel, 'App-Audio wird nur angezeigt, wenn echte Windows-Audio-Sessions erkannt werden.')
        self.app_audio_hint.setWordWrap(True)
        self.app_audio_hint.setObjectName("mutedLabel")

        self.start_hotkey_edit = QKeySequenceEdit()
        self.stop_hotkey_edit = QKeySequenceEdit()
        self.push_to_transcribe_hotkey_edit = QKeySequenceEdit()
        hotkey_hint = self._text_widget(QLabel, 'Tipp: Wird derselbe Start/Stopp-Hotkey gesetzt, arbeitet die App als Toggle. Push-to-Transcribe startet beim Drücken und stoppt beim Loslassen.')
        hotkey_hint.setWordWrap(True)
        hotkey_hint.setObjectName("mutedLabel")


        self.recording_source_combo.setParent(root)
        self.recording_source_combo.hide()
        self.app_audio_microphone_checkbox = self._text_widget(ToggleCheckBox, 'Mikrofon mit aufnehmen')
        self.system_microphone_checkbox = self._text_widget(ToggleCheckBox, 'Mikrofon mit aufnehmen')
        self.system_microphone_checkbox.toggled.connect(self._toggle_system_microphone)
        recording_card = QFrame()
        recording_card.setObjectName("panelCard")
        recording_card.setMinimumHeight(300)
        recording_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        recording = QVBoxLayout(recording_card)
        recording.setContentsMargins(22, 20, 22, 20)
        recording.setSpacing(16)
        source_row = QHBoxLayout()
        self.source_buttons = {}
        for label, source in (("Systemaudio", RECORDING_SOURCE_SYSTEM_AUDIO),
                              ("Mikrofon", RECORDING_SOURCE_MICROPHONE),
                              ("App-Audio", RECORDING_SOURCE_APPLICATION_AUDIO)):
            button = self._text_widget(FadingSourceButton, label)
            button.setCheckable(True)
            button.setMinimumHeight(48)
            button.setObjectName("sourceButton")
            button.clicked.connect(lambda checked=False, value=source: self._select_source(value))
            self.source_buttons[source] = button
            source_row.addWidget(button)
        recording.addLayout(source_row)
        devices = QHBoxLayout()
        devices.setSpacing(28)
        self.output_fields = QWidget()
        output_fields = QVBoxLayout(self.output_fields)
        output_fields.setContentsMargins(0, 0, 0, 0)
        self.app_fields = QWidget()
        app_fields = QVBoxLayout(self.app_fields)
        app_fields.setContentsMargins(0, 0, 0, 0)
        app_fields.addWidget(self._text_widget(QLabel, 'App'))
        app_fields.addWidget(wrap_inline_layout(app_audio_row))
        self.system_fields = QWidget()
        system_fields = QVBoxLayout(self.system_fields)
        system_fields.setContentsMargins(0, 0, 0, 0)
        system_fields.addWidget(self._text_widget(QLabel, 'Wiedergabegerät'))
        system_fields.addWidget(wrap_inline_layout(audio_device_row))
        output_fields.addWidget(self.app_fields)
        output_fields.addWidget(self.system_fields)
        output_fields.addStretch()
        devices.addWidget(self.output_fields, 1)
        microphone_fields = QVBoxLayout()
        microphone_fields.addWidget(self.app_audio_microphone_checkbox)
        microphone_fields.addWidget(self.system_microphone_checkbox)
        microphone_fields.addWidget(self._text_widget(QLabel, 'Mikrofon'))
        microphone_fields.addWidget(wrap_inline_layout(microphone_device_row))
        self.mix_hint = self._text_widget(QLabel, 'Beide Quellen landen in einer Audiodatei.')
        self.mix_hint.setObjectName("mutedLabel")
        microphone_fields.addWidget(self.mix_hint)
        devices.addLayout(microphone_fields, 1)
        recording.addLayout(devices)
        recording.addWidget(self.app_audio_hint)
        actions = QHBoxLayout()
        self.start_button = self._text_widget(QPushButton, '●  Aufnahme starten')
        self.start_button.setObjectName("primaryButton")
        self.stop_button = self._text_widget(QPushButton, '■  Aufnahme stoppen')
        self.stop_button.setObjectName("dangerButton")
        self.import_button = self._text_widget(QPushButton, 'Datei importieren')
        for button in (self.start_button, self.stop_button, self.import_button):
            button.setMinimumHeight(46)
            actions.addWidget(button)
        actions.addStretch()
        self.status_value = self._text_widget(QLabel, 'Bereit')
        self.recording_value = QLabel("00:00")
        self.recording_value.setObjectName("timer")
        actions.addWidget(self.status_value)
        actions.addSpacing(20)
        actions.addWidget(self.recording_value)
        recording.addLayout(actions)
        work.addWidget(recording_card)
        self.progress_detail = self._text_widget(QLabel, 'Noch keine Transkription gestartet.')
        self.progress_detail.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.cancel_button = self._text_widget(QPushButton, 'Transkription abbrechen')
        self.progress_panel = QWidget()
        progress = QHBoxLayout(self.progress_panel)
        progress.setContentsMargins(0, 0, 0, 0)
        progress.addWidget(self.progress_bar, 1)
        progress.addWidget(self.cancel_button)
        work.addWidget(self.progress_panel)
        self.error_label = QLabel()
        self.error_label.setObjectName("errorBanner")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        work.addWidget(self.error_label)
        transcript = QFrame()
        transcript.setObjectName("panelCard")
        transcript_layout = QVBoxLayout(transcript)
        transcript_layout.setContentsMargins(20, 14, 20, 16)
        toolbar = QHBoxLayout()
        heading = self._text_widget(QLabel, 'Transkript')
        heading.setObjectName("sectionTitle")
        toolbar.addWidget(heading)
        toolbar.addStretch()
        self._set_ui_text(self.timestamps_checkbox, 'Zeitstempel')
        toolbar.addWidget(self.timestamps_checkbox)
        self.copy_transcript_button = self._text_widget(QPushButton, 'Kopieren')
        self.export_button = self._text_widget(QPushButton, 'Exportieren')
        toolbar.addWidget(self.copy_transcript_button)
        toolbar.addWidget(self.export_button)
        transcript_layout.addLayout(toolbar)
        self.transcript_edit = IsolatedWheelPlainTextEdit()
        self.transcript_edit.translate_ui = self._tr
        self.transcript_edit.setObjectName("transcriptEditor")
        self._set_ui_text(self.transcript_edit, 'Hier erscheint dein Transkript nach der Aufnahme.\nDu kannst den Text anschließend bearbeiten, kopieren oder exportieren.', setter='setPlaceholderText')
        self.transcript_edit.setMinimumHeight(180)
        transcript_layout.addWidget(self.transcript_edit, 1)
        work.addWidget(transcript, 1)
        work.addWidget(self.progress_detail)
        add_setting("Speicherpfad", wrap_inline_layout(storage_row))
        add_setting("Oberflächensprache", self.ui_language_combo)
        language_hint = self._text_widget(QLabel, "Oberflächensprache wird sofort angezeigt. Zum Beibehalten Einstellungen speichern.")
        language_hint.setWordWrap(True)
        language_hint.setObjectName("mutedLabel")
        settings_form.addWidget(language_hint)
        add_setting("Erkennungssprache (Audio)", self.language_combo)
        add_setting("Whisper-Modell", self.model_combo)
        add_setting("Performance", self.performance_profile_combo)
        add_setting("Textformat", self.transcript_format_combo)
        add_setting("Aufnahme-Dateien", self.cleanup_recordings_checkbox)
        add_setting("Start-Hotkey", self.start_hotkey_edit)
        add_setting("Stopp-Hotkey", self.stop_hotkey_edit)
        add_setting("Push-to-Transcribe", self.push_to_transcribe_hotkey_edit)
        settings_form.addWidget(hotkey_hint)
        self.save_settings_button = self._text_widget(QPushButton, 'Einstellungen speichern')
        self.save_settings_button.setObjectName("primaryButton")
        settings_form.addWidget(self.save_settings_button)
        settings_form.addStretch()
        settings_panel.setWidget(settings_content)
        workspace_scroll = QScrollArea()
        workspace_scroll.setWidgetResizable(True)
        workspace_scroll.setFrameShape(QFrame.Shape.NoFrame)
        workspace_scroll.setWidget(workspace)
        self.pages.addWidget(workspace_scroll)
        self.pages.addWidget(settings_panel)
        self.workspace_button.clicked.connect(lambda: self._show_page(0))
        self.settings_button.clicked.connect(lambda: self._show_page(1))
        footer = QHBoxLayout()
        footer.addWidget(self._text_widget(QLabel, 'Lokal transkribieren'))
        self.settings_summary = QLabel()
        self.settings_summary.setObjectName("mutedLabel")
        footer.addWidget(self.settings_summary)
        footer.addStretch()
        self.diagnostics_button = self._text_widget(QPushButton, 'Diagnose')
        self.diagnostics_button.setCheckable(True)
        footer.addWidget(self.diagnostics_button)
        layout.addLayout(footer)
        self.diagnostics_panel = QWidget()
        diagnostics = QVBoxLayout(self.diagnostics_panel)
        self.source_value = QLabel()
        self.source_value.setWordWrap(True)
        self.autosave_value = QLabel()
        self.autosave_value.setWordWrap(True)
        self.push_to_transcribe_value = self._text_widget(QLabel, 'Bereit')
        for widget in (self.source_value, self.autosave_value, self.push_to_transcribe_value):
            diagnostics.addWidget(widget)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(500)
        self.log_view.setMaximumHeight(100)
        diagnostics.addWidget(self.log_view)
        self.diagnostics_panel.hide()
        self.diagnostics_button.toggled.connect(self.diagnostics_panel.setVisible)
        layout.addWidget(self.diagnostics_panel)
        for combo in (self.language_combo, self.model_combo):
            combo.currentTextChanged.connect(self._update_settings_summary)
        self.setCentralWidget(root)
        self.ui_language_combo.currentIndexChanged.connect(self._apply_ui_language)
        self.duration_timer = QTimer(self)
        self.duration_timer.setInterval(1_000)
        self._status_finished_until = 0.0
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(50)
        self.status_timer.timeout.connect(self._update_audio_status)
        self.status_timer.start()

    def _update_audio_status(self) -> None:
        if self.audio_service.is_recording:
            self.audio_status.set_status("recording", float(self.audio_service.audio_level))
        elif self._is_transcribing():
            self.audio_status.set_status("transcribing")
        elif time.monotonic() < self._status_finished_until:
            self.audio_status.set_status("finished")
        else:
            self.audio_status.set_status("ready")
        state = self.push_to_transcribe_state
        bar_state = ("recording" if state == PushToTranscribeState.RECORDING else
                     "transcribing" if state != PushToTranscribeState.IDLE else "ready")
        idle_text = (self._dictation_feedback if time.monotonic() < self._dictation_feedback_until
                     else "Zum Diktieren halten")
        self.dictation_bar.indicator.LABELS = {
            "ready": self._tr(idle_text), "recording": self._tr("Sprich jetzt …"),
            "transcribing": self._tr("Wird transkribiert …"), "finished": self._tr("Transkript fertig"),
        }
        self.dictation_bar.shortcut.setText(
            keyboard_to_qt_hotkey(self.settings.push_to_transcribe_hotkey)
        )
        self.dictation_bar.indicator.set_status(
            bar_state, float(self.audio_service.audio_level) if bar_state == "recording" else 0.0
        )
        self.dictation_bar.indicator.update()

    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.workspace_button.setChecked(index == 0)
        self.settings_button.setChecked(index == 1)

    def _update_settings_summary(self, *_args) -> None:
        self._set_ui_text(self.settings_summary,
                          "Sprache: {language}    ·    Modell: {model}",
                          language=self.language_combo.currentText(), model=self.model_combo.currentText())

    def _select_source(self, source: str) -> None:
        if source == RECORDING_SOURCE_SYSTEM_AUDIO and self.system_microphone_checkbox.isChecked():
            source = RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE
        self._set_combo_data(self.recording_source_combo, source)

    def _toggle_system_microphone(self, checked: bool) -> None:
        self._set_combo_data(self.recording_source_combo,
                             RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE if checked
                             else RECORDING_SOURCE_SYSTEM_AUDIO)

    def _apply_style(self) -> None:
        assets = (Path(__file__).parent / "assets").as_posix()
        self.setStyleSheet("""
            QWidget { background: transparent; color: #e7edf2; font-family: 'Segoe UI'; font-size: 14px; }
            QMainWindow, QWidget#appRoot, QScrollArea > QWidget > QWidget { background: #111418; }
            QLabel { background: transparent; }
            QLabel#brand { font-size: 20px; font-weight: 600; }
            QLabel#pageTitle { font-size: 28px; font-weight: 600; }
            QLabel#sectionTitle { font-size: 22px; font-weight: 600; }
            QLabel#timer { font-size: 22px; color: #b9c6d2; }
            QLabel#mutedLabel { color: #a7b3c0; }
            QFrame#panelCard { background: #1a1f25; border: 1px solid #343e47; border-radius: 8px; }

            QPushButton { border: 1px solid #46515c; border-radius: 7px; padding: 9px 16px; background: #1a1f25; }
            QPushButton:hover { background: #29343c; border-color: #82919e; }
            QPushButton:pressed { background: #35454c; }
            QPushButton:disabled { color: #74808b; border-color: #303941; background: #171c21; }
            QPushButton#primaryButton { background: #62d6be; color: #071b19; border: none; font-weight: 600; padding: 12px 28px; }
            QPushButton#primaryButton:hover { background: #83e6d1; }
            QPushButton#dangerButton { background: #a23d49; color: white; border: none; }
            QPushButton#sourceButton:checked { background: #24403f; border: 1px solid #62d6be; color: #78e0ce; }
            QPushButton#navigationButton { border: none; border-radius: 0; background: transparent; }
            QPushButton#navigationButton:checked { color: #78e0ce; border-bottom: 2px solid #62d6be; }
            QComboBox, QLineEdit, QKeySequenceEdit { background: #171d23; border: 1px solid #46515c; border-radius: 6px; padding: 9px; min-height: 20px; selection-background-color: #285f59; }
            QComboBox { padding-right: 36px; }
            QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 34px; border: none; background: transparent; }
            QComboBox::down-arrow { image: url("__ASSETS__/chevron-down.svg"); width: 16px; height: 16px; }
            QComboBox::down-arrow:disabled { image: url("__ASSETS__/chevron-down-disabled.svg"); }
            QComboBox:hover { border-color: #63727e; }
            QComboBox:focus { border-color: #62d6be; }
            QComboBox:disabled { color: #74808b; border-color: #303941; }
            QComboBox QAbstractItemView { background: #202830; color: #e7edf2; selection-background-color: #285f59; }
            QCheckBox { spacing: 10px; padding: 4px 0; }
            QCheckBox::indicator { width: 32px; height: 18px; border-radius: 9px; background: #424e58; border: 1px solid #63727e; }
            QCheckBox::indicator:checked { background: #62d6be; border: 1px solid #62d6be; }
            QCheckBox:disabled { color: #74808b; }
            QPlainTextEdit { background: #1a1f25; border: 1px solid #343e47; border-radius: 6px; padding: 12px; selection-background-color: #285f59; }
            QPlainTextEdit#transcriptEditor { border: none; font-size: 16px; padding: 14px 0; }
            QProgressBar { border: 1px solid #343e47; border-radius: 5px; text-align: center; min-height: 16px; }
            QProgressBar::chunk { background: #347f74; }
            QLabel#errorBanner { color: #ffb4bb; background: #442830; padding: 10px; border-radius: 6px; }
            QScrollArea { border: none; }
            QScrollBar { background: #151a20; border: none; border-radius: 5px; }
            QScrollBar:vertical { width: 10px; margin: 2px 0; }
            QScrollBar:horizontal { height: 10px; margin: 0 2px; }
            QScrollBar::handle { background: #46515c; border-radius: 5px; }
            QScrollBar::handle:vertical { min-height: 32px; }
            QScrollBar::handle:horizontal { min-width: 32px; }
            QScrollBar::handle:hover { background: #63727e; }
            QScrollBar::handle:pressed { background: #62d6be; }
            QScrollBar::handle:disabled { background: #303941; }
            QScrollBar::add-page, QScrollBar::sub-page { background: transparent; border: none; }
            QScrollBar::add-line, QScrollBar::sub-line { background: transparent; border: none; width: 0; height: 0; }
            QScrollBar::up-arrow, QScrollBar::down-arrow,
            QScrollBar::left-arrow, QScrollBar::right-arrow { image: none; width: 0; height: 0; }
            QAbstractScrollArea::corner { background: #151a20; border: none; }
            QToolTip { background: #26313a; color: #e7edf2; border: 1px solid #63727e; padding: 5px; }
        """.replace("__ASSETS__", assets))

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
        self._set_ui_text(self.error_label, message)
        self.error_label.show()
        self._set_ui_text(self.progress_detail, message)

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
        self._set_ui_text(self.push_to_transcribe_value, status_text)
        self._dictation_feedback = status_text
        self._dictation_feedback_until = time.monotonic() + 5.0
        if not detail_text:
            return
        if is_error:
            self._show_error(detail_text)
        else:
            self._set_ui_text(self.progress_detail, detail_text)

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
        delete_after_transcription = (
            normalised_cleanup_path is not None
            and self.settings.delete_recorded_audio_after_transcription
        )
        job = TranscriptionJobContext(
            job_id=uuid4().hex,
            audio_path=normalised_audio_path,
            cleanup_audio_path=normalised_cleanup_path,
            delete_recorded_audio_after_transcription=delete_after_transcription,
        )
        logger.info(
            "Transkriptionsjob erstellt: job=%s audio=%s cleanup=%s delete_after_transcription=%s",
            job.job_id,
            job.audio_path,
            job.cleanup_audio_path or "<kein Cleanup>",
            job.delete_recorded_audio_after_transcription,
        )
        return job

    def _cleanup_recorded_audio_after_transcription(self, result: TranscriptionResult | None = None) -> None:
        job = self.active_transcription_job
        if job is None:
            logger.warning("Kein aktiver Transkriptionsjob für Cleanup vorhanden; Audiodatei bleibt erhalten.")
            return

        result_audio_path = (
            self._normalise_cleanup_path(result.audio_path) if result is not None else job.audio_path
        )
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
        if not job.delete_recorded_audio_after_transcription:
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
            logger.exception("Aufgenommene Audiodatei konnte nach Ende der Transkription nicht gelöscht werden: %s", cleanup_path)
        else:
            logger.info("Aufgenommene Audiodatei nach Ende der Transkription gelöscht: %s", cleanup_path)

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
        self.app_audio_microphone_checkbox.toggled.connect(self._apply_recording_source_to_ui)
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
        dialog.setWindowTitle(self._tr("Hilfe - {app}", app=APP_NAME))
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

        title = QLabel(self._tr("{app} Hilfe", app=APP_NAME))
        title.setObjectName("helpTitle")
        subtitle = QLabel(self._tr("Bedienung, Aufnahmequellen, Hotkeys, Transkription und Export auf einen Blick."))
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
        SmoothWheelScroll(help_view)
        help_view.setOpenExternalLinks(False)
        help_view.document().setDocumentMargin(24)
        help_view.document().setDefaultStyleSheet(
            """
            body {
                color: #e7edf2;
                font-family: "Segoe UI", "Noto Sans", sans-serif;
                font-size: 14px;
                line-height: 1.45;
            }
            h1 {
                color: #e7edf2;
                font-size: 26px;
                font-weight: 600;
                margin: 0 0 16px 0;
            }
            h2 {
                color: #78e0ce;
                font-size: 18px;
                font-weight: 600;
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
            close_button.setText(self._tr("Schließen"))
            close_button.setObjectName("helpCloseButton")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.setStyleSheet(
            """
            QDialog#helpDialog {
                background: #111418;
            }
            QFrame#helpHeader {
                background: #1a1f25;
                border: 1px solid #343e47;
                border-radius: 8px;
            }
            QLabel#helpTitle {
                color: #e7edf2;
                font-size: 26px;
                font-weight: 600;
            }
            QLabel#helpSubtitle {
                color: #a7b3c0;
                font-size: 14px;
                font-weight: 600;
            }
            QFrame#helpCard {
                background: #1a1f25;
                border: 1px solid #343e47;
                border-radius: 8px;
            }
            QTextBrowser#helpText {
                background: #1a1f25;
                color: #e7edf2;
                border: none;
                border-radius: 8px;
                padding: 4px;
                selection-background-color: #285f59;
                selection-color: #e7edf2;
            }
            QDialogButtonBox#helpButtons {
                background: transparent;
            }
            QPushButton#helpCloseButton {
                background: #62d6be;
                color: #071b19;
                border: none;
                border-radius: 7px;
                padding: 9px 18px;
                min-height: 28px;
                font-weight: 600;
            }
            QPushButton#helpCloseButton:hover {
                background: #83e6d1;
            }
            """
        )
        dialog.exec()

    def _build_help_markdown(self) -> str:
        if self.ui_language != "de":
            hotkeys = {
                "start": keyboard_to_qt_hotkey(self.settings.start_hotkey) or "—",
                "stop": keyboard_to_qt_hotkey(self.settings.stop_hotkey) or "—",
                "push": keyboard_to_qt_hotkey(self.settings.push_to_transcribe_hotkey) or "—",
            }
            sections = []
            for heading, paragraph in HELP_SECTIONS:
                values = hotkeys if "{start}" in paragraph else {}
                sections.append(f"## {self._tr(heading)}\n\n{self._tr(paragraph, **values)}")
            sections.append(self._tr(
                "Schnell reduziert CPU-Last. Ausgewogen und Qualität nutzen mehr Suchbreite und können länger dauern."
            ))
            return "\n\n".join(sections)
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
- Bei App-Audio kannst du "Mikrofon mit aufnehmen" aktivieren. App und Mikrofon werden in einer gemeinsamen WAV-Datei aufgenommen und zusammen transkribiert.
- Die passenden Listen können mit "Loopback aktualisieren", "Mikrofone aktualisieren" und "Apps aktualisieren" neu geladen werden.

## Aufnahme und Transkription
- "Aufnahme starten" beginnt eine WAV-Aufnahme mit der aktuell gewählten Quelle und dem gewählten Gerät.
- "Aufnahme stoppen" beendet die Aufnahme und startet danach automatisch die Transkription im Hintergrund.
- Aufgenommene WAV-Dateien werden im Unterordner "recordings" des Speicherpfads abgelegt.
- Wenn automatisches Löschen aktiv ist, werden app-eigene WAV-Aufnahmen nach Ende der Transkription entfernt, auch bei Abbruch oder Fehlern. Importierte Dateien werden nie automatisch gelöscht.
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
- Zum Diktieren Push-to-Transcribe halten, ins Mikrofon sprechen und loslassen. Der Text wird im ursprünglichen, weiterhin aktiven Textfeld eingefügt.
- Die kleine Diktierleiste zeigt Aufnahme und Verarbeitung, auch bei minimiertem Hauptfenster. Push-to-Transcribe verwendet immer das Mikrofon.
- Bei Fensterwechsel oder Einfügefehler bleibt der Text für Strg+V in der Zwischenablage. Nach automatischem Einfügen wird der vorherige Inhalt nach etwa drei Sekunden wiederhergestellt, sofern du nichts anderes kopiert hast.
- Globale Hotkeys können von Windows oder anderen Programmen blockiert werden; in diesem Fall zeigt die App einen Hotkey-Fehler an.

## Fortschritt, Status und Abbruch
- Der Bereich "Status" zeigt Aufnahme, Transkription, Fertig- und Fehlerzustand.
- "Push-to-Transcribe" zeigt den Zustand: bereit, Aufnahme, Transkription oder Einfügen.
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

Oberflächensprache und Audio-Erkennungssprache sind unabhängig. Die Oberfläche wechselt sofort; zum Beibehalten bitte die Einstellungen speichern.

Speicherpfad, Sprache, Modell und Hotkeys findest du oben unter "Einstellungen". Laufzeitmeldungen kannst du unten unter "Diagnose" einblenden.
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
        self.audio_device_combo.addItem(self._tr('Standard-Wiedergabegerät'), '')

        try:
            self.available_audio_devices = self.audio_service.list_loopback_devices()
        except AudioCaptureError as exc:
            logger.exception("Loopback-Geräteliste konnte nicht geladen werden")
            self.available_audio_devices = []
            self._show_error(str(exc))
            if show_feedback:
                QMessageBox.warning(self, self._tr('Audiogeräte'), self._tr(str(exc)))
            return

        for device in self.available_audio_devices:
            self.audio_device_combo.addItem(device.display_name, device.id)

        self._set_combo_data(self.audio_device_combo, previous_selection)
        if self.audio_device_combo.currentIndex() < 0:
            self.audio_device_combo.setCurrentIndex(0)

        self._clear_error()
        logger.info("Loopback-Geräteliste aktualisiert")
        if show_feedback:
            self._set_ui_text(self.progress_detail, 'Loopback-Geräteliste aktualisiert.')

    def reload_microphone_devices(self, show_feedback: bool = True) -> None:
        previous_selection = str(
            self.microphone_device_combo.currentData() or self.settings.microphone_device_id
        )
        self.microphone_device_combo.clear()
        self.microphone_device_combo.addItem(self._tr('Standard-Mikrofon'), '')

        try:
            self.available_microphone_devices = self.audio_service.list_microphone_devices()
        except AudioCaptureError as exc:
            logger.exception("Mikrofonliste konnte nicht geladen werden")
            self.available_microphone_devices = []
            self._show_error(str(exc))
            if show_feedback:
                QMessageBox.warning(self, self._tr('Mikrofone'), self._tr(str(exc)))
            return

        for device in self.available_microphone_devices:
            self.microphone_device_combo.addItem(device.display_name, device.id)

        self._set_combo_data(self.microphone_device_combo, previous_selection)
        if self.microphone_device_combo.currentIndex() < 0:
            self.microphone_device_combo.setCurrentIndex(0)

        self._clear_error()
        logger.info("Mikrofonliste aktualisiert")
        if show_feedback:
            self._set_ui_text(self.progress_detail, 'Mikrofonliste aktualisiert.')

    def reload_application_audio_sessions(
        self,
        show_feedback: bool = True,
        force_refresh: bool = False,
    ) -> None:
        previous_selection = str(
            self.app_audio_session_combo.currentData() or self.settings.application_audio_session_id
        )
        self.app_audio_session_combo.clear()
        self.app_audio_session_combo.addItem(self._tr('Keine App-Audio-Session ausgewählt'), '')

        self.app_audio_capability = self.audio_service.application_audio_capability(
            force_refresh=force_refresh
        )
        if not self.app_audio_capability.supported:
            self.available_app_audio_sessions = []
            self._set_ui_text(self.app_audio_hint, 'App-Audio ist nicht verfügbar: {value0}', value0=self.app_audio_capability.reason)
            self._set_recording_source_item_enabled(RECORDING_SOURCE_APPLICATION_AUDIO, False)
            if self._current_recording_source() == RECORDING_SOURCE_APPLICATION_AUDIO:
                self._set_combo_data(self.recording_source_combo, RECORDING_SOURCE_SYSTEM_AUDIO)
            logger.warning("App-Audio deaktiviert: %s", self.app_audio_capability.reason)
            if show_feedback:
                self._set_ui_text(self.progress_detail, 'App-Audio ist auf diesem System nicht verfügbar.')
            return

        self._set_recording_source_item_enabled(RECORDING_SOURCE_APPLICATION_AUDIO, True)
        try:
            self.available_app_audio_sessions = self.audio_service.list_application_audio_sessions(
                force_refresh=force_refresh
            )
        except AudioCaptureError as exc:
            logger.exception("App-Audio-Sessions konnten nicht geladen werden")
            self.available_app_audio_sessions = []
            self._set_ui_text(self.app_audio_hint, 'App-Audio-Sessions konnten nicht gelesen werden: {value0}', value0=exc)
            self._show_error(str(exc))
            if show_feedback:
                QMessageBox.warning(self, self._tr('App-Audio'), self._tr(str(exc)))
            return

        for session in self.available_app_audio_sessions:
            self.app_audio_session_combo.addItem(session.display_label, session.id)

        self._set_combo_data(self.app_audio_session_combo, previous_selection)
        if self.app_audio_session_combo.currentIndex() < 0:
            self.app_audio_session_combo.setCurrentIndex(0)

        if self.available_app_audio_sessions:
            self._set_ui_text(self.app_audio_hint, 'App-Audio nutzt Windows Process Loopback und nimmt nur die gewählte Audio-App auf.')
        else:
            self._set_ui_text(self.app_audio_hint, 'Keine laufende App-Audio-Session erkannt. Starte Audio in der Ziel-App und aktualisiere die Liste.')
        logger.info("App-Audio-Sessions aktualisiert")
        if show_feedback:
            self._set_ui_text(self.progress_detail, 'App-Audio-Sessions aktualisiert.')

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
        uses_microphone = uses_microphone or (uses_app_audio and self.app_audio_microphone_checkbox.isChecked())
        can_change_devices = not self.audio_service.is_recording and not self._is_transcribing()
        self.app_audio_microphone_checkbox.setEnabled(uses_app_audio and can_change_devices)
        self.audio_device_combo.setEnabled(uses_systemaudio and can_change_devices)
        self.refresh_devices_button.setEnabled(uses_systemaudio and can_change_devices)
        self.microphone_device_combo.setEnabled(uses_microphone and can_change_devices)
        self.refresh_microphones_button.setEnabled(
            uses_microphone and can_change_devices
        )
        app_audio_available = self.app_audio_capability.supported
        self.app_audio_session_combo.setEnabled(uses_app_audio and app_audio_available and can_change_devices)
        self.refresh_app_audio_button.setEnabled(can_change_devices)
        self.app_audio_hint.setVisible(uses_app_audio and not app_audio_available)
        self.app_fields.setVisible(uses_app_audio)
        self.system_fields.setVisible(uses_systemaudio)
        self.output_fields.setVisible(uses_app_audio or uses_systemaudio)
        self.app_audio_microphone_checkbox.setVisible(uses_app_audio)
        self.system_microphone_checkbox.setVisible(uses_systemaudio)
        self.system_microphone_checkbox.blockSignals(True)
        self.system_microphone_checkbox.setChecked(current_source == RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE)
        self.system_microphone_checkbox.blockSignals(False)
        self.system_microphone_checkbox.setEnabled(can_change_devices)
        self.mix_hint.setVisible(uses_microphone and current_source != RECORDING_SOURCE_MICROPHONE)
        selected_button_source = RECORDING_SOURCE_SYSTEM_AUDIO if uses_systemaudio else current_source
        for source, button in self.source_buttons.items():
            button.setChecked(source == selected_button_source)
            button.setEnabled(can_change_devices and (source != RECORDING_SOURCE_APPLICATION_AUDIO or app_audio_available))
        self._set_ui_text(self.source_description, 'App-Audio und Mikrofon gemeinsam aufnehmen' if uses_app_audio and uses_microphone else 'Audio aufnehmen und lokal transkribieren')

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
        needs_microphone = needs_microphone or (needs_app_audio and self.settings.application_audio_include_microphone)
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
        self._set_combo_data(self.ui_language_combo, self.settings.ui_language)
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
        self.app_audio_microphone_checkbox.setChecked(self.settings.application_audio_include_microphone)
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
                QMessageBox.warning(self, self._tr('Hotkey-Fehler'), self._tr(str(exc)))
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
            QMessageBox.warning(self, self._tr('Ungültige Einstellungen'), self._tr(str(exc)))
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
            self._set_ui_text(self.progress_detail, 'Einstellungen gespeichert und Hotkeys aktualisiert.')
        return True

    def _collect_settings_from_form(self) -> AppSettings:
        output_directory = self.storage_input.text().strip()
        if not output_directory:
            raise ValueError(self._tr('Bitte einen Speicherpfad auswählen.'))

        start_hotkey = normalize_hotkey(self._sequence_to_text(self.start_hotkey_edit))
        stop_hotkey = normalize_hotkey(self._sequence_to_text(self.stop_hotkey_edit))
        push_to_transcribe_hotkey = normalize_hotkey(
            self._sequence_to_text(self.push_to_transcribe_hotkey_edit)
        )
        if not start_hotkey and not stop_hotkey:
            raise ValueError(self._tr('Mindestens ein globaler Hotkey muss gesetzt sein.'))
        if not push_to_transcribe_hotkey:
            raise ValueError(self._tr('Bitte einen Push-to-Transcribe-Hotkey setzen.'))

        if not start_hotkey:
            start_hotkey = stop_hotkey
        if not stop_hotkey:
            stop_hotkey = start_hotkey
        if push_to_transcribe_hotkey in {start_hotkey, stop_hotkey}:
            raise ValueError(self._tr('Der Push-to-Transcribe-Hotkey muss sich von Start- und Stopp-Hotkey unterscheiden.'))

        selected_recording_source = str(self.recording_source_combo.currentData() or RECORDING_SOURCE_SYSTEM_AUDIO)
        selected_app_audio_session_id = str(self.app_audio_session_combo.currentData() or "")
        selected_app_audio_session_label = self.app_audio_session_combo.currentText() or ""
        if selected_recording_source == RECORDING_SOURCE_APPLICATION_AUDIO:
            if not self.app_audio_capability.supported:
                raise ValueError(self._tr('App-Audio ist nicht verfügbar: {value0}', value0=self.app_audio_capability.reason))
            if not selected_app_audio_session_id:
                raise ValueError(self._tr('Bitte eine App-Audio-Session auswählen oder die App-Liste aktualisieren.'))

        settings = AppSettings(
            output_directory=output_directory,
            start_hotkey=start_hotkey,
            stop_hotkey=stop_hotkey,
            push_to_transcribe_hotkey=push_to_transcribe_hotkey,
            language=str(self.language_combo.currentData()),
            ui_language=str(self.ui_language_combo.currentData()),
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
            application_audio_include_microphone=self.app_audio_microphone_checkbox.isChecked(),
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
        return next((label for label, code in RECORDING_SOURCE_OPTIONS.items()
                     if code == self._current_recording_source()), "Systemaudio")

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
        return self.audio_device_combo.currentText() or self._tr("Standard-Wiedergabegerät")

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
        selected = QFileDialog.getExistingDirectory(self, self._tr('Speicherpfad auswählen'), self.storage_input.text() or str(self.settings.output_path()))
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
            QMessageBox.information(self, self._tr('Bitte warten'), self._tr('Während einer laufenden Transkription kann keine neue Aufnahme gestartet werden.'))
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
            QMessageBox.critical(self, self._tr('Aufnahme fehlgeschlagen'), self._tr(str(exc)))
            return

        self._clear_error()
        logger.info("Aufnahme gestartet: %s", output_path)
        self.current_audio_path = output_path
        self.current_transcript_path = None
        self.current_timed_transcript_text = ""
        self.transcript_edit.clear()
        self.progress_bar.setValue(0)
        self._set_ui_text(self.progress_detail, '{value0} wird aufgenommen ...', value0=UiText(selected_source_label))
        self._set_ui_text(self.status_value, 'Aufnahme läuft ({value0})', value0=UiText(selected_source_label))
        self._set_ui_text(self.source_value, '{value0}\n{value1}\n{value2}', value0=UiText(selected_source_label), value1=selected_device_label, value2=output_path)
        self._set_ui_text(self.autosave_value, 'Die WAV-Datei wird bereits geschrieben.')
        self.recording_started_at = datetime.now()
        self._set_ui_text(self.recording_value, '00:00')
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
            QMessageBox.warning(self, self._tr('Aufnahme stoppen'), self._tr(str(exc)))
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
        self._set_ui_text(self.status_value, 'Aufnahme gespeichert')
        self._set_ui_text(self.source_value, '{value0}\n{value1}', value0=result.capture_device, value1=result.file_path)
        self._set_ui_text(self.recording_value, self._format_duration(result.duration_seconds))
        self._set_ui_text(self.progress_detail, 'Aufnahme abgeschlossen ({value0}). Automatische Transkription startet ...', value0=result.capture_device)
        self._set_ui_text(self.autosave_value, str(result.file_path))
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
        self.dictation_insert_timer.stop()
        self._pending_dictation_text = None
        self._dictation_target = None
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

        source_label = "Mikrofon"
        dictation_settings = replace(self.settings, recording_source=RECORDING_SOURCE_MICROPHONE)
        self._dictation_target = self.dictation_input.capture_target()
        try:
            output_path = self.audio_service.start_recording(dictation_settings)
        except AudioCaptureError as exc:
            self._dictation_target = None
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
        self._set_ui_text(self.progress_detail, 'Push-to-Transcribe nimmt {value0} auf ...', value0=UiText(source_label))
        self._set_ui_text(self.status_value, 'Push-Aufnahme läuft ({value0})', value0=UiText(source_label))
        self._set_ui_text(self.source_value, 'Push-to-Transcribe: {value0}\n{value1}', value0=UiText(source_label), value1=output_path)
        self._set_ui_text(self.autosave_value, 'Die WAV-Datei wird bereits geschrieben.')
        self.recording_started_at = datetime.now()
        self._set_ui_text(self.recording_value, '00:00')
        self.duration_timer.start()
        self._transition_push_to_transcribe(
            PushToTranscribeState.RECORDING,
            "Push: Aufnahme läuft",
        )
        self.dictation_bar.show_near_cursor_screen()
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
        self._set_ui_text(self.status_value, 'Push-Aufnahme gespeichert')
        self._set_ui_text(self.source_value, '{value0}\n{value1}', value0=result.capture_device, value1=result.file_path)
        self._set_ui_text(self.recording_value, self._format_duration(result.duration_seconds))
        self._set_ui_text(self.progress_detail, 'Push-Aufnahme gestoppt. Transkription startet ...')
        self._set_ui_text(self.autosave_value, str(result.file_path))
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
            QMessageBox.information(self, self._tr('Aufnahme aktiv'), self._tr('Bitte die laufende Aufnahme zuerst stoppen.'))
            return

        if self._is_transcribing():
            logger.warning("Import waehrend laufender Transkription blockiert")
            self._show_error("Es läuft bereits eine Transkription.")
            QMessageBox.information(self, self._tr('Bitte warten'), self._tr('Es läuft bereits eine Transkription.'))
            return

        if not self._persist_form_settings(show_feedback=False):
            return

        file_path, _ = QFileDialog.getOpenFileName(self, self._tr('Audiodatei auswählen'), str(self.settings.output_path()), self._tr('Audiodateien (*.wav *.mp3 *.m4a *.flac *.ogg *.aac);;Alle Dateien (*.*)'))
        if not file_path:
            return

        self._clear_error()
        logger.info("Importiere Audiodatei %s", file_path)
        self.current_audio_path = Path(file_path)
        self.current_timed_transcript_text = ""
        self._set_ui_text(self.source_value, file_path)
        self._set_ui_text(self.status_value, 'Datei importiert')
        self._set_ui_text(self.progress_detail, 'Importierte Datei wird transkribiert ...')
        self._set_ui_text(self.recording_value, 'Dateiimport')
        self._refresh_actions()
        self._start_transcription(Path(file_path))

    def cancel_transcription(self) -> None:
        if not self._is_transcribing() or self.transcription_worker is None:
            return

        logger.info("Abbruch für laufende Transkription angefordert")
        self.transcription_cancel_requested = True
        self.cancel_button.setEnabled(False)
        self._set_ui_text(self.status_value, 'Abbruch angefordert')
        self.progress_bar.setValue(0)
        self._set_ui_text(self.progress_detail, 'Abbruch angefordert. Aktueller Whisper-Chunk wird beendet ...')
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
            self._set_ui_text(self.progress_detail, 'Transkription wird im Hintergrund vorbereitet ...')
        self._set_ui_text(self.status_value, 'Transkription läuft')
        self.transcription_thread.start()
        self._refresh_actions()

    def _on_transcription_progress(self, value: int, message: str) -> None:
        if self.transcription_cancel_requested and "Abbruch" not in message:
            return
        self.progress_bar.setValue(value)
        self._set_ui_text(self.progress_detail, message)

    def _on_transcription_finished(self, result: TranscriptionResult) -> None:
        self._status_finished_until = time.monotonic() + 3.0
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
        self._set_ui_text(self.status_value, 'Fertig ({value0})', value0=result.detected_language)
        self._set_ui_text(self.autosave_value, str(result.transcript_path))
        self.progress_bar.setValue(100)
        self._set_ui_text(self.progress_detail, 'Transkription abgeschlossen. Automatisch gespeichert unter {value0}.', value0=result.transcript_path.name)
        if should_copy_to_clipboard:
            clipboard_text = format_transcript_text(
                result.timed_transcript_text,
                include_timestamps=False,
                transcript_format=getattr(self.settings, "transcript_format", TRANSCRIPT_FORMAT_COMPACT),
            )
            if clipboard_text.strip():
                self._pending_dictation_text = clipboard_text
                self._dictation_insert_deadline = time.monotonic() + 3.0
                self._transition_push_to_transcribe(PushToTranscribeState.INSERTING, "Push: Text einfügen")
                self.dictation_insert_timer.start()
                self._try_insert_dictation()
            else:
                self._reset_push_to_transcribe_state("Keine Sprache erkannt")
        self._cleanup_recorded_audio_after_transcription(result)
        self._refresh_actions()

    def _try_insert_dictation(self) -> None:
        text = self._pending_dictation_text
        if text is None:
            return
        target_is_current = self.dictation_input.target_is_current(self._dictation_target)
        modifiers_down = self.dictation_input.modifiers_are_down()
        if target_is_current and modifiers_down and time.monotonic() < self._dictation_insert_deadline:
            return
        self.dictation_insert_timer.stop()
        self._restore_dictation_clipboard()
        previous = snapshot_native_windows_clipboard(self._native_clipboard_hwnd())
        if not self._copy_text_to_clipboard(
            text, success_message="Push-to-Transcribe fertig: Text ist in der Zwischenablage.",
            log_label="Push-to-Transcribe Transkript",
        ):
            self._reset_push_to_transcribe_state("Fehler: Clipboard")
            self._refresh_actions()
            return
        if target_is_current and not modifiers_down and self.dictation_input.paste(self._dictation_target):
            self._clipboard_restore_data = previous
            self._clipboard_restore_sequence = self.dictation_input.clipboard_sequence()
            # Allow slow editors to consume the paste before restoring copied data.
            self.clipboard_restore_timer.start(3000)
            self._reset_push_to_transcribe_state("Push: Text eingefügt")
        else:
            self._reset_push_to_transcribe_state("Push: bitte Strg+V")
            self._set_ui_text(self.progress_detail, 'Automatisches Einfügen nicht möglich. Der Text ist mit Strg+V verfügbar.')
        self._refresh_actions()

    def _restore_dictation_clipboard(self) -> None:
        self.clipboard_restore_timer.stop()
        previous = self._clipboard_restore_data
        self._clipboard_restore_data = None
        if previous is not None and self.dictation_input.clipboard_sequence() == self._clipboard_restore_sequence:
            restore_native_windows_clipboard(previous, self._native_clipboard_hwnd())

    def _on_transcription_cancelled(self, message: str) -> None:
        logger.info("Transkription wurde abgebrochen: %s", message)
        was_push_to_transcribe = self.transcription_auto_clipboard_requested
        self._cleanup_recorded_audio_after_transcription()
        self.transcription_cancel_requested = False
        self.transcription_auto_clipboard_requested = False
        self._clear_error()
        self._set_ui_text(self.status_value, 'Abgebrochen')
        if was_push_to_transcribe:
            self._reset_push_to_transcribe_state("Push: abgebrochen")
        self._set_ui_text(self.progress_detail, message)
        self._refresh_actions()

    def _on_transcription_failed(self, message: str) -> None:
        logger.error("Transkription fehlgeschlagen: %s", message)
        was_push_to_transcribe = self.transcription_auto_clipboard_requested
        self._cleanup_recorded_audio_after_transcription()
        self.transcription_cancel_requested = False
        self.transcription_auto_clipboard_requested = False
        self._set_ui_text(self.status_value, 'Fehler')
        if was_push_to_transcribe:
            self._reset_push_to_transcribe_state("Push: Fehler")
        self.progress_bar.setValue(0)
        self._show_error(message)
        if not was_push_to_transcribe:
            QMessageBox.critical(self, self._tr('Transkription fehlgeschlagen'), self._tr(message))
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
        self._set_ui_text(self.status_value, 'Transkript kopiert')
        self._set_ui_text(self.progress_detail, '{value0} ({value1} Zeichen)', value0=UiText(success_message), value1=len(text))
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
            QMessageBox.information(self, self._tr('Kein Transkript'), self._tr('Es gibt aktuell keinen Text zum Exportieren.'))
            return

        base_name = "transcript.txt"
        if self.current_audio_path is not None:
            base_name = f"{self.current_audio_path.stem}.txt"

        save_path, selected_filter = QFileDialog.getSaveFileName(self, self._tr('Transkript exportieren'), str(self.settings.output_path() / base_name), self._tr(EXPORT_FILTER))
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
            QMessageBox.warning(self, self._tr('Export fehlgeschlagen'), self._tr(str(exc)))
            return
        except OSError:
            message = (
                "Die Exportdatei konnte nicht geschrieben werden. "
                f"Bitte prüfe Schreibrechte oder ob die Datei geöffnet ist: {target_path}"
            )
            logger.exception("Export nach %s fehlgeschlagen", target_path)
            self._show_error(message)
            QMessageBox.critical(self, self._tr('Export fehlgeschlagen'), self._tr(message))
            return

        logger.info("Transkript exportiert nach %s", target_path)
        self._clear_error()
        self.current_transcript_path = target_path
        self._set_ui_text(self.autosave_value, str(target_path))
        self._set_ui_text(self.progress_detail, 'Transkript exportiert als {value0}.', value0=target_path.suffix)
        self._refresh_actions()

    def _update_recording_duration(self) -> None:
        if self.recording_started_at is None:
            self._set_ui_text(self.recording_value, '00:00')
            return
        elapsed = (datetime.now() - self.recording_started_at).total_seconds()
        self._set_ui_text(self.recording_value, self._format_duration(elapsed))

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
        return self._pending_dictation_text is not None or (
            self.transcription_thread is not None and self.transcription_thread.isRunning()
        )

    def _refresh_actions(self) -> None:
        has_transcript = bool(self.transcript_edit.toPlainText().strip())
        is_recording = self.audio_service.is_recording
        is_transcribing = self._is_transcribing()

        self._update_audio_status()
        self.start_button.setVisible(not is_recording)
        self.stop_button.setVisible(is_recording)
        self.progress_panel.setVisible(is_transcribing)
        self._update_settings_summary()
        self._set_ui_text(self.start_button, 'Aufnahme starten ({value0})', value0=self.settings.start_hotkey, setter='setToolTip')
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
            QMessageBox.information(self, self._tr('Aufnahme aktiv'), self._tr('Bitte die laufende Aufnahme zuerst stoppen.'))
            event.ignore()
            return

        if self._is_transcribing():
            if self.active_transcription_job is not None:
                logger.info(
                    "Schließen blockiert: Transkriptionsjob %s läuft noch für %s",
                    self.active_transcription_job.job_id,
                    self.active_transcription_job.audio_path,
                )
            QMessageBox.information(self, self._tr('Transkription aktiv'), self._tr('Bitte warte, bis die laufende Transkription abgeschlossen ist.'))
            event.ignore()
            return

        logger.info("Anwendung wird geschlossen")
        self._restore_dictation_clipboard()
        self.dictation_bar.close()
        self.hotkey_manager.shutdown()
        self._remove_log_handler()
        super().closeEvent(event)
