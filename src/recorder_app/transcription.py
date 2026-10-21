from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable

from faster_whisper import WhisperModel
from PySide6.QtCore import QObject, Signal, Slot
import soundfile as sf

from .config import ensure_unique_path

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]

FAST_COMPUTE_TYPE = "int8"
FAST_BEAM_SIZE = 1
FAST_BEST_OF = 1
FAST_CHUNK_LENGTH_SECONDS = 15
FAST_VAD_PARAMETERS = {"min_silence_duration_ms": 700}
MAX_CPU_THREADS = 4
TRANSCRIPTION_PROFILE_FAST = "fast"
TRANSCRIPTION_PROFILE_BALANCED = "balanced"
TRANSCRIPTION_PROFILE_QUALITY = "quality"
TRANSCRIPT_FORMAT_STANDARD = "standard"
TRANSCRIPT_FORMAT_COMPACT = "compact"
COMPACT_PARAGRAPH_GAP_SECONDS = 4.0
COMPACT_PARAGRAPH_SOFT_LIMIT_CHARS = 700
COMPACT_PARAGRAPH_HARD_LIMIT_CHARS = 1200


@dataclass(frozen=True, slots=True)
class TranscriptionPerformanceProfile:
    key: str
    label: str
    compute_type: str
    beam_size: int
    best_of: int
    chunk_length_seconds: int
    vad_filter: bool
    vad_parameters: dict[str, int]
    description: str


TRANSCRIPTION_PERFORMANCE_PROFILES: dict[str, TranscriptionPerformanceProfile] = {
    TRANSCRIPTION_PROFILE_FAST: TranscriptionPerformanceProfile(
        key=TRANSCRIPTION_PROFILE_FAST,
        label="Schnell",
        compute_type="int8",
        beam_size=1,
        best_of=1,
        chunk_length_seconds=15,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 700},
        description="Minimale CPU-Last und kurze Laufzeit; ideal für lange Meetings.",
    ),
    TRANSCRIPTION_PROFILE_BALANCED: TranscriptionPerformanceProfile(
        key=TRANSCRIPTION_PROFILE_BALANCED,
        label="Ausgewogen",
        compute_type="int8",
        beam_size=2,
        best_of=1,
        chunk_length_seconds=20,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 600},
        description="Mehr Suchbreite als Schnell, aber mit reduziertem CPU-Aufwand gegenüber Qualität.",
    ),
    TRANSCRIPTION_PROFILE_QUALITY: TranscriptionPerformanceProfile(
        key=TRANSCRIPTION_PROFILE_QUALITY,
        label="Qualität",
        compute_type="int8",
        beam_size=5,
        best_of=1,
        chunk_length_seconds=30,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        description="Höhere Suchbreite für Genauigkeit; spürbar langsamer und CPU-intensiver.",
    ),
}
TRANSCRIPTION_PROFILE_OPTIONS = {
    profile.label: profile.key for profile in TRANSCRIPTION_PERFORMANCE_PROFILES.values()
}
TRANSCRIPT_FORMAT_OPTIONS = {
    "Kompakt": TRANSCRIPT_FORMAT_COMPACT,
    "Standard": TRANSCRIPT_FORMAT_STANDARD,
}


def resolve_cpu_threads() -> int:
    configured_value = os.environ.get("LOOPSCRIBE_CPU_THREADS", "").strip()
    if configured_value:
        try:
            return max(1, int(configured_value))
        except ValueError:
            logger.warning("Ignoriere ungültigen LOOPSCRIBE_CPU_THREADS-Wert: %s", configured_value)

    cpu_count = os.cpu_count() or MAX_CPU_THREADS
    return max(1, min(MAX_CPU_THREADS, cpu_count - 1))


def resolve_performance_profile(profile_key: str | None) -> TranscriptionPerformanceProfile:
    normalized_key = (profile_key or TRANSCRIPTION_PROFILE_FAST).strip().lower()
    profile = TRANSCRIPTION_PERFORMANCE_PROFILES.get(normalized_key)
    if profile is not None:
        return profile
    logger.warning("Unbekanntes Transkriptionsprofil '%s', nutze Schnell.", profile_key)
    return TRANSCRIPTION_PERFORMANCE_PROFILES[TRANSCRIPTION_PROFILE_FAST]


def describe_performance_profile(model_size: str, profile_key: str | None = None) -> str:
    profile = resolve_performance_profile(profile_key)
    return (
        f"{profile.label}: Modell={model_size}, compute={profile.compute_type}, "
        f"beam={profile.beam_size}, chunk={profile.chunk_length_seconds}s, "
        f"threads={resolve_cpu_threads()}, VAD={'an' if profile.vad_filter else 'aus'}"
    )


@dataclass(slots=True)
class TranscriptionRequest:
    audio_path: Path
    output_directory: Path
    language: str
    model_size: str
    performance_profile: str = TRANSCRIPTION_PROFILE_FAST
    transcript_format: str = TRANSCRIPT_FORMAT_COMPACT
    show_timestamps: bool = True


@dataclass(slots=True)
class TranscriptionResult:
    audio_path: Path
    transcript_path: Path
    transcript_text: str
    timed_transcript_text: str
    detected_language: str
    duration_seconds: float


class TranscriptionCancelled(RuntimeError):
    """Raised when a running transcription is cancelled by the user."""


@dataclass(slots=True)
class TranscriptCue:
    start_seconds: float
    end_seconds: float
    text: str


def format_timestamp(value: float) -> str:
    total_seconds = max(int(value), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def parse_timestamp_string(value: str) -> float:
    parts = [part.strip() for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return (int(minutes) * 60) + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)
    raise ValueError(f"Ungültiger Zeitstempel: {value}")


def parse_transcript_cues(transcript_text: str) -> list[TranscriptCue]:
    cues: list[TranscriptCue] = []
    for raw_line in transcript_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("[") or "] " not in line or " - " not in line:
            raise ValueError(
                "SRT- und VTT-Export benötigen Zeitmarken im Format [mm:ss - mm:ss] Text."
            )

        timestamp_block, text = line[1:].split("] ", maxsplit=1)
        start_text, end_text = timestamp_block.split(" - ", maxsplit=1)
        cues.append(
            TranscriptCue(
                start_seconds=parse_timestamp_string(start_text),
                end_seconds=parse_timestamp_string(end_text),
                text=text.strip(),
            )
        )

    if not cues:
        raise ValueError("Das aktuelle Transkript enthält keine exportierbaren Segmente.")
    return cues


def format_subtitle_timestamp(value: float, decimal_separator: str) -> str:
    total_milliseconds = max(int(round(value * 1000)), 0)
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        f"{decimal_separator}{milliseconds:03d}"
    )


def render_srt(transcript_text: str) -> str:
    blocks: list[str] = []
    for index, cue in enumerate(parse_transcript_cues(transcript_text), start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    (
                        f"{format_subtitle_timestamp(cue.start_seconds, ',')} --> "
                        f"{format_subtitle_timestamp(cue.end_seconds, ',')}"
                    ),
                    cue.text,
                ]
            )
        )
    return "\n\n".join(blocks).strip() + "\n"


def render_vtt(transcript_text: str) -> str:
    blocks = ["WEBVTT"]
    for cue in parse_transcript_cues(transcript_text):
        blocks.append(
            "\n".join(
                [
                    (
                        f"{format_subtitle_timestamp(cue.start_seconds, '.')} --> "
                        f"{format_subtitle_timestamp(cue.end_seconds, '.')}"
                    ),
                    cue.text,
                ]
            )
        )
    return "\n\n".join(blocks).strip() + "\n"


def strip_transcript_timestamps(transcript_text: str) -> str:
    lines: list[str] = []
    for raw_line in transcript_text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and "] " in line:
            lines.append(line.split("] ", maxsplit=1)[1])
        else:
            lines.append(raw_line)
    return "\n".join(lines).strip()


def normalize_transcript_text(text: str) -> str:
    return " ".join(text.replace("\u2028", " ").replace("\u2029", " ").split())


def format_transcript_cues(
    cues: list[TranscriptCue],
    *,
    include_timestamps: bool,
    transcript_format: str = TRANSCRIPT_FORMAT_COMPACT,
) -> str:
    if include_timestamps:
        return "\n".join(
            f"[{format_timestamp(cue.start_seconds)} - {format_timestamp(cue.end_seconds)}] {normalize_transcript_text(cue.text)}"
            for cue in cues
            if normalize_transcript_text(cue.text)
        ).strip()

    normalized_format = (transcript_format or TRANSCRIPT_FORMAT_COMPACT).strip().lower()
    if normalized_format == TRANSCRIPT_FORMAT_COMPACT:
        return render_compact_transcript(cues)

    return "\n".join(
        normalize_transcript_text(cue.text)
        for cue in cues
        if normalize_transcript_text(cue.text)
    ).strip()


def format_transcript_text(
    transcript_text: str,
    *,
    include_timestamps: bool,
    transcript_format: str = TRANSCRIPT_FORMAT_COMPACT,
) -> str:
    if include_timestamps:
        return transcript_text.strip()

    try:
        cues = parse_transcript_cues(transcript_text)
    except ValueError:
        if transcript_format == TRANSCRIPT_FORMAT_COMPACT:
            fallback_cues = [
                TranscriptCue(0.0, 0.0, line)
                for line in strip_transcript_timestamps(transcript_text).splitlines()
                if line.strip()
            ]
            return render_compact_transcript(fallback_cues)
        return strip_transcript_timestamps(transcript_text).strip()

    return format_transcript_cues(
        cues,
        include_timestamps=False,
        transcript_format=transcript_format,
    )


def render_compact_transcript(cues: list[TranscriptCue]) -> str:
    paragraphs: list[str] = []
    current_parts: list[str] = []
    current_chars = 0
    previous_end: float | None = None

    for cue in cues:
        text = normalize_transcript_text(cue.text)
        if not text:
            continue

        gap = 0.0 if previous_end is None else max(0.0, cue.start_seconds - previous_end)
        should_break = bool(current_parts) and (
            gap >= COMPACT_PARAGRAPH_GAP_SECONDS
            or current_chars >= COMPACT_PARAGRAPH_HARD_LIMIT_CHARS
            or (
                current_chars >= COMPACT_PARAGRAPH_SOFT_LIMIT_CHARS
                and _ends_sentence(current_parts[-1])
                and _starts_sentence(text)
            )
        )
        if should_break:
            paragraphs.append(" ".join(current_parts).strip())
            current_parts = []
            current_chars = 0

        current_parts.append(text)
        current_chars += len(text) + 1
        previous_end = cue.end_seconds

    if current_parts:
        paragraphs.append(" ".join(current_parts).strip())

    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph).strip()


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?", "…"))


def _starts_sentence(text: str) -> bool:
    stripped = text.lstrip()
    return bool(stripped) and (not stripped[0].islower())


def build_export_content(
    transcript_text: str,
    suffix: str,
    timed_transcript_text: str | None = None,
    include_timestamps: bool = True,
    transcript_format: str = TRANSCRIPT_FORMAT_COMPACT,
) -> str:
    normalized_suffix = suffix.lower()
    if normalized_suffix == ".txt":
        if include_timestamps:
            return transcript_text.strip() + "\n"
        return format_transcript_text(
            transcript_text,
            include_timestamps=False,
            transcript_format=transcript_format,
        ).strip() + "\n"
    if normalized_suffix == ".srt":
        return render_srt(timed_transcript_text or transcript_text)
    if normalized_suffix == ".vtt":
        return render_vtt(timed_transcript_text or transcript_text)
    raise ValueError(f"Nicht unterstütztes Exportformat: {suffix}")


class TranscriptionService:
    def __init__(self, model_root: Path | None = None) -> None:
        self.model_root = model_root
        self._model_cache: dict[str, WhisperModel] = {}
        self._model_lock = threading.Lock()

    def transcribe(
        self,
        request: TranscriptionRequest,
        on_progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> TranscriptionResult:
        logger.info(
            "Starte Transkription für %s mit Modell=%s Sprache=%s",
            request.audio_path,
            request.model_size,
            request.language,
        )
        self._raise_if_cancelled(is_cancelled)
        estimated_duration = self._estimate_duration(request.audio_path)
        self._report(on_progress, 2, "Transkriptionsjob wurde in den Hintergrund verschoben ...")
        self._report(on_progress, 8, "Whisper-Modell wird geladen ...")
        model = self._get_model(request.model_size)

        self._raise_if_cancelled(is_cancelled)
        language = None if request.language == "auto" else request.language
        self._report(on_progress, 15, "Audio wird für die Transkription vorbereitet ...")
        segments, info = model.transcribe(
            str(request.audio_path),
            language=language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )

        self._raise_if_cancelled(is_cancelled)
        detected_language = getattr(info, "language", request.language or "auto")
        duration = float(
            getattr(info, "duration_after_vad", 0.0)
            or getattr(info, "duration", 0.0)
            or estimated_duration
            or 0.0
        )
        self._report(
            on_progress,
            25,
            f"Transkription läuft ({detected_language or 'unbekannte Sprache'}) ...",
        )

        transcript_lines: list[str] = []
        last_progress = 25
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            transcript_lines.append(
                f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}] {text}"
            )

            if duration > 0:
                progress = 25 + int((min(segment.end, duration) / duration) * 70)
                if progress > last_progress:
                    last_progress = min(progress, 95)
                    self._report(
                        on_progress,
                        last_progress,
                        "Segmente werden zusammengeführt ...",
                    )

        transcript_text = "\n".join(transcript_lines).strip()
        if not transcript_text:
            transcript_text = "(Keine Sprache erkannt.)"

        transcript_path = self._write_transcript(
            request.output_directory,
            request.audio_path,
            transcript_text,
        )
        logger.info("Transkription gespeichert unter %s", transcript_path)
        self._report(on_progress, 100, f"Transkript gespeichert: {transcript_path.name}")
        return TranscriptionResult(
            audio_path=request.audio_path,
            transcript_path=transcript_path,
            transcript_text=transcript_text,
            detected_language=detected_language or "auto",
            duration_seconds=duration,
        )

    @staticmethod
    def _report(
        callback: ProgressCallback | None,
        value: int,
        message: str,
    ) -> None:
        if callback is not None:
            callback(value, message)

    @staticmethod
    def _write_transcript(output_directory: Path, audio_path: Path, transcript_text: str) -> Path:
        transcript_path = ensure_unique_path(output_directory / "transcripts", audio_path.stem, ".txt")
        transcript_path.write_text(transcript_text, encoding="utf-8")
        return transcript_path


class TranscriptionWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, service: TranscriptionService, request: TranscriptionRequest) -> None:
        super().__init__()
        self._service = service
        self._request = request

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.transcribe(self._request, on_progress=self.progress.emit)
        except Exception as exc:
            logger.exception("Transkription fehlgeschlagen")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)

class TranscriptionService(TranscriptionService):
    def _get_model(self, model_size: str) -> WhisperModel:
        with self._model_lock:
            cached_model = self._model_cache.get(model_size)
            if cached_model is not None:
                logger.info("Nutze bereits geladenes Whisper-Modell %s", model_size)
                return cached_model

            logger.info("Lade Whisper-Modell %s neu", model_size)
            try:
                model = WhisperModel(
                    model_size,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(self.model_root) if self.model_root is not None else None,
                )
            except Exception as exc:
                raise RuntimeError(describe_model_error(exc)) from exc

            self._model_cache[model_size] = model
            return model

    @staticmethod
    def _write_transcript(output_directory: Path, audio_path: Path, transcript_text: str) -> Path:
        transcript_path = ensure_unique_path(output_directory / "transcripts", audio_path.stem, ".txt")
        try:
            transcript_path.write_text(transcript_text, encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(describe_transcript_write_error(transcript_path)) from exc
        return transcript_path


def describe_model_error(exc: Exception) -> str:
    message = str(exc).lower()
    if any(token in message for token in ["download", "connection", "network", "ssl", "huggingface", "timeout", "xet"]):
        return (
            "Das Whisper-Modell konnte nicht heruntergeladen oder geladen werden. "
            "Bitte prüfe Internetverbindung, Modellname und Schreibrechte im Modellordner."
        )
    return f"Das Whisper-Modell konnte nicht geladen werden: {exc}"


def describe_transcript_write_error(path: Path) -> str:
    return (
        "Die Transkriptdatei konnte nicht gespeichert werden. "
        f"Bitte prüfe Schreibrechte oder ob die Datei gesperrt ist: {path}"
    )


class TranscriptionService(TranscriptionService):
    def _get_model(self, model_size: str) -> WhisperModel:
        with self._model_lock:
            cached_model = self._model_cache.get(model_size)
            if cached_model is not None:
                logger.info("Nutze bereits geladenes Whisper-Modell %s", model_size)
                return cached_model

            logger.info("Lade Whisper-Modell %s neu", model_size)
            try:
                model = WhisperModel(
                    model_size,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(self.model_root) if self.model_root is not None else None,
                )
            except Exception as exc:
                raise RuntimeError(describe_model_error(exc)) from exc

            self._model_cache[model_size] = model
            return model

    @staticmethod
    def _write_transcript(output_directory: Path, audio_path: Path, transcript_text: str) -> Path:
        transcript_path = ensure_unique_path(output_directory / "transcripts", audio_path.stem, ".txt")
        try:
            transcript_path.write_text(transcript_text, encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(describe_transcript_write_error(transcript_path)) from exc
        return transcript_path


class TranscriptionService:
    def __init__(self, model_root: Path | None = None) -> None:
        self.model_root = model_root
        self._model_cache: dict[str, WhisperModel] = {}
        self._model_lock = threading.Lock()

    def transcribe(
        self,
        request: TranscriptionRequest,
        on_progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> TranscriptionResult:
        logger.info(
            "Starte Transkription für %s mit Modell=%s Sprache=%s",
            request.audio_path,
            request.model_size,
            request.language,
        )
        self._raise_if_cancelled(is_cancelled)
        estimated_duration = self._estimate_duration(request.audio_path)
        self._report(on_progress, 2, "Transkriptionsjob wurde in den Hintergrund verschoben ...")
        self._report(on_progress, 8, "Whisper-Modell wird geladen ...")
        model = self._get_model(request.model_size)

        self._raise_if_cancelled(is_cancelled)
        language = None if request.language == "auto" else request.language
        self._report(on_progress, 15, "Audio wird für die Transkription vorbereitet ...")
        segments, info = model.transcribe(
            str(request.audio_path),
            language=language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )

        self._raise_if_cancelled(is_cancelled)
        detected_language = getattr(info, "language", request.language or "auto")
        duration = float(
            getattr(info, "duration_after_vad", 0.0)
            or getattr(info, "duration", 0.0)
            or estimated_duration
            or 0.0
        )
        self._report(
            on_progress,
            22,
            self._progress_message(0.0, duration, detected_language),
        )

        transcript_lines: list[str] = []
        last_progress = 22
        last_reported_second = -1
        for segment in segments:
            self._raise_if_cancelled(is_cancelled)
            text = segment.text.strip()
            if not text:
                continue
            transcript_lines.append(
                f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}] {text}"
            )

            if duration > 0:
                processed = min(segment.end, duration)
                progress = 22 + int((processed / duration) * 74)
                processed_second = int(processed)
                progress = min(progress, 96)
                if progress > last_progress or processed_second > last_reported_second:
                    last_progress = max(last_progress, progress)
                    last_reported_second = processed_second
                    self._report(
                        on_progress,
                        last_progress,
                        self._progress_message(processed, duration, detected_language),
                    )

        self._raise_if_cancelled(is_cancelled)
        transcript_text = "\n".join(transcript_lines).strip()
        if not transcript_text:
            transcript_text = "(Keine Sprache erkannt.)"

        self._report(on_progress, 98, "Transkript wird gespeichert ...")
        transcript_path = self._write_transcript(
            request.output_directory,
            request.audio_path,
            transcript_text,
        )
        logger.info("Transkription gespeichert unter %s", transcript_path)
        self._report(on_progress, 100, f"Transkript gespeichert: {transcript_path.name}")
        return TranscriptionResult(
            audio_path=request.audio_path,
            transcript_path=transcript_path,
            transcript_text=transcript_text,
            detected_language=detected_language or "auto",
            duration_seconds=duration,
        )

    def _get_model(self, model_size: str) -> WhisperModel:
        with self._model_lock:
            cached_model = self._model_cache.get(model_size)
            if cached_model is not None:
                logger.info("Nutze bereits geladenes Whisper-Modell %s", model_size)
                return cached_model

            logger.info("Lade Whisper-Modell %s neu", model_size)
            model = WhisperModel(
                model_size,
                device="cpu",
                compute_type="int8",
                download_root=str(self.model_root) if self.model_root is not None else None,
            )
            self._model_cache[model_size] = model
            return model

    @staticmethod
    def _report(
        callback: ProgressCallback | None,
        value: int,
        message: str,
    ) -> None:
        if callback is not None:
            callback(value, message)

    @staticmethod
    def _write_transcript(output_directory: Path, audio_path: Path, transcript_text: str) -> Path:
        transcript_path = ensure_unique_path(output_directory / "transcripts", audio_path.stem, ".txt")
        transcript_path.write_text(transcript_text, encoding="utf-8")
        return transcript_path

    @staticmethod
    def _estimate_duration(audio_path: Path) -> float:
        try:
            return float(sf.info(str(audio_path)).duration or 0.0)
        except Exception:
            logger.debug("Audio-Dauer konnte nicht vorab gelesen werden", exc_info=True)
            return 0.0

    @staticmethod
    def _progress_message(processed: float, duration: float, language: str | None) -> str:
        if duration > 0:
            return (
                f"Transkription läuft ({format_timestamp(processed)} / "
                f"{format_timestamp(duration)}) - Sprache: {language or 'unbekannt'}"
            )
        return f"Transkription läuft - Sprache: {language or 'unbekannt'}"

    @staticmethod
    def _raise_if_cancelled(is_cancelled: CancelCallback | None) -> None:
        if is_cancelled is not None and is_cancelled():
            raise TranscriptionCancelled("Transkription wurde abgebrochen.")


class TranscriptionWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(self, service: TranscriptionService, request: TranscriptionRequest) -> None:
        super().__init__()
        self._service = service
        self._request = request
        self._cancel_event = threading.Event()

    @Slot()
    def cancel(self) -> None:
        self._cancel_event.set()
        self.progress.emit(0, "Abbruch wird angefordert ...")

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.transcribe(
                self._request,
                on_progress=self.progress.emit,
                is_cancelled=self._cancel_event.is_set,
            )
        except TranscriptionCancelled as exc:
            logger.info("Transkription abgebrochen")
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:
            logger.exception("Transkription fehlgeschlagen")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


FastTranscriptionService = TranscriptionService
FastTranscriptionWorker = TranscriptionWorker


class TranscriptionService:
    def __init__(self, model_root: Path | None = None) -> None:
        self.model_root = model_root
        self._model_cache: dict[str, WhisperModel] = {}
        self._model_lock = threading.Lock()

    def transcribe(
        self,
        request: TranscriptionRequest,
        on_progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> TranscriptionResult:
        total_started_at = time.perf_counter()
        profile = resolve_performance_profile(request.performance_profile)
        performance_profile = describe_performance_profile(request.model_size, profile.key)
        logger.info(
            "Starte Transkription für %s mit Modell=%s Sprache=%s timestamps=%s textformat=%s performance=%s tradeoff=%s",
            request.audio_path,
            request.model_size,
            request.language,
            request.show_timestamps,
            request.transcript_format,
            performance_profile,
            profile.description,
        )
        self._raise_if_cancelled(is_cancelled)

        estimate_started_at = time.perf_counter()
        estimated_duration = self._estimate_duration(request.audio_path)
        estimate_seconds = time.perf_counter() - estimate_started_at
        self._report(on_progress, 2, "Transkriptionsjob wurde in den Hintergrund verschoben ...")
        self._report(on_progress, 4, f"Performance-Modus aktiv: {performance_profile}")
        self._report(on_progress, 8, "Whisper-Modell wird geladen ...")
        model_started_at = time.perf_counter()
        model = self._get_model(request.model_size, profile)
        model_seconds = time.perf_counter() - model_started_at

        self._raise_if_cancelled(is_cancelled)
        language = None if request.language == "auto" else request.language
        self._report(on_progress, 15, f"Audio wird mit Profil {profile.label} vorbereitet ...")
        transcribe_started_at = time.perf_counter()
        segments, info = model.transcribe(
            str(request.audio_path),
            language=language,
            beam_size=profile.beam_size,
            best_of=profile.best_of,
            temperature=0.0,
            vad_filter=profile.vad_filter,
            vad_parameters=dict(profile.vad_parameters),
            condition_on_previous_text=False,
            chunk_length=profile.chunk_length_seconds,
        )
        transcribe_setup_seconds = time.perf_counter() - transcribe_started_at

        self._raise_if_cancelled(is_cancelled)
        detected_language = getattr(info, "language", request.language or "auto")
        duration = float(
            getattr(info, "duration_after_vad", 0.0)
            or getattr(info, "duration", 0.0)
            or estimated_duration
            or 0.0
        )
        self._report(on_progress, 22, self._progress_message(0.0, duration, detected_language))

        cues: list[TranscriptCue] = []
        last_progress = 22
        last_reported_second = -1
        segment_count = 0
        decode_started_at = time.perf_counter()
        for segment in segments:
            self._raise_if_cancelled(is_cancelled)
            text = normalize_transcript_text(segment.text)
            if not text:
                continue
            segment_count += 1

            cues.append(
                TranscriptCue(
                    start_seconds=float(segment.start),
                    end_seconds=float(segment.end),
                    text=text,
                )
            )

            self._raise_if_cancelled(is_cancelled)
            if duration > 0:
                processed = min(segment.end, duration)
                progress = 22 + int((processed / duration) * 74)
                processed_second = int(processed)
                progress = min(progress, 96)
                if progress > last_progress or processed_second > last_reported_second:
                    last_progress = max(last_progress, progress)
                    last_reported_second = processed_second
                    self._report(
                        on_progress,
                        last_progress,
                        self._progress_message(processed, duration, detected_language),
                    )
        decode_seconds = time.perf_counter() - decode_started_at

        self._raise_if_cancelled(is_cancelled)
        timed_transcript_text = format_transcript_cues(cues, include_timestamps=True)
        transcript_text = format_transcript_cues(
            cues,
            include_timestamps=request.show_timestamps,
            transcript_format=request.transcript_format,
        )
        if not transcript_text:
            transcript_text = "(Keine Sprache erkannt.)"
            timed_transcript_text = transcript_text

        self._report(on_progress, 98, "Transkript wird gespeichert ...")
        write_started_at = time.perf_counter()
        transcript_path = self._write_transcript(
            request.output_directory,
            request.audio_path,
            transcript_text,
        )
        write_seconds = time.perf_counter() - write_started_at
        total_seconds = time.perf_counter() - total_started_at
        realtime_factor = (total_seconds / estimated_duration) if estimated_duration > 0 else 0.0
        logger.info("Transkription gespeichert unter %s", transcript_path)
        logger.info(
            "Transkriptionsprofil-Messung: profil=%s modell=%s audio_dauer=%.2fs "
            "estimate=%.2fs model=%.2fs setup=%.2fs decode=%.2fs write=%.2fs total=%.2fs rtf=%.3f segmente=%s",
            profile.label,
            request.model_size,
            estimated_duration,
            estimate_seconds,
            model_seconds,
            transcribe_setup_seconds,
            decode_seconds,
            write_seconds,
            total_seconds,
            realtime_factor,
            segment_count,
        )
        self._report(on_progress, 100, f"Transkript gespeichert: {transcript_path.name}")
        return TranscriptionResult(
            audio_path=request.audio_path,
            transcript_path=transcript_path,
            transcript_text=transcript_text,
            timed_transcript_text=timed_transcript_text,
            detected_language=detected_language or "auto",
            duration_seconds=duration,
        )

    def _get_model(self, model_size: str, profile: TranscriptionPerformanceProfile) -> WhisperModel:
        with self._model_lock:
            cpu_threads = resolve_cpu_threads()
            cache_key = f"{model_size}|{profile.compute_type}|threads={cpu_threads}"
            cached_model = self._model_cache.get(cache_key)
            if cached_model is not None:
                logger.info("Nutze bereits geladenes Whisper-Modell %s (%s)", model_size, cache_key)
                return cached_model

            logger.info(
                "Lade Whisper-Modell %s neu mit compute_type=%s cpu_threads=%s num_workers=1",
                model_size,
                profile.compute_type,
                cpu_threads,
            )
            try:
                # Installed builds include base; development and other models keep downloading normally.
                bundled_base = Path(__file__).resolve().parent / "bundled_models" / "base"
                model_source = (
                    str(bundled_base)
                    if model_size == "base" and (bundled_base / "model.bin").is_file()
                    else model_size
                )
                model = WhisperModel(
                    model_source,
                    device="cpu",
                    compute_type=profile.compute_type,
                    cpu_threads=cpu_threads,
                    num_workers=1,
                    download_root=str(self.model_root) if self.model_root is not None else None,
                )
            except Exception as exc:
                raise RuntimeError(describe_model_error(exc)) from exc

            self._model_cache[cache_key] = model
            return model

    @staticmethod
    def _report(callback: ProgressCallback | None, value: int, message: str) -> None:
        if callback is not None:
            callback(value, message)

    @staticmethod
    def _write_transcript(output_directory: Path, audio_path: Path, transcript_text: str) -> Path:
        transcript_path = ensure_unique_path(output_directory / "transcripts", audio_path.stem, ".txt")
        try:
            transcript_path.write_text(transcript_text, encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(describe_transcript_write_error(transcript_path)) from exc
        return transcript_path

    @staticmethod
    def _estimate_duration(audio_path: Path) -> float:
        try:
            return float(sf.info(str(audio_path)).duration or 0.0)
        except Exception:
            logger.debug("Audio-Dauer konnte nicht vorab gelesen werden", exc_info=True)
            return 0.0

    @staticmethod
    def _progress_message(processed: float, duration: float, language: str | None) -> str:
        if duration > 0:
            return (
                f"Transkription läuft ({format_timestamp(processed)} / "
                f"{format_timestamp(duration)}) - Sprache: {language or 'unbekannt'}"
            )
        return f"Transkription läuft - Sprache: {language or 'unbekannt'}"

    @staticmethod
    def _raise_if_cancelled(is_cancelled: CancelCallback | None) -> None:
        if is_cancelled is not None and is_cancelled():
            raise TranscriptionCancelled("Transkription wurde abgebrochen.")


class TranscriptionWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(self, service: TranscriptionService, request: TranscriptionRequest) -> None:
        super().__init__()
        self._service = service
        self._request = request
        self._cancel_event = threading.Event()

    @Slot()
    def cancel(self) -> None:
        self._cancel_event.set()
        self.progress.emit(0, "Abbruch angefordert. Aktueller Whisper-Chunk wird beendet ...")

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.transcribe(
                self._request,
                on_progress=self.progress.emit,
                is_cancelled=self._cancel_event.is_set,
            )
        except TranscriptionCancelled as exc:
            logger.info("Transkription abgebrochen")
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:
            logger.exception("Transkription fehlgeschlagen")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


FastTranscriptionService = TranscriptionService
FastTranscriptionWorker = TranscriptionWorker


class TranscriptionService:
    def __init__(self, model_root: Path | None = None) -> None:
        self.model_root = model_root
        self._model_cache: dict[str, WhisperModel] = {}
        self._model_lock = threading.Lock()

    def transcribe(
        self,
        request: TranscriptionRequest,
        on_progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> TranscriptionResult:
        logger.info(
            "Starte Transkription für %s mit Modell=%s Sprache=%s",
            request.audio_path,
            request.model_size,
            request.language,
        )
        self._raise_if_cancelled(is_cancelled)

        estimated_duration = self._estimate_duration(request.audio_path)
        self._report(on_progress, 2, "Transkriptionsjob wurde in den Hintergrund verschoben ...")
        self._report(on_progress, 8, "Whisper-Modell wird geladen ...")
        model = self._get_model(request.model_size)

        self._raise_if_cancelled(is_cancelled)
        language = None if request.language == "auto" else request.language
        self._report(on_progress, 15, "Audio wird für die Transkription vorbereitet ...")
        segments, info = model.transcribe(
            str(request.audio_path),
            language=language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )

        self._raise_if_cancelled(is_cancelled)
        detected_language = getattr(info, "language", request.language or "auto")
        duration = float(
            getattr(info, "duration_after_vad", 0.0)
            or getattr(info, "duration", 0.0)
            or estimated_duration
            or 0.0
        )
        self._report(
            on_progress,
            22,
            self._progress_message(0.0, duration, detected_language),
        )

        transcript_lines: list[str] = []
        last_progress = 22
        last_reported_second = -1
        for segment in segments:
            self._raise_if_cancelled(is_cancelled)
            text = segment.text.strip()
            if not text:
                continue

            transcript_lines.append(
                f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}] {text}"
            )

            if duration > 0:
                processed = min(segment.end, duration)
                progress = 22 + int((processed / duration) * 74)
                processed_second = int(processed)
                progress = min(progress, 96)
                if progress > last_progress or processed_second > last_reported_second:
                    last_progress = max(last_progress, progress)
                    last_reported_second = processed_second
                    self._report(
                        on_progress,
                        last_progress,
                        self._progress_message(processed, duration, detected_language),
                    )

        self._raise_if_cancelled(is_cancelled)
        transcript_text = "\n".join(transcript_lines).strip()
        if not transcript_text:
            transcript_text = "(Keine Sprache erkannt.)"

        self._report(on_progress, 98, "Transkript wird gespeichert ...")
        transcript_path = self._write_transcript(
            request.output_directory,
            request.audio_path,
            transcript_text,
        )
        logger.info("Transkription gespeichert unter %s", transcript_path)
        self._report(on_progress, 100, f"Transkript gespeichert: {transcript_path.name}")
        return TranscriptionResult(
            audio_path=request.audio_path,
            transcript_path=transcript_path,
            transcript_text=transcript_text,
            detected_language=detected_language or "auto",
            duration_seconds=duration,
        )

    def _get_model(self, model_size: str) -> WhisperModel:
        with self._model_lock:
            cached_model = self._model_cache.get(model_size)
            if cached_model is not None:
                logger.info("Nutze bereits geladenes Whisper-Modell %s", model_size)
                return cached_model

            logger.info("Lade Whisper-Modell %s neu", model_size)
            try:
                model = WhisperModel(
                    model_size,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(self.model_root) if self.model_root is not None else None,
                )
            except Exception as exc:
                raise RuntimeError(describe_model_error(exc)) from exc

            self._model_cache[model_size] = model
            return model

    @staticmethod
    def _report(callback: ProgressCallback | None, value: int, message: str) -> None:
        if callback is not None:
            callback(value, message)

    @staticmethod
    def _write_transcript(output_directory: Path, audio_path: Path, transcript_text: str) -> Path:
        transcript_path = ensure_unique_path(output_directory / "transcripts", audio_path.stem, ".txt")
        try:
            transcript_path.write_text(transcript_text, encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(describe_transcript_write_error(transcript_path)) from exc
        return transcript_path

    @staticmethod
    def _estimate_duration(audio_path: Path) -> float:
        try:
            return float(sf.info(str(audio_path)).duration or 0.0)
        except Exception:
            logger.debug("Audio-Dauer konnte nicht vorab gelesen werden", exc_info=True)
            return 0.0

    @staticmethod
    def _progress_message(processed: float, duration: float, language: str | None) -> str:
        if duration > 0:
            return (
                f"Transkription läuft ({format_timestamp(processed)} / "
                f"{format_timestamp(duration)}) - Sprache: {language or 'unbekannt'}"
            )
        return f"Transkription läuft - Sprache: {language or 'unbekannt'}"

    @staticmethod
    def _raise_if_cancelled(is_cancelled: CancelCallback | None) -> None:
        if is_cancelled is not None and is_cancelled():
            raise TranscriptionCancelled("Transkription wurde abgebrochen.")


class TranscriptionWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(self, service: TranscriptionService, request: TranscriptionRequest) -> None:
        super().__init__()
        self._service = service
        self._request = request
        self._cancel_event = threading.Event()

    @Slot()
    def cancel(self) -> None:
        self._cancel_event.set()
        self.progress.emit(0, "Abbruch wird angefordert ...")

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.transcribe(
                self._request,
                on_progress=self.progress.emit,
                is_cancelled=self._cancel_event.is_set,
            )
        except TranscriptionCancelled as exc:
            logger.info("Transkription abgebrochen")
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:
            logger.exception("Transkription fehlgeschlagen")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


# Keep direct imports on the same optimized implementation used by the GUI.
TranscriptionService = FastTranscriptionService
TranscriptionWorker = FastTranscriptionWorker
