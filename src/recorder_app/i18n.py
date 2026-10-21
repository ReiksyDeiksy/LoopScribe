"""Local UI catalogs. Language codes are stable settings values, never paths."""

from functools import lru_cache
from dataclasses import dataclass
import json
from pathlib import Path
import re
from string import Formatter


UI_LANGUAGES = {
    "de": "Deutsch", "en": "English", "ro": "Română",
    "fr": "Français", "es": "Español", "it": "Italiano",
    "pt": "Português", "nl": "Nederlands", "pl": "Polski",
    "ru": "Русский", "uk": "Українська", "tr": "Türkçe",
    "ar": "العربية", "zh": "中文", "ja": "日本語", "ko": "한국어",
}


@dataclass(frozen=True)
class UiText:
    """A nested UI label that must follow later language changes."""

    source: str


def normalize_ui_language(code: str) -> str:
    return code if code in UI_LANGUAGES else "de"


@lru_cache(maxsize=len(UI_LANGUAGES))
def catalog(code: str) -> dict[str, str]:
    code = normalize_ui_language(code)
    if code == "de":
        return {}
    path = Path(__file__).parent / "locales" / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def translate(code: str, source: str, **values: object) -> str:
    values = {name: translate(code, value.source) if isinstance(value, UiText) else value
              for name, value in values.items()}
    if normalize_ui_language(code) == "de":
        return source.format(**values) if values else source
    translations = catalog(code)
    template = translations.get(source)
    if template is None and not values:
        # Worker signals and device errors are plain strings. Match only complete,
        # known message templates; user transcripts never pass through this path.
        for pattern, key in message_patterns():
            match = pattern.fullmatch(source)
            if match and key in translations:
                return translations[key].format(**match.groupdict())
    if template is None:
        template = source
    return template.format(**values) if values else template


@lru_cache(maxsize=1)
def message_patterns():
    patterns = []
    for source in catalog("en"):
        pieces = list(Formatter().parse(source))
        # Ignore templates containing only values, numeric format specs or repeats.
        names = [name for _, name, _, _ in pieces if name is not None]
        if not names or len(set(names)) != len(names):
            continue
        if not any(any(char.isalpha() for char in literal) for literal, _, _, _ in pieces):
            continue
        if any(spec or conversion for _, _, spec, conversion in pieces):
            continue
        expression = ''.join(re.escape(literal) + (f'(?P<{name}>.*?)' if name else '')
                             for literal, name, _, _ in pieces)
        patterns.append((re.compile(expression, re.DOTALL), source))
    return tuple(patterns)
