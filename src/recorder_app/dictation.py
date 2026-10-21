"""Windows input for dictation without activating or changing the target window."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyboardInput), ("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", _InputUnion)]


@dataclass(frozen=True)
class DictationTarget:
    window: int
    focus: int
    process: int


class WindowsDictationInput:
    def __init__(self) -> None:
        self._api = None
        if sys.platform != "win32":
            return
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.GetForegroundWindow.argtypes = []
        api.GetForegroundWindow.restype = wintypes.HWND
        api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        api.GetWindowThreadProcessId.restype = wintypes.DWORD
        api.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo)]
        api.GetGUIThreadInfo.restype = wintypes.BOOL
        api.GetAsyncKeyState.argtypes = [ctypes.c_int]
        api.GetAsyncKeyState.restype = wintypes.SHORT
        api.GetClipboardSequenceNumber.argtypes = []
        api.GetClipboardSequenceNumber.restype = wintypes.DWORD
        api.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
        api.SendInput.restype = wintypes.UINT
        self._api = api

    def capture_target(self) -> DictationTarget | None:
        if self._api is None:
            return None
        window = self._api.GetForegroundWindow()
        if not window:
            return None
        pid = wintypes.DWORD()
        thread = self._api.GetWindowThreadProcessId(window, ctypes.byref(pid))
        info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
        if not thread or not self._api.GetGUIThreadInfo(thread, ctypes.byref(info)) or not info.hwndFocus:
            return None
        return DictationTarget(int(window), int(info.hwndFocus), pid.value)

    def target_is_current(self, target: DictationTarget | None) -> bool:
        return target is not None and self.capture_target() == target

    def modifiers_are_down(self) -> bool:
        return self._api is not None and any(
            self._api.GetAsyncKeyState(key) & 0x8000
            for key in (0x10, 0x11, 0x12, 0x5B, 0x5C)
        )

    def clipboard_sequence(self) -> int:
        return int(self._api.GetClipboardSequenceNumber()) if self._api is not None else 0

    def paste(self, target: DictationTarget | None) -> bool:
        if not self.target_is_current(target) or self.modifiers_are_down():
            return False
        # A single input batch keeps Ctrl/V down/up in order. No Enter is sent.
        inputs = (_Input * 4)(*(
            _Input(type=1, ki=_KeyboardInput(wVk=key, dwFlags=flags))
            for key, flags in ((0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2))
        ))
        sent = self._api.SendInput(len(inputs), inputs, ctypes.sizeof(_Input))
        if sent != len(inputs):
            # Avoid leaving synthetic modifiers pressed after a partial failure.
            releases = (_Input * 2)(*(
                _Input(type=1, ki=_KeyboardInput(wVk=key, dwFlags=2)) for key in (0x56, 0x11)
            ))
            self._api.SendInput(len(releases), releases, ctypes.sizeof(_Input))
            return False
        return True
