from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
import soundcard as sc
import soundfile as sf

from .config import (
    AppSettings,
    RECORDING_SOURCE_APPLICATION_AUDIO,
    RECORDING_SOURCE_MICROPHONE,
    RECORDING_SOURCE_SYSTEM_AUDIO,
    RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE,
    ensure_unique_path,
)

logger = logging.getLogger(__name__)

SINGLE_STOP_JOIN_TIMEOUT_SECONDS = 10.0
COMBINED_STOP_JOIN_MIN_TIMEOUT_SECONDS = 60.0
COMBINED_STOP_JOIN_MAX_TIMEOUT_SECONDS = 300.0
COMBINED_STOP_JOIN_SECONDS_PER_RECORDED_SECOND = 0.05
COMBINED_MERGE_BLOCK_SIZE_FRAMES = 262_144
COMBINED_LENGTH_WARNING_SECONDS = 2.0
COMBINED_MERGE_PROGRESS_LOG_INTERVAL_SECONDS = 10.0
APP_AUDIO_HELPER_NAME = "app_audio_capture.exe"
APP_AUDIO_HELPER_TIMEOUT_SECONDS = 8.0
APP_AUDIO_HELPER_STOP_TIMEOUT_SECONDS = 10.0


class AudioCaptureError(RuntimeError):
    """Raised when loopback recording cannot be started or completed."""


@dataclass(slots=True)
class RecordingResult:
    file_path: Path
    duration_seconds: float
    started_at: datetime
    finished_at: datetime
    capture_device: str


class AudioCaptureService:
    def __init__(self, sample_rate: int = 48_000, block_size: int = 2_048) -> None:
        self.sample_rate = sample_rate
        self.block_size = block_size
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frames_written = 0
        self._current_output_path: Path | None = None
        self._started_at: datetime | None = None
        self._last_finished_at: datetime | None = None
        self._error: Exception | None = None
        self._active_input_name = "WASAPI Loopback"

    @property
    def is_recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_recording(self, settings: AppSettings) -> Path:
        if sys.platform != "win32":
            raise AudioCaptureError("WASAPI-Loopback wird in dieser Version nur unter Windows unterstützt.")

        with self._lock:
            if self.is_recording:
                raise AudioCaptureError("Es läuft bereits eine Aufnahme.")

            file_stem = f"recording_{datetime.now():%Y%m%d_%H%M%S}"
            self._current_output_path = ensure_unique_path(settings.recordings_path(), file_stem, ".wav")
            self._frames_written = 0
            self._error = None
            self._active_input_name = "WASAPI Loopback"
            self._started_at = datetime.now()
            self._last_finished_at = None
            self._stop_event.clear()
            logger.info("Starte Systemaudio-Aufnahme nach %s", self._current_output_path)
            self._thread = threading.Thread(
                target=self._capture_loop,
                name="LoopScribeRecorder",
                daemon=True,
            )
            self._thread.start()
            return self._current_output_path

    def stop_recording(self) -> RecordingResult:
        with self._lock:
            if not self.is_recording or self._thread is None:
                raise AudioCaptureError("Es läuft derzeit keine Aufnahme.")
            thread = self._thread
            self._stop_event.set()

        thread.join(timeout=10)
        if thread.is_alive():
            raise AudioCaptureError("Die Aufnahme konnte nicht sauber beendet werden.")

        with self._lock:
            self._thread = None
            if self._error is not None:
                raise AudioCaptureError(str(self._error)) from self._error
            if self._current_output_path is None or self._started_at is None:
                raise AudioCaptureError("Die Aufnahmedatei konnte nicht ermittelt werden.")

            finished_at = self._last_finished_at or datetime.now()
            duration = self._frames_written / float(self.sample_rate)
            logger.info(
                "Aufnahme beendet: %s (%.2f Sekunden) über %s",
                self._current_output_path,
                duration,
                self._active_input_name,
            )
            return RecordingResult(
                file_path=self._current_output_path,
                duration_seconds=duration,
                started_at=self._started_at,
                finished_at=finished_at,
                capture_device=self._active_input_name,
            )

    def _capture_loop(self) -> None:
        try:
            loopback_mic = self._resolve_loopback_microphone()
            channel_map = self._build_channel_map(loopback_mic)
            output_path = self._current_output_path
            logger.info(
                "Verwende %s mit Kanalzuordnung %s für die Aufnahme",
                self._active_input_name,
                channel_map,
            )
            if output_path is None:
                raise AudioCaptureError("Kein Zielspeicherort für die Aufnahme vorhanden.")

            with loopback_mic.recorder(
                samplerate=self.sample_rate,
                channels=channel_map,
                blocksize=self.block_size,
            ) as recorder, sf.SoundFile(
                output_path,
                mode="w",
                samplerate=self.sample_rate,
                channels=len(channel_map),
                subtype="PCM_16",
            ) as audio_file:
                while not self._stop_event.is_set():
                    frames = recorder.record(numframes=self.block_size)
                    samples = np.asarray(frames, dtype=np.float32)
                    if samples.size == 0:
                        continue
                    audio_file.write(np.clip(samples, -1.0, 1.0))
                    self._frames_written += int(samples.shape[0])

                self._flush_recorder(recorder, audio_file)
        except Exception as exc:
            self._error = exc
            logger.exception("Aufnahmefehler beim WASAPI-Loopback")
        finally:
            self._last_finished_at = datetime.now()
            logger.info("Aufnahme-Thread beendet")

    def _resolve_loopback_microphone(self):
        speaker = sc.default_speaker()
        all_mics = sc.all_microphones(include_loopback=True)
        speaker_name = speaker.name.lower()
        logger.info("Suche WASAPI-Loopback für Standardlautsprecher %s", speaker.name)

        preferred = [
            mic
            for mic in all_mics
            if getattr(mic, "isloopback", False) and speaker_name in mic.name.lower()
        ]
        if preferred:
            self._active_input_name = f"WASAPI Loopback: {preferred[0].name}"
            logger.info(
                "Loopback-Quelle gefunden: Lautsprecher=%s, Capture=%s",
                speaker.name,
                preferred[0].name,
            )
            return preferred[0]

        try:
            candidate = sc.get_microphone(speaker.name, include_loopback=True)
            if getattr(candidate, "isloopback", False):
                self._active_input_name = f"WASAPI Loopback: {candidate.name}"
                logger.info(
                    "Loopback-Quelle über get_microphone gefunden: Lautsprecher=%s, Capture=%s",
                    speaker.name,
                    candidate.name,
                )
                return candidate
            logger.warning(
                "get_microphone lieferte %s ohne isloopback=True. Mikrofon wird bewusst nicht verwendet.",
                getattr(candidate, "name", "<unbekannt>"),
            )
        except Exception:
            logger.warning(
                "Loopback-Ermittlung über get_microphone ist fehlgeschlagen",
                exc_info=True,
            )

        fallback = [mic for mic in all_mics if getattr(mic, "isloopback", False)]
        if fallback:
            self._active_input_name = f"WASAPI Loopback: {fallback[0].name}"
            logger.warning(
                "Nutze Loopback-Fallback: Lautsprecher=%s, Capture=%s",
                speaker.name,
                fallback[0].name,
            )
            return fallback[0]

        raise AudioCaptureError(
            "Kein Loopback-Gerät gefunden. Prüfe, ob ein aktives Wiedergabegerät als Standard gesetzt ist."
        )

    @staticmethod
    def _build_channel_map(loopback_mic) -> list[int]:
        channel_count = getattr(loopback_mic, "channels", 2) or 2
        if isinstance(channel_count, int) and channel_count >= 2:
            return [0, 1]
        if isinstance(channel_count, int) and channel_count == 1:
            return [0]
        return [0, 1]

    def _flush_recorder(self, recorder, audio_file) -> None:
        flush = getattr(recorder, "flush", None)
        if flush is None:
            return

        try:
            tail = flush()
        except Exception:
            logger.warning("Recorder-Flush ist fehlgeschlagen", exc_info=True)
            return

        samples = np.asarray(tail, dtype=np.float32)
        if samples.size == 0:
            return

        audio_file.write(np.clip(samples, -1.0, 1.0))
        self._frames_written += int(samples.shape[0])


@dataclass(slots=True)
class MicrophoneDeviceInfo:
    id: str
    name: str
    is_default: bool = False

    @property
    def display_name(self) -> str:
        if self.is_default:
            return f"{self.name} (Standard)"
        return self.name


@dataclass(slots=True)
class ApplicationAudioCapability:
    supported: bool
    reason: str
    helper_path: Path | None = None
    windows_build: int | None = None


@dataclass(slots=True)
class ApplicationAudioSessionInfo:
    id: str
    pid: int
    process_name: str
    display_name: str
    executable_path: str
    state: str

    @property
    def is_active(self) -> bool:
        return self.state.lower() == "active"

    @property
    def display_label(self) -> str:
        state_label = "aktiv" if self.is_active else "bereit"
        return f"{self.display_name} ({self.process_name}, PID {self.pid}, {state_label})"


@dataclass(slots=True)
class _CombinedCaptureBranch:
    source: str
    label: str
    device: object
    channel_map: list[int]
    output_path: Path
    ready_event: threading.Event
    frames_written: int = 0
    error: Exception | None = None


class AudioCaptureService:
    def __init__(self, sample_rate: int = 48_000, block_size: int = 2_048) -> None:
        self.sample_rate = sample_rate
        self.block_size = block_size
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frames_written = 0
        self._current_output_path: Path | None = None
        self._started_at: datetime | None = None
        self._last_finished_at: datetime | None = None
        self._error: Exception | None = None
        self._selected_device_id = ""
        self._selected_microphone_device_id = ""
        self._selected_application_audio_session_id = ""
        self._combined_devices: tuple[tuple[object, str], tuple[object, str]] | None = None
        self._recording_source = RECORDING_SOURCE_SYSTEM_AUDIO
        self._active_input_name = "WASAPI Loopback"
        self._app_audio_ready_event: threading.Event | None = None
        self._app_audio_process: subprocess.Popen[str] | None = None
        self._app_audio_capability_cache: ApplicationAudioCapability | None = None

    @property
    def is_recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def list_loopback_devices(self) -> list[LoopbackDeviceInfo]:
        if sys.platform != "win32":
            return []

        try:
            default_speaker = sc.default_speaker()
            default_id = str(getattr(default_speaker, "id", "") or "")
            microphones = sc.all_microphones(include_loopback=True)
        except Exception as exc:
            logger.exception("WASAPI-Loopback-Geräte konnten nicht geladen werden")
            raise AudioCaptureError(
                "Die verfügbaren WASAPI-Loopback-Geräte konnten nicht gelesen werden."
            ) from exc

        devices: dict[str, LoopbackDeviceInfo] = {}
        for microphone in microphones:
            if not getattr(microphone, "isloopback", False):
                continue
            device_id = str(getattr(microphone, "id", "") or "")
            if not device_id:
                continue
            devices[device_id] = LoopbackDeviceInfo(
                id=device_id,
                name=str(getattr(microphone, "name", "Unbekanntes Loopback-Gerät")),
                is_default=device_id == default_id,
            )

        result = sorted(
            devices.values(),
            key=lambda item: (not item.is_default, item.name.lower()),
        )
        logger.info("Gefundene WASAPI-Loopback-Geräte: %s", [device.name for device in result])
        return result

    def list_microphone_devices(self) -> list[MicrophoneDeviceInfo]:
        default_id = ""
        try:
            default_microphone = sc.default_microphone()
            default_id = str(getattr(default_microphone, "id", "") or "")
        except Exception:
            logger.info("Kein Standard-Mikrofon ermittelbar", exc_info=True)

        try:
            microphones = sc.all_microphones(include_loopback=False)
        except Exception as exc:
            logger.exception("Mikrofon-Geräte konnten nicht geladen werden")
            raise AudioCaptureError("Die verfügbaren Mikrofone konnten nicht gelesen werden.") from exc

        devices: dict[str, MicrophoneDeviceInfo] = {}
        for microphone in microphones:
            if getattr(microphone, "isloopback", False):
                continue
            device_id = str(getattr(microphone, "id", "") or "")
            if not device_id:
                continue
            devices[device_id] = MicrophoneDeviceInfo(
                id=device_id,
                name=str(getattr(microphone, "name", "Unbekanntes Mikrofon")),
                is_default=device_id == default_id,
            )

        result = sorted(
            devices.values(),
            key=lambda item: (not item.is_default, item.name.lower()),
        )
        logger.info("Gefundene Mikrofone: %s", [device.name for device in result])
        return result

    def application_audio_capability(self, *, force_refresh: bool = False) -> ApplicationAudioCapability:
        if not force_refresh and self._app_audio_capability_cache is not None:
            return self._app_audio_capability_cache

        helper_path = self._application_audio_helper_path()
        if sys.platform != "win32":
            capability = ApplicationAudioCapability(
                supported=False,
                reason="App-Audio ist nur unter Windows verfügbar.",
                helper_path=helper_path,
            )
            self._app_audio_capability_cache = capability
            return capability

        if not helper_path.exists():
            capability = ApplicationAudioCapability(
                supported=False,
                reason=f"App-Audio-Helfer nicht gefunden: {helper_path}",
                helper_path=helper_path,
            )
            self._app_audio_capability_cache = capability
            return capability

        try:
            capability_payload = self._run_application_audio_helper(["capability"], timeout=5.0)
            smoke_payload = self._run_application_audio_helper(["smoke"], timeout=APP_AUDIO_HELPER_TIMEOUT_SECONDS)
        except AudioCaptureError as exc:
            capability = ApplicationAudioCapability(
                supported=False,
                reason=str(exc),
                helper_path=helper_path,
            )
            self._app_audio_capability_cache = capability
            logger.warning("App-Audio Capability-Smoke fehlgeschlagen: %s", exc)
            return capability

        supported = bool(capability_payload.get("supported")) and bool(
            smoke_payload.get("supported", smoke_payload.get("ok"))
        )
        reason = str(smoke_payload.get("reason") or capability_payload.get("reason") or "")
        capability = ApplicationAudioCapability(
            supported=supported,
            reason=reason if reason else ("App-Audio verfügbar" if supported else "App-Audio nicht verfügbar"),
            helper_path=helper_path,
            windows_build=self._safe_int(capability_payload.get("windows_build")),
        )
        self._app_audio_capability_cache = capability
        logger.info(
            "App-Audio Capability: supported=%s reason=%s helper=%s windows_build=%s",
            capability.supported,
            capability.reason,
            capability.helper_path,
            capability.windows_build,
        )
        return capability

    def list_application_audio_sessions(self, *, force_refresh: bool = False) -> list[ApplicationAudioSessionInfo]:
        capability = self.application_audio_capability(force_refresh=force_refresh)
        if not capability.supported:
            logger.info("App-Audio-Sessions nicht geladen: %s", capability.reason)
            return []

        payload = self._run_application_audio_helper(["list"], timeout=APP_AUDIO_HELPER_TIMEOUT_SECONDS)
        sessions_payload = payload.get("sessions")
        if not isinstance(sessions_payload, list):
            raise AudioCaptureError("Der App-Audio-Helfer hat keine gültige Session-Liste geliefert.")

        sessions: list[ApplicationAudioSessionInfo] = []
        for item in sessions_payload:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("id") or "").strip()
            pid = self._safe_int(item.get("pid")) or 0
            if not session_id or pid <= 0:
                continue
            sessions.append(
                ApplicationAudioSessionInfo(
                    id=session_id,
                    pid=pid,
                    process_name=str(item.get("process_name") or f"PID {pid}"),
                    display_name=str(item.get("display_name") or item.get("process_name") or f"PID {pid}"),
                    executable_path=str(item.get("executable_path") or ""),
                    state=str(item.get("state") or "Unknown"),
                )
            )

        logger.info("Gefundene App-Audio-Sessions: %s", [session.display_label for session in sessions])
        return sessions

    def start_recording(self, settings: AppSettings) -> Path:
        source = settings.recording_source or RECORDING_SOURCE_SYSTEM_AUDIO
        if source not in {
            RECORDING_SOURCE_SYSTEM_AUDIO,
            RECORDING_SOURCE_MICROPHONE,
            RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE,
            RECORDING_SOURCE_APPLICATION_AUDIO,
        }:
            raise AudioCaptureError("Die gespeicherte Aufnahmequelle ist ungültig. Bitte die Einstellungen prüfen.")
        if source in {
            RECORDING_SOURCE_SYSTEM_AUDIO,
            RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE,
            RECORDING_SOURCE_APPLICATION_AUDIO,
        } and sys.platform != "win32":
            raise AudioCaptureError("WASAPI loopback is only supported on Windows in this build.")

        with self._lock:
            if self.is_recording:
                raise AudioCaptureError("Es läuft bereits eine Aufnahme.")

            combined_devices: tuple[tuple[object, str], tuple[object, str]] | None = None
            combined_input_label = self._source_label(source)
            app_audio_session: ApplicationAudioSessionInfo | None = None
            if source == RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE:
                system_device = self._resolve_loopback_microphone(settings.audio_device_id)
                system_label = self._active_input_name
                microphone_device = self._resolve_input_microphone(settings.microphone_device_id)
                microphone_label = self._active_input_name
                combined_devices = (
                    (system_device, system_label),
                    (microphone_device, microphone_label),
                )
                combined_input_label = f"{system_label} + {microphone_label}"
            elif source == RECORDING_SOURCE_APPLICATION_AUDIO:
                app_audio_session = self._resolve_application_audio_session(
                    settings.application_audio_session_id
                )
                combined_input_label = app_audio_session.display_label

            file_stem = self._build_file_stem(source)
            self._current_output_path = ensure_unique_path(settings.recordings_path(), file_stem, ".wav")
            self._frames_written = 0
            self._error = None
            self._recording_source = source
            self._selected_device_id = (
                settings.audio_device_id
                if source != RECORDING_SOURCE_MICROPHONE
                else settings.microphone_device_id
            )
            self._selected_microphone_device_id = settings.microphone_device_id
            self._selected_application_audio_session_id = (
                app_audio_session.id if app_audio_session is not None else ""
            )
            self._combined_devices = combined_devices
            self._active_input_name = combined_input_label
            self._app_audio_ready_event = (
                threading.Event() if source == RECORDING_SOURCE_APPLICATION_AUDIO else None
            )
            self._app_audio_process = None
            self._started_at = datetime.now()
            self._last_finished_at = None
            self._stop_event.clear()
            logger.info(
                "Starte Aufnahme: quelle=%s geraet=%s ziel=%s",
                self._source_label(source),
                self._active_input_name if source in {
                    RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE,
                    RECORDING_SOURCE_APPLICATION_AUDIO,
                } else self._selected_device_id or "<Standard>",
                self._current_output_path,
            )
            self._thread = threading.Thread(
                target=self._capture_loop,
                name="LoopScribeRecorder",
                daemon=True,
            )
            self._thread.start()
            output_path = self._current_output_path
            ready_event = self._app_audio_ready_event

        if source == RECORDING_SOURCE_APPLICATION_AUDIO and ready_event is not None:
            if not ready_event.wait(APP_AUDIO_HELPER_TIMEOUT_SECONDS):
                self._stop_event.set()
                process = self._app_audio_process
                if process is not None and process.poll() is None:
                    process.kill()
                with self._lock:
                    self._thread = None
                raise AudioCaptureError("App-Audio-Aufnahme konnte nicht rechtzeitig gestartet werden.")
            if self._error is not None:
                with self._lock:
                    self._thread = None
                raise AudioCaptureError(str(self._error)) from self._error

        return output_path

    def stop_recording(self) -> RecordingResult:
        with self._lock:
            if not self.is_recording or self._thread is None:
                raise AudioCaptureError("Es läuft derzeit keine Aufnahme.")
            thread = self._thread
            stop_timeout = self._stop_join_timeout_seconds()
            self._stop_event.set()

        logger.info(
            "Stop angefordert: quelle=%s timeout=%.1fs",
            self._source_label(self._recording_source),
            stop_timeout,
        )
        thread.join(timeout=stop_timeout)
        if thread.is_alive():
            raise AudioCaptureError(
                "Die Aufnahme konnte nicht sauber beendet werden. Bei langen Kombi-Aufnahmen kann das auf einen blockierten Recorder oder Merge-Prozess hinweisen."
            )

        with self._lock:
            self._thread = None
            if self._error is not None:
                raise AudioCaptureError(str(self._error)) from self._error
            if self._current_output_path is None or self._started_at is None:
                raise AudioCaptureError("Die Aufnahmedatei konnte nicht ermittelt werden.")

            finished_at = self._last_finished_at or datetime.now()
            duration = self._frames_written / float(self.sample_rate)
            capture_label = f"{self._source_label(self._recording_source)} | {self._active_input_name}"
            logger.info(
                "Aufnahme beendet: %s (%.2f Sekunden) über %s",
                self._current_output_path,
                duration,
                capture_label,
            )
            return RecordingResult(
                file_path=self._current_output_path,
                duration_seconds=duration,
                started_at=self._started_at,
                finished_at=finished_at,
                capture_device=capture_label,
            )

    def _stop_join_timeout_seconds(self) -> float:
        if self._recording_source != RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE:
            return SINGLE_STOP_JOIN_TIMEOUT_SECONDS

        recorded_seconds = 0.0
        if self._started_at is not None:
            recorded_seconds = max(0.0, (datetime.now() - self._started_at).total_seconds())
        return min(
            COMBINED_STOP_JOIN_MAX_TIMEOUT_SECONDS,
            max(
                COMBINED_STOP_JOIN_MIN_TIMEOUT_SECONDS,
                recorded_seconds * COMBINED_STOP_JOIN_SECONDS_PER_RECORDED_SECOND,
            ),
        )

    def _capture_loop(self) -> None:
        if self._recording_source == RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE:
            self._capture_combined_loop()
            return
        if self._recording_source == RECORDING_SOURCE_APPLICATION_AUDIO:
            self._capture_application_audio_loop()
            return

        try:
            source_device = self._resolve_source_device(self._recording_source, self._selected_device_id)
            channel_map = self._build_channel_map(source_device)
            output_path = self._current_output_path
            logger.info(
                "Verwende Aufnahmequelle %s mit Gerät %s und Kanalzuordnung %s",
                self._source_label(self._recording_source),
                self._active_input_name,
                channel_map,
            )
            if output_path is None:
                raise AudioCaptureError("Kein Zielspeicherort für die Aufnahme vorhanden.")

            try:
                audio_file = sf.SoundFile(
                    output_path,
                    mode="w",
                    samplerate=self.sample_rate,
                    channels=len(channel_map),
                    subtype="PCM_16",
                )
            except OSError as exc:
                raise AudioCaptureError(
                    "Die Aufnahmedatei kann nicht geschrieben werden. Bitte prüfe Schreibrechte oder ob die Datei geöffnet ist."
                ) from exc

            recorder_manager = source_device.recorder(
                samplerate=self.sample_rate,
                channels=channel_map,
                blocksize=self.block_size,
            )
            try:
                with recorder_manager as recorder, audio_file:
                    while not self._stop_event.is_set():
                        frames = recorder.record(numframes=self.block_size)
                        samples = np.asarray(frames, dtype=np.float32)
                        if samples.size == 0:
                            continue
                        audio_file.write(np.clip(samples, -1.0, 1.0))
                        self._frames_written += int(samples.shape[0])

                    self._flush_recorder(recorder, audio_file)
            except AudioCaptureError:
                raise
            except Exception as exc:
                raise self._capture_access_error(exc) from exc
        except Exception as exc:
            self._error = exc
            logger.exception("Aufnahmefehler für Quelle %s", self._source_label(self._recording_source))
        finally:
            self._last_finished_at = datetime.now()
            logger.info("Aufnahme-Thread beendet")

    def _capture_application_audio_loop(self) -> None:
        ready_event = self._app_audio_ready_event
        process: subprocess.Popen[str] | None = None
        try:
            output_path = self._current_output_path
            if output_path is None:
                raise AudioCaptureError("Kein Zielspeicherort für die App-Audio-Aufnahme vorhanden.")
            session = self._resolve_application_audio_session(self._selected_application_audio_session_id)
            logger.info(
                "Starte App-Audio-Helfer: session=%s pid=%s ziel=%s",
                session.display_label,
                session.pid,
                output_path,
            )
            command = [
                str(self._application_audio_helper_path()),
                "capture",
                "--pid",
                str(session.pid),
                "--output",
                str(output_path),
                "--sample-rate",
                str(self.sample_rate),
            ]
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            self._app_audio_process = process

            first_line = process.stdout.readline().strip() if process.stdout is not None else ""
            if first_line != "READY":
                stderr_text = process.stderr.read() if process.stderr is not None else ""
                raise AudioCaptureError(
                    f"App-Audio-Helfer konnte nicht starten: {first_line or stderr_text or 'keine Rückmeldung'}"
                )
            if ready_event is not None:
                ready_event.set()

            while not self._stop_event.is_set():
                return_code = process.poll()
                if return_code is not None:
                    stderr_text = process.stderr.read() if process.stderr is not None else ""
                    raise AudioCaptureError(
                        f"App-Audio-Session wurde während der Aufnahme beendet oder der Helfer ist ausgestiegen (Code {return_code}). {stderr_text}".strip()
                    )
                time.sleep(0.04)

            if process.stdin is not None:
                try:
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                except OSError:
                    logger.warning("Stop-Signal konnte nicht an den App-Audio-Helfer gesendet werden", exc_info=True)

            try:
                stdout_text, stderr_text = process.communicate(timeout=APP_AUDIO_HELPER_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                stdout_text, stderr_text = process.communicate(timeout=2.0)
                raise AudioCaptureError("App-Audio-Helfer konnte nicht rechtzeitig gestoppt werden.") from exc

            if process.returncode != 0:
                raise AudioCaptureError(
                    f"App-Audio-Aufnahme fehlgeschlagen (Code {process.returncode}). {stderr_text or stdout_text}".strip()
                )

            info = sf.info(output_path)
            self._frames_written = int(info.frames)
            self._active_input_name = session.display_label
            logger.info(
                "App-Audio-Aufnahme beendet: session=%s frames=%s samplerate=%s dauer=%.2fs",
                session.display_label,
                info.frames,
                info.samplerate,
                float(info.frames) / float(info.samplerate) if info.samplerate else 0.0,
            )
        except Exception as exc:
            self._error = exc
            logger.exception("Aufnahmefehler für Quelle %s", self._source_label(self._recording_source))
            if ready_event is not None:
                ready_event.set()
        finally:
            self._last_finished_at = datetime.now()
            self._app_audio_process = None
            logger.info("App-Audio-Aufnahme-Thread beendet")

    def _capture_combined_loop(self) -> None:
        temp_paths: list[Path] = []
        try:
            output_path = self._current_output_path
            if output_path is None:
                raise AudioCaptureError("Kein Zielspeicherort für die Aufnahme vorhanden.")
            if self._combined_devices is None:
                raise AudioCaptureError("Die Kombi-Aufnahme wurde nicht vollständig vorbereitet.")

            (system_device, system_label), (microphone_device, microphone_label) = self._combined_devices
            system_temp_path = ensure_unique_path(
                output_path.parent,
                f"{output_path.stem}_systemaudio_source",
                ".wav",
            )
            microphone_temp_path = ensure_unique_path(
                output_path.parent,
                f"{output_path.stem}_microphone_source",
                ".wav",
            )
            temp_paths = [system_temp_path, microphone_temp_path]

            branches = [
                _CombinedCaptureBranch(
                    source=RECORDING_SOURCE_SYSTEM_AUDIO,
                    label=system_label,
                    device=system_device,
                    channel_map=self._build_channel_map(system_device),
                    output_path=system_temp_path,
                    ready_event=threading.Event(),
                ),
                _CombinedCaptureBranch(
                    source=RECORDING_SOURCE_MICROPHONE,
                    label=microphone_label,
                    device=microphone_device,
                    channel_map=self._build_channel_map(microphone_device),
                    output_path=microphone_temp_path,
                    ready_event=threading.Event(),
                ),
            ]
            self._active_input_name = f"{system_label} + {microphone_label}"
            logger.info(
                "Starte Kombi-Aufnahme: systemaudio=%s mikrofon=%s ziel=%s",
                system_label,
                microphone_label,
                output_path,
            )

            start_event = threading.Event()
            threads = [
                threading.Thread(
                    target=self._capture_combined_branch_loop,
                    args=(branch, start_event),
                    name=f"LoopScribeRecorder-{branch.source}",
                    daemon=True,
                )
                for branch in branches
            ]
            for thread in threads:
                thread.start()

            self._wait_for_combined_branches_ready(branches, threads, start_event)
            self._started_at = datetime.now()
            start_event.set()
            logger.info("Kombi-Aufnahme synchron freigegeben")

            while not self._stop_event.is_set():
                if any(branch.error is not None for branch in branches):
                    self._stop_event.set()
                    break
                if any(not thread.is_alive() for thread in threads):
                    for branch, thread in zip(branches, threads):
                        if not thread.is_alive() and branch.error is None:
                            branch.error = AudioCaptureError(
                                f"Kombi-Aufnahmezweig wurde unerwartet beendet: {branch.label}"
                            )
                    self._stop_event.set()
                    break
                time.sleep(0.05)

            for thread in threads:
                thread.join(timeout=10)
            if any(thread.is_alive() for thread in threads):
                raise AudioCaptureError("Die Kombi-Aufnahme konnte nicht sauber beendet werden.")

            for branch in branches:
                logger.info(
                    "Kombi-Quelle beendet: quelle=%s geraet=%s frames=%s dauer=%.2fs temp=%s",
                    self._source_label(branch.source),
                    branch.label,
                    branch.frames_written,
                    branch.frames_written / float(self.sample_rate),
                    branch.output_path,
                )

            branch_errors = [branch.error for branch in branches if branch.error is not None]
            if branch_errors:
                raise AudioCaptureError(str(branch_errors[0])) from branch_errors[0]

            self._frames_written = self._merge_recordings(system_temp_path, microphone_temp_path, output_path)
            self._delete_temp_recordings(temp_paths)
            logger.info(
                "Kombi-Aufnahme gemischt: ziel=%s frames=%s",
                output_path,
                self._frames_written,
            )
        except Exception as exc:
            self._error = exc
            if temp_paths:
                logger.warning(
                    "Temporäre Kombi-Aufnahmen bleiben zur Fehleranalyse erhalten: %s",
                    [str(path) for path in temp_paths],
                )
            logger.exception("Aufnahmefehler für Quelle %s", self._source_label(self._recording_source))
        finally:
            self._last_finished_at = datetime.now()
            logger.info("Kombi-Aufnahme-Thread beendet")

    def _wait_for_combined_branches_ready(
        self,
        branches: list[_CombinedCaptureBranch],
        threads: list[threading.Thread],
        start_event: threading.Event,
    ) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                break
            if all(branch.ready_event.is_set() for branch in branches):
                break
            if any(branch.error is not None for branch in branches):
                break
            if any(not thread.is_alive() for thread in threads):
                break
            time.sleep(0.02)

        branch_errors = [branch.error for branch in branches if branch.error is not None]
        if branch_errors:
            self._stop_event.set()
            start_event.set()
            for thread in threads:
                thread.join(timeout=2)
            raise AudioCaptureError(str(branch_errors[0])) from branch_errors[0]

        if not all(branch.ready_event.is_set() for branch in branches):
            self._stop_event.set()
            start_event.set()
            for thread in threads:
                thread.join(timeout=2)
            raise AudioCaptureError("Die Kombi-Aufnahme konnte nicht vollständig vorbereitet werden.")

    def _capture_combined_branch_loop(
        self,
        branch: _CombinedCaptureBranch,
        start_event: threading.Event,
    ) -> None:
        try:
            try:
                audio_file = sf.SoundFile(
                    branch.output_path,
                    mode="w",
                    samplerate=self.sample_rate,
                    channels=len(branch.channel_map),
                    subtype="PCM_16",
                )
            except OSError as exc:
                raise AudioCaptureError(
                    "Die temporäre Aufnahmedatei kann nicht geschrieben werden. Bitte prüfe Schreibrechte oder ob die Datei geöffnet ist."
                ) from exc

            recorder_manager = branch.device.recorder(
                samplerate=self.sample_rate,
                channels=branch.channel_map,
                blocksize=self.block_size,
            )
            with recorder_manager as recorder, audio_file:
                branch.ready_event.set()
                start_event.wait()
                while not self._stop_event.is_set():
                    frames = recorder.record(numframes=self.block_size)
                    samples = np.asarray(frames, dtype=np.float32)
                    if samples.size == 0:
                        continue
                    audio_file.write(np.clip(samples, -1.0, 1.0))
                    branch.frames_written += int(samples.shape[0])

                branch.frames_written += self._flush_recorder_to_file(recorder, audio_file)
        except AudioCaptureError as exc:
            branch.error = exc
            self._stop_event.set()
            logger.exception("Kombi-Aufnahmezweig fehlgeschlagen: %s", branch.label)
        except Exception as exc:
            branch.error = self._capture_access_error_for_source(branch.source, exc)
            self._stop_event.set()
            logger.exception("Kombi-Aufnahmezweig fehlgeschlagen: %s", branch.label)
        finally:
            branch.ready_event.set()

    def _flush_recorder_to_file(self, recorder, audio_file) -> int:
        flush = getattr(recorder, "flush", None)
        if flush is None:
            return 0

        try:
            tail = flush()
        except Exception:
            logger.warning("Recorder-Flush ist fehlgeschlagen", exc_info=True)
            return 0

        samples = np.asarray(tail, dtype=np.float32)
        if samples.size == 0:
            return 0

        audio_file.write(np.clip(samples, -1.0, 1.0))
        return int(samples.shape[0])

    def _merge_recordings(self, system_path: Path, microphone_path: Path, target_path: Path) -> int:
        merge_started_at = time.monotonic()
        system_rms, system_peak = self._measure_mono_level(system_path)
        microphone_rms, microphone_peak = self._measure_mono_level(microphone_path)
        system_gain, microphone_gain = self._calculate_mix_gains(system_rms, microphone_rms)
        logger.info(
            "Kombi-Mix Pegel: system_rms=%.5f system_peak=%.5f system_gain=%.2f "
            "microphone_rms=%.5f microphone_peak=%.5f microphone_gain=%.2f",
            system_rms,
            system_peak,
            system_gain,
            microphone_rms,
            microphone_peak,
            microphone_gain,
        )

        try:
            system_file = sf.SoundFile(system_path)
            microphone_file = sf.SoundFile(microphone_path)
        except OSError as exc:
            raise AudioCaptureError("Die temporären Kombi-Aufnahmen konnten nicht gelesen werden.") from exc

        with system_file, microphone_file:
            if system_file.samplerate != self.sample_rate or microphone_file.samplerate != self.sample_rate:
                raise AudioCaptureError("Die Kombi-Aufnahmen haben eine unerwartete Sample-Rate.")

            system_frames = system_file.frames
            microphone_frames = microphone_file.frames
            system_duration = system_frames / float(self.sample_rate)
            microphone_duration = microphone_frames / float(self.sample_rate)
            length_delta_seconds = abs(system_frames - microphone_frames) / float(self.sample_rate)
            logger.info(
                "Kombi-Merge Eingaben: system_frames=%s system_dauer=%.2fs microphone_frames=%s microphone_dauer=%.2fs differenz=%.2fs",
                system_frames,
                system_duration,
                microphone_frames,
                microphone_duration,
                length_delta_seconds,
            )
            if length_delta_seconds > COMBINED_LENGTH_WARNING_SECONDS:
                logger.warning(
                    "Kombi-Aufnahmen haben eine auffällige Längendifferenz von %.2fs. "
                    "Die kürzere Quelle wird mit Stille aufgefüllt; mögliche Ursache ist Device-Drift oder ein verspäteter Start/Stop.",
                    length_delta_seconds,
                )

            total_frames = max(system_frames, microphone_frames)
            if total_frames <= 0:
                raise AudioCaptureError("Die Kombi-Aufnahme enthält keine Audiodaten.")

            try:
                output_file = sf.SoundFile(
                    target_path,
                    mode="w",
                    samplerate=self.sample_rate,
                    channels=1,
                    subtype="PCM_16",
                )
            except OSError as exc:
                raise AudioCaptureError(
                    "Die gemischte Aufnahmedatei kann nicht geschrieben werden. Bitte prüfe Schreibrechte oder ob die Datei geöffnet ist."
                ) from exc

            written_frames = 0
            mix_block_size = max(COMBINED_MERGE_BLOCK_SIZE_FRAMES, self.block_size)
            next_progress_log_at = time.monotonic() + COMBINED_MERGE_PROGRESS_LOG_INTERVAL_SECONDS
            with output_file:
                while written_frames < total_frames:
                    requested_frames = min(mix_block_size, total_frames - written_frames)
                    system_samples = self._pad_mono_block(
                        self._read_mono_block(system_file, requested_frames),
                        requested_frames,
                    )
                    microphone_samples = self._pad_mono_block(
                        self._read_mono_block(microphone_file, requested_frames),
                        requested_frames,
                    )
                    mixed_samples = (
                        0.5 * system_samples * system_gain
                        + 0.5 * microphone_samples * microphone_gain
                    )
                    output_file.write(np.clip(mixed_samples, -1.0, 1.0).astype(np.float32))
                    written_frames += requested_frames
                    now = time.monotonic()
                    if now >= next_progress_log_at:
                        logger.info(
                            "Kombi-Merge Fortschritt: %.1f%% (%.1fs / %.1fs)",
                            (written_frames / total_frames) * 100.0,
                            written_frames / float(self.sample_rate),
                            total_frames / float(self.sample_rate),
                        )
                        next_progress_log_at = now + COMBINED_MERGE_PROGRESS_LOG_INTERVAL_SECONDS

            logger.info(
                "Kombi-Merge abgeschlossen: ziel=%s frames=%s dauer=%.2fs merge_dauer=%.2fs block_frames=%s",
                target_path,
                written_frames,
                written_frames / float(self.sample_rate),
                time.monotonic() - merge_started_at,
                mix_block_size,
            )
            return written_frames

    def _measure_mono_level(self, path: Path) -> tuple[float, float]:
        try:
            audio_file = sf.SoundFile(path)
        except OSError as exc:
            raise AudioCaptureError(f"Die temporäre Aufnahmedatei '{path.name}' konnte nicht gelesen werden.") from exc

        total_square = 0.0
        total_count = 0
        peak = 0.0
        with audio_file:
            while True:
                samples = self._read_mono_block(audio_file, COMBINED_MERGE_BLOCK_SIZE_FRAMES)
                if samples.size == 0:
                    break
                total_square += float(np.sum(np.square(samples.astype(np.float64, copy=False))))
                total_count += int(samples.shape[0])
                peak = max(peak, float(np.max(np.abs(samples))))

        rms = float(np.sqrt(total_square / total_count)) if total_count else 0.0
        return rms, peak

    @staticmethod
    def _calculate_mix_gains(system_rms: float, microphone_rms: float) -> tuple[float, float]:
        silence_threshold = 1e-4
        active_levels = [
            level
            for level in (system_rms, microphone_rms)
            if level > silence_threshold
        ]
        if len(active_levels) < 2:
            return 1.0, 1.0

        target_level = float(np.median(active_levels))
        def bounded_gain(source_level: float) -> float:
            if source_level <= silence_threshold:
                return 1.0
            return float(np.clip(target_level / source_level, 0.5, 2.0))

        return (
            bounded_gain(system_rms),
            bounded_gain(microphone_rms),
        )

    @staticmethod
    def _read_mono_block(audio_file, frame_count: int) -> np.ndarray:
        samples = audio_file.read(frames=frame_count, dtype="float32", always_2d=True)
        if samples.size == 0:
            return np.empty(0, dtype=np.float32)
        return np.asarray(samples, dtype=np.float32).mean(axis=1).astype(np.float32, copy=False)

    @staticmethod
    def _pad_mono_block(samples: np.ndarray, frame_count: int) -> np.ndarray:
        if samples.shape[0] >= frame_count:
            return samples
        return np.pad(samples, (0, frame_count - samples.shape[0]), mode="constant")

    @staticmethod
    def _delete_temp_recordings(paths: list[Path]) -> None:
        for path in paths:
            try:
                if path.exists():
                    path.unlink()
                    logger.info("Temporäre Kombi-Aufnahme gelöscht: %s", path)
            except OSError:
                logger.warning("Temporäre Kombi-Aufnahme konnte nicht gelöscht werden: %s", path, exc_info=True)

    def _resolve_application_audio_session(self, session_id: str) -> ApplicationAudioSessionInfo:
        capability = self.application_audio_capability()
        if not capability.supported:
            raise AudioCaptureError(
                f"App-Audio ist auf diesem System nicht verfügbar: {capability.reason}"
            )
        if not session_id:
            raise AudioCaptureError(
                "Keine App-Audio-Session ausgewählt. Bitte eine laufende Audio-App wählen und die Liste aktualisieren."
            )

        sessions = self.list_application_audio_sessions()
        selected = next((session for session in sessions if session.id == session_id), None)
        if selected is None:
            raise AudioCaptureError(
                "Die ausgewählte App-Audio-Session ist nicht mehr verfügbar. Bitte die App-Liste aktualisieren."
            )
        return selected

    @staticmethod
    def _application_audio_helper_path() -> Path:
        return Path(__file__).resolve().parent / "native" / APP_AUDIO_HELPER_NAME

    def _run_application_audio_helper(self, args: list[str], *, timeout: float) -> dict[str, object]:
        helper_path = self._application_audio_helper_path()
        if not helper_path.exists():
            raise AudioCaptureError(f"App-Audio-Helfer nicht gefunden: {helper_path}")

        command = [str(helper_path), *args]
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise AudioCaptureError("App-Audio-Helfer hat nicht rechtzeitig geantwortet.") from exc
        except OSError as exc:
            raise AudioCaptureError(f"App-Audio-Helfer konnte nicht gestartet werden: {exc}") from exc

        output = completed.stdout.strip()
        if not output:
            output = "{}"
        try:
            payload = json.loads(output.splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise AudioCaptureError(
                f"App-Audio-Helfer lieferte keine gültige JSON-Antwort: {output or completed.stderr}"
            ) from exc

        if completed.returncode != 0 or payload.get("ok") is False:
            reason = str(payload.get("reason") or payload.get("error") or completed.stderr or output)
            raise AudioCaptureError(reason)
        return payload

    @staticmethod
    def _safe_int(value: object) -> int | None:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _resolve_source_device(self, source: str, device_id: str):
        if source == RECORDING_SOURCE_MICROPHONE:
            return self._resolve_input_microphone(device_id)
        return self._resolve_loopback_microphone(device_id)

    def _resolve_loopback_microphone(self, device_id: str):
        devices = self.list_loopback_devices()
        if not devices:
            raise AudioCaptureError(
                "Kein WASAPI-Loopback-Gerät gefunden. Bitte prüfe, ob ein Wiedergabegerät aktiv ist."
            )

        if device_id:
            selected = next((device for device in devices if device.id == device_id), None)
            if selected is None:
                raise AudioCaptureError(
                    "Das ausgewählte WASAPI-Loopback-Gerät ist nicht mehr verfügbar. Bitte die Geräteliste aktualisieren."
                )
            return self._get_loopback_device(selected)

        default_device = next((device for device in devices if device.is_default), devices[0])
        return self._get_loopback_device(default_device)

    def _resolve_input_microphone(self, device_id: str):
        devices = self.list_microphone_devices()
        if not devices:
            raise AudioCaptureError(
                "Kein Mikrofon gefunden. Bitte ein Eingabegerät anschließen oder aktivieren."
            )

        if device_id:
            selected = next((device for device in devices if device.id == device_id), None)
            if selected is None:
                raise AudioCaptureError(
                    "Das ausgewählte Mikrofon ist nicht mehr verfügbar. Bitte die Mikrofonliste aktualisieren."
                )
            return self._get_input_device(selected)

        default_device = next((device for device in devices if device.is_default), devices[0])
        return self._get_input_device(default_device)

    def _get_loopback_device(self, device: LoopbackDeviceInfo):
        try:
            microphone = sc.get_microphone(device.id, include_loopback=True)
        except Exception as exc:
            raise AudioCaptureError(
                f"Das WASAPI-Loopback-Gerät '{device.name}' konnte nicht geöffnet werden."
            ) from exc

        if not getattr(microphone, "isloopback", False):
            raise AudioCaptureError(
                f"Das ausgewählte Gerät '{device.name}' ist kein WASAPI-Loopback-Gerät."
            )

        self._active_input_name = f"WASAPI Loopback: {device.name}"
        return microphone

    def _get_input_device(self, device: MicrophoneDeviceInfo):
        try:
            microphone = sc.get_microphone(device.id, include_loopback=False)
        except Exception as exc:
            raise AudioCaptureError(
                f"Das Mikrofon '{device.name}' konnte nicht geöffnet werden. Bitte prüfe den Mikrofonzugriff."
            ) from exc

        if getattr(microphone, "isloopback", False):
            raise AudioCaptureError(
                f"Das ausgewählte Gerät '{device.name}' ist ein Loopback-Gerät und kein Mikrofon."
            )

        self._active_input_name = f"Mikrofon: {device.name}"
        return microphone

    def _capture_access_error(self, exc: Exception) -> AudioCaptureError:
        return self._capture_access_error_for_source(self._recording_source, exc)

    @staticmethod
    def _capture_access_error_for_source(source: str, exc: Exception) -> AudioCaptureError:
        if source == RECORDING_SOURCE_APPLICATION_AUDIO:
            return AudioCaptureError(
                "App-Audio-Aufnahme fehlgeschlagen. Bitte prüfe, ob die gewählte App noch läuft und eine Audio-Session hat."
            )
        if source == RECORDING_SOURCE_MICROPHONE:
            return AudioCaptureError(
                "Mikrofonzugriff fehlgeschlagen. Bitte prüfe, ob das Mikrofon verfügbar ist oder von einer anderen App blockiert wird."
            )
        if source == RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE:
            return AudioCaptureError(
                "Kombi-Aufnahme fehlgeschlagen. Bitte prüfe, ob Loopback-Gerät und Mikrofon verfügbar sind."
            )
        return AudioCaptureError(
            "Das ausgewählte WASAPI-Loopback-Gerät konnte nicht für die Aufnahme geöffnet werden."
        )

    @staticmethod
    def _source_label(source: str) -> str:
        if source == RECORDING_SOURCE_APPLICATION_AUDIO:
            return "App-Audio"
        if source == RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE:
            return "Systemaudio + Mikrofon"
        if source == RECORDING_SOURCE_MICROPHONE:
            return "Mikrofon"
        return "Systemaudio"

    @staticmethod
    def _build_file_stem(source: str) -> str:
        if source == RECORDING_SOURCE_APPLICATION_AUDIO:
            prefix = "appaudio"
        elif source == RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE:
            prefix = "systemaudio_microphone"
        elif source == RECORDING_SOURCE_MICROPHONE:
            prefix = "microphone"
        else:
            prefix = "systemaudio"
        return f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"

    @staticmethod
    def _build_channel_map(source_device) -> list[int]:
        channel_count = getattr(source_device, "channels", 2) or 2
        if isinstance(channel_count, int) and channel_count >= 2:
            return [0, 1]
        if isinstance(channel_count, int) and channel_count == 1:
            return [0]
        return [0, 1]

    def _flush_recorder(self, recorder, audio_file) -> None:
        flush = getattr(recorder, "flush", None)
        if flush is None:
            return

        try:
            tail = flush()
        except Exception:
            logger.warning("Recorder-Flush ist fehlgeschlagen", exc_info=True)
            return

        samples = np.asarray(tail, dtype=np.float32)
        if samples.size == 0:
            return

        audio_file.write(np.clip(samples, -1.0, 1.0))
        self._frames_written += int(samples.shape[0])


DualSourceAudioCaptureService = AudioCaptureService


@dataclass(slots=True)
class LoopbackDeviceInfo:
    id: str
    name: str
    is_default: bool = False

    @property
    def display_name(self) -> str:
        if self.is_default:
            return f"{self.name} (Standard)"
        return self.name


class AudioCaptureService:
    def __init__(self, sample_rate: int = 48_000, block_size: int = 2_048) -> None:
        self.sample_rate = sample_rate
        self.block_size = block_size
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frames_written = 0
        self._current_output_path: Path | None = None
        self._started_at: datetime | None = None
        self._last_finished_at: datetime | None = None
        self._error: Exception | None = None
        self._active_input_name = "WASAPI Loopback"
        self._selected_device_id = ""

    @property
    def is_recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def list_loopback_devices(self) -> list[LoopbackDeviceInfo]:
        if sys.platform != "win32":
            return []

        try:
            default_speaker = sc.default_speaker()
            default_id = str(getattr(default_speaker, "id", "") or "")
            microphones = sc.all_microphones(include_loopback=True)
        except Exception as exc:
            logger.exception("WASAPI-Loopback-Geräte konnten nicht geladen werden")
            raise AudioCaptureError(
                "Die verfügbaren WASAPI-Loopback-Geräte konnten nicht gelesen werden."
            ) from exc

        devices: dict[str, LoopbackDeviceInfo] = {}
        for microphone in microphones:
            if not getattr(microphone, "isloopback", False):
                continue
            device_id = str(getattr(microphone, "id", "") or "")
            if not device_id:
                continue
            devices[device_id] = LoopbackDeviceInfo(
                id=device_id,
                name=str(getattr(microphone, "name", "Unbekanntes Loopback-Gerät")),
                is_default=device_id == default_id,
            )

        result = sorted(
            devices.values(),
            key=lambda item: (not item.is_default, item.name.lower()),
        )
        logger.info("Gefundene WASAPI-Loopback-Geräte: %s", [device.name for device in result])
        return result

    def start_recording(self, settings: AppSettings) -> Path:
        if sys.platform != "win32":
            raise AudioCaptureError("WASAPI loopback is only supported on Windows in this build.")

        with self._lock:
            if self.is_recording:
                raise AudioCaptureError("Es läuft bereits eine Aufnahme.")

            file_stem = f"recording_{datetime.now():%Y%m%d_%H%M%S}"
            self._current_output_path = ensure_unique_path(settings.recordings_path(), file_stem, ".wav")
            self._frames_written = 0
            self._error = None
            self._active_input_name = "WASAPI Loopback"
            self._selected_device_id = settings.audio_device_id
            self._started_at = datetime.now()
            self._last_finished_at = None
            self._stop_event.clear()
            logger.info(
                "Starte Systemaudio-Aufnahme nach %s mit Gerät %s",
                self._current_output_path,
                settings.audio_device_id or "<Standard>",
            )
            self._thread = threading.Thread(
                target=self._capture_loop,
                name="LoopScribeRecorder",
                daemon=True,
            )
            self._thread.start()
            return self._current_output_path

    def stop_recording(self) -> RecordingResult:
        with self._lock:
            if not self.is_recording or self._thread is None:
                raise AudioCaptureError("Es läuft derzeit keine Aufnahme.")
            thread = self._thread
            self._stop_event.set()

        thread.join(timeout=10)
        if thread.is_alive():
            raise AudioCaptureError("Die Aufnahme konnte nicht sauber beendet werden.")

        with self._lock:
            self._thread = None
            if self._error is not None:
                raise AudioCaptureError(str(self._error)) from self._error
            if self._current_output_path is None or self._started_at is None:
                raise AudioCaptureError("Die Aufnahmedatei konnte nicht ermittelt werden.")

            finished_at = self._last_finished_at or datetime.now()
            duration = self._frames_written / float(self.sample_rate)
            logger.info(
                "Aufnahme beendet: %s (%.2f Sekunden) über %s",
                self._current_output_path,
                duration,
                self._active_input_name,
            )
            return RecordingResult(
                file_path=self._current_output_path,
                duration_seconds=duration,
                started_at=self._started_at,
                finished_at=finished_at,
                capture_device=self._active_input_name,
            )

    def _capture_loop(self) -> None:
        try:
            loopback_mic = self._resolve_loopback_microphone(self._selected_device_id)
            channel_map = self._build_channel_map(loopback_mic)
            output_path = self._current_output_path
            logger.info(
                "Verwende %s mit Kanalzuordnung %s für die Aufnahme",
                self._active_input_name,
                channel_map,
            )
            if output_path is None:
                raise AudioCaptureError("Kein Zielspeicherort für die Aufnahme vorhanden.")

            try:
                audio_file = sf.SoundFile(
                    output_path,
                    mode="w",
                    samplerate=self.sample_rate,
                    channels=len(channel_map),
                    subtype="PCM_16",
                )
            except OSError as exc:
                raise AudioCaptureError(
                    "Die Aufnahmedatei kann nicht geschrieben werden. Bitte prüfe Schreibrechte oder ob die Datei geöffnet ist."
                ) from exc

            with loopback_mic.recorder(
                samplerate=self.sample_rate,
                channels=channel_map,
                blocksize=self.block_size,
            ) as recorder, audio_file:
                while not self._stop_event.is_set():
                    frames = recorder.record(numframes=self.block_size)
                    samples = np.asarray(frames, dtype=np.float32)
                    if samples.size == 0:
                        continue
                    audio_file.write(np.clip(samples, -1.0, 1.0))
                    self._frames_written += int(samples.shape[0])

                self._flush_recorder(recorder, audio_file)
        except Exception as exc:
            self._error = exc
            logger.exception("Aufnahmefehler beim WASAPI-Loopback")
        finally:
            self._last_finished_at = datetime.now()
            logger.info("Aufnahme-Thread beendet")

    def _resolve_loopback_microphone(self, device_id: str):
        devices = self.list_loopback_devices()
        if not devices:
            raise AudioCaptureError(
                "Kein WASAPI-Loopback-Gerät gefunden. Bitte prüfe, ob ein Wiedergabegerät aktiv ist."
            )

        if device_id:
            selected = next((device for device in devices if device.id == device_id), None)
            if selected is None:
                raise AudioCaptureError(
                    "Das ausgewählte WASAPI-Loopback-Gerät ist nicht mehr verfügbar. Bitte die Geräteliste aktualisieren."
                )
            return self._get_microphone_by_device(selected)

        default_device = next((device for device in devices if device.is_default), devices[0])
        return self._get_microphone_by_device(default_device)

    def _get_microphone_by_device(self, device: LoopbackDeviceInfo):
        try:
            microphone = sc.get_microphone(device.id, include_loopback=True)
        except Exception as exc:
            raise AudioCaptureError(
                f"Das Audiogerät '{device.name}' konnte nicht geöffnet werden."
            ) from exc

        if not getattr(microphone, "isloopback", False):
            raise AudioCaptureError(
                f"Das ausgewählte Gerät '{device.name}' ist kein WASAPI-Loopback-Gerät."
            )

        self._active_input_name = f"WASAPI Loopback: {device.name}"
        return microphone

    @staticmethod
    def _build_channel_map(loopback_mic) -> list[int]:
        channel_count = getattr(loopback_mic, "channels", 2) or 2
        if isinstance(channel_count, int) and channel_count >= 2:
            return [0, 1]
        if isinstance(channel_count, int) and channel_count == 1:
            return [0]
        return [0, 1]

    def _flush_recorder(self, recorder, audio_file) -> None:
        flush = getattr(recorder, "flush", None)
        if flush is None:
            return

        try:
            tail = flush()
        except Exception:
            logger.warning("Recorder-Flush ist fehlgeschlagen", exc_info=True)
            return

        samples = np.asarray(tail, dtype=np.float32)
        if samples.size == 0:
            return

        audio_file.write(np.clip(samples, -1.0, 1.0))
        self._frames_written += int(samples.shape[0])
