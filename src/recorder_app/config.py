from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path

from platformdirs import user_data_dir

from .i18n import normalize_ui_language

APP_NAME = "scribe"
# Keep the established on-disk identity so existing settings and models remain available.
STORAGE_NAME = "LoopScribe"
APP_AUTHOR = "Recorder"
RECORDING_SOURCE_SYSTEM_AUDIO = "system_audio"
RECORDING_SOURCE_MICROPHONE = "microphone"
RECORDING_SOURCE_SYSTEM_AUDIO_AND_MICROPHONE = "system_audio_microphone"
RECORDING_SOURCE_APPLICATION_AUDIO = "application_audio"
DEFAULT_PUSH_TO_TRANSCRIBE_HOTKEY = "ctrl+alt+space"


def application_state_directory() -> Path:
    root = Path(user_data_dir(STORAGE_NAME, APP_AUTHOR))
    root.mkdir(parents=True, exist_ok=True)
    return root


def default_output_directory() -> Path:
    documents = Path.home() / "Documents"
    base_dir = documents if documents.exists() else Path.home()
    output_dir = base_dir / STORAGE_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def ensure_unique_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


@dataclass(slots=True)
class AppSettings:
    output_directory: str
    start_hotkey: str = "ctrl+alt+r"
    stop_hotkey: str = "ctrl+alt+s"
    push_to_transcribe_hotkey: str = DEFAULT_PUSH_TO_TRANSCRIBE_HOTKEY
    language: str = "auto"
    model_size: str = "base"
    transcription_profile: str = "fast"
    transcript_format: str = "compact"
    show_timestamps: bool = True
    delete_recorded_audio_after_transcription: bool = True
    recording_source: str = RECORDING_SOURCE_SYSTEM_AUDIO
    audio_device_id: str = ""
    microphone_device_id: str = ""
    application_audio_session_id: str = ""
    application_audio_session_label: str = ""
    application_audio_include_microphone: bool = False
    ui_language: str = "de"

    @classmethod
    def with_defaults(cls) -> "AppSettings":
        return cls(output_directory=str(default_output_directory()))

    def output_path(self) -> Path:
        path = Path(self.output_directory).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def recordings_path(self) -> Path:
        path = self.output_path() / "recordings"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def transcripts_path(self) -> Path:
        path = self.output_path() / "transcripts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ensure_directories(self) -> None:
        self.recordings_path()
        self.transcripts_path()


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or application_state_directory() / "settings.json"

    def load(self) -> AppSettings:
        defaults = AppSettings.with_defaults()
        if not self.path.exists():
            defaults.ensure_directories()
            return defaults

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            defaults.ensure_directories()
            return defaults

        merged: dict[str, object] = {
            field.name: getattr(defaults, field.name) for field in fields(AppSettings)
        }
        for key, value in payload.items():
            if key not in merged:
                continue
            default_value = getattr(defaults, key)
            if isinstance(default_value, bool) and isinstance(value, bool):
                merged[key] = value
            elif isinstance(default_value, bool) and isinstance(value, str):
                merged[key] = value.strip().lower() in {"1", "true", "yes", "on"}
            elif isinstance(default_value, str) and isinstance(value, str):
                merged[key] = value

        settings = AppSettings(**merged)
        settings.ui_language = normalize_ui_language(settings.ui_language)
        settings.ensure_directories()
        return settings

    def save(self, settings: AppSettings) -> None:
        settings.ensure_directories()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(asdict(settings), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
