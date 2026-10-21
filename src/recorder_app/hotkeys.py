from __future__ import annotations

import logging
import threading

import keyboard
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

QT_TO_KEYBOARD = {
    "Ctrl": "ctrl",
    "Alt": "alt",
    "Shift": "shift",
    "Meta": "windows",
    "Return": "enter",
    "Enter": "enter",
    "Esc": "esc",
    "Del": "delete",
    "Ins": "insert",
    "PgUp": "page up",
    "PgDown": "page down",
    "Space": "space",
    "Tab": "tab",
    "Backspace": "backspace",
}

KEYBOARD_TO_QT = {
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "windows": "Meta",
    "enter": "Return",
    "esc": "Esc",
    "delete": "Del",
    "insert": "Ins",
    "page up": "PgUp",
    "page down": "PgDown",
    "space": "Space",
    "tab": "Tab",
    "backspace": "Backspace",
}

KEY_ALIASES = {
    "left ctrl": "ctrl",
    "right ctrl": "ctrl",
    "left alt": "alt",
    "right alt": "alt",
    "alt gr": "alt",
    "left shift": "shift",
    "right shift": "shift",
    "left windows": "windows",
    "right windows": "windows",
    "win": "windows",
}


class HotkeyRegistrationError(RuntimeError):
    """Raised when global hotkeys cannot be registered."""


def normalize_hotkey(value: str) -> str:
    parts = [part.strip() for part in value.split("+") if part.strip()]
    normalized = []
    for part in parts:
        token = QT_TO_KEYBOARD.get(part, QT_TO_KEYBOARD.get(part.title(), part))
        normalized.append(token.lower())
    return "+".join(normalized)


def keyboard_to_qt_hotkey(value: str) -> str:
    parts = [part.strip() for part in value.split("+") if part.strip()]
    tokens = [
        KEYBOARD_TO_QT.get(part.lower(), part.upper() if len(part) == 1 else part.title())
        for part in parts
    ]
    return "+".join(tokens)


def canonical_key_name(value: str) -> str:
    try:
        normalized = keyboard.normalize_name(value).lower()
    except Exception:
        normalized = value.strip().lower()
    return KEY_ALIASES.get(normalized, normalized)


def hotkey_tokens(value: str) -> frozenset[str]:
    return frozenset(
        canonical_key_name(part)
        for part in normalize_hotkey(value).split("+")
        if part.strip()
    )


class GlobalHotkeyManager(QObject):
    start_requested = Signal()
    stop_requested = Signal()
    toggle_requested = Signal()
    push_pressed = Signal()
    push_released = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._handles: list[int] = []
        self._push_hook = None
        self._push_tokens: frozenset[str] = frozenset()
        self._push_scan_codes: tuple[frozenset[int], ...] = ()
        self._pressed_scan_codes: set[int] = set()
        self._push_is_down = False
        self._lock = threading.Lock()

    def update_hotkeys(self, start_hotkey: str, stop_hotkey: str, push_hotkey: str = "") -> None:
        start_hotkey = normalize_hotkey(start_hotkey)
        stop_hotkey = normalize_hotkey(stop_hotkey)
        push_hotkey = normalize_hotkey(push_hotkey)

        if push_hotkey and push_hotkey in {start_hotkey, stop_hotkey}:
            raise HotkeyRegistrationError(
                "Der Push-to-Transcribe-Hotkey darf nicht mit Start- oder Stopp-Hotkey identisch sein."
            )

        self.unregister_all()

        try:
            if start_hotkey and stop_hotkey and start_hotkey == stop_hotkey:
                self._handles.append(keyboard.add_hotkey(start_hotkey, self.toggle_requested.emit))
            else:
                if start_hotkey:
                    self._handles.append(keyboard.add_hotkey(start_hotkey, self.start_requested.emit))
                if stop_hotkey:
                    self._handles.append(keyboard.add_hotkey(stop_hotkey, self.stop_requested.emit))

            if push_hotkey:
                steps = keyboard.parse_hotkey(push_hotkey)
                if len(steps) != 1:
                    raise ValueError("Push-to-Transcribe benötigt eine einzelne Tastenkombination.")
                push_tokens = hotkey_tokens(push_hotkey)
                if push_tokens:
                    with self._lock:
                        self._push_tokens = push_tokens
                        self._push_scan_codes = tuple(frozenset(codes) for codes in steps[0])
                        self._pressed_scan_codes.clear()
                        self._push_is_down = False
                    self._push_hook = keyboard.hook(self._handle_push_event)
                    logger.info("Push-to-Transcribe Hotkey eventbasiert registriert: %s", push_hotkey)
        except Exception as exc:
            self.unregister_all()
            raise HotkeyRegistrationError(
                f"Die globalen Hotkeys konnten nicht registriert werden: {exc}"
            ) from exc

    def _handle_push_event(self, event) -> None:
        scan_code = getattr(event, "scan_code", None)
        event_type = getattr(event, "event_type", "")
        emit_pressed = False
        emit_released = False
        with self._lock:
            if not any(scan_code in codes for codes in self._push_scan_codes):
                return

            if event_type == "down":
                self._pressed_scan_codes.add(scan_code)
                if not self._push_is_down and self._push_chord_is_down():
                    self._push_is_down = True
                    emit_pressed = True
            elif event_type == "up":
                self._pressed_scan_codes.discard(scan_code)
                if self._push_is_down and not self._push_chord_is_down():
                    self._push_is_down = False
                    emit_released = True
                elif not self._push_is_down:
                    logger.debug("Push-to-Transcribe KeyUp ohne aktiven Druck ignoriert")

        if emit_pressed:
            logger.info("Push-to-Transcribe KeyDown erkannt")
            self.push_pressed.emit()
        if emit_released:
            logger.info("Push-to-Transcribe KeyUp erkannt")
            self.push_released.emit()

    def reset_push_state(self) -> None:
        with self._lock:
            # Keep physical modifier state when only Space was released.
            self._push_is_down = self._push_chord_is_down()
        logger.debug("Push-to-Transcribe Hotkey-State defensiv zurückgesetzt")

    def _push_chord_is_down(self) -> bool:
        return bool(self._push_scan_codes) and all(
            codes.intersection(self._pressed_scan_codes) for codes in self._push_scan_codes
        )

    def unregister_all(self) -> None:
        if self._push_hook is not None:
            try:
                keyboard.unhook(self._push_hook)
            except Exception as exc:
                logger.warning("Push-to-Transcribe Hook konnte nicht entfernt werden: %s", exc)
            self._push_hook = None

        emit_release = False
        with self._lock:
            emit_release = self._push_is_down
            self._push_is_down = False
            self._push_tokens = frozenset()
            self._push_scan_codes = ()
            self._pressed_scan_codes.clear()

        if emit_release:
            logger.warning(
                "Push-to-Transcribe war beim Deregistrieren aktiv; sende defensives Release-Signal."
            )
            self.push_released.emit()

        while self._handles:
            handle = self._handles.pop()
            try:
                keyboard.remove_hotkey(handle)
            except Exception as exc:
                logger.debug("Globaler Hotkey konnte nicht entfernt werden: %s", exc)
                continue

    def shutdown(self) -> None:
        self.unregister_all()
