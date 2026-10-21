import ast
import json
import sys
from uuid import uuid4
import unittest
from pathlib import Path
from string import Formatter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from recorder_app.config import AppSettings, SettingsStore
from recorder_app.help_text import HELP_SECTIONS
from recorder_app.i18n import UI_LANGUAGES, catalog, translate


class LocalizationTests(unittest.TestCase):
    def test_all_catalogs_have_same_keys_and_preserve_format_fields(self):
        expected_codes = {"de", "en", "ro", "fr", "es", "it", "pt", "nl",
                          "pl", "ru", "uk", "tr", "ar", "zh", "ja", "ko"}
        self.assertEqual(set(UI_LANGUAGES), expected_codes)
        fields = lambda text: sorted((name, spec, conversion) for _, name, spec, conversion
                                     in Formatter().parse(text) if name is not None)
        for code in expected_codes - {"de"}:
            with self.subTest(code=code):
                translations = catalog(code)
                self.assertEqual(translations.keys(), catalog("en").keys())
                for source, target in translations.items():
                    self.assertTrue(target.strip(), source)
                    self.assertEqual(fields(source), fields(target), (code, source))
                for _, paragraph in HELP_SECTIONS:
                    self.assertIn(paragraph, translations)

    def test_ui_literal_translation_coverage(self):
        path = Path(__file__).resolve().parents[1] / "src/recorder_app/ui.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        references = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            index = 1 if name in {"self._set_ui_text", "self._text_widget"} else (
                0 if name in {"self._tr", "add_setting", "self._show_error",
                              "self._set_push_feedback", "self._reset_push_to_transcribe_state"} else -1)
            if index >= 0 and len(node.args) > index:
                arg = node.args[index]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    references.add(arg.value)
        invariant = {"00:00", "{value0}\n{value1}", "{value0}\n{value1}\n{value2}"}
        self.assertEqual(references - catalog("en").keys() - invariant, set())

    def test_dynamic_worker_messages_and_untrusted_values(self):
        self.assertEqual(translate("en", "Fertig ({value0})", value0="ro"), "Done (ro)")
        self.assertEqual(translate("ro", "Transkript gespeichert: meeting.txt"),
                         "Transcriere salvată: meeting.txt")
        self.assertEqual(translate("en", "Transkription läuft (00:12 / 01:00) - Sprache: ro"),
                         "Transcribing (00:12 / 01:00) - Language: ro")
        self.assertEqual(translate("fr", "Transkript gespeichert: {private}.txt"),
                         "Transcription enregistrée : {private}.txt")
        self.assertEqual(translate("../../outside", "Einstellungen"), "Einstellungen")
        self.assertEqual(translate("en", "Unknown native error {detail}"), "Unknown native error {detail}")

    def test_old_settings_and_independent_language_roundtrip(self):
        root = Path(__file__).resolve().parents[1] / "output" / f"i18n-test-{uuid4().hex}"
        root.mkdir(parents=True)
        try:
            defaults = AppSettings(output_directory=str(root / "data"))
            with patch.object(AppSettings, "with_defaults", return_value=defaults):
                store = SettingsStore(root / "settings.json")
                store.path.write_text(json.dumps({"language": "ro", "model_size": "small"}), encoding="utf-8")
                settings = store.load()
                self.assertEqual(settings.ui_language, "de")
                for code in UI_LANGUAGES:
                    settings.ui_language = code
                    store.save(settings)
                    loaded = store.load()
                    self.assertEqual(loaded.ui_language, code)
                    self.assertEqual(loaded.language, "ro")
                    self.assertEqual(loaded.model_size, "small")
                settings.ui_language = "invalid"
                store.save(settings)
                self.assertEqual(store.load().ui_language, "de")
        finally:
            (root / "settings.json").unlink(missing_ok=True)
            for path in (root / "data/recordings", root / "data/transcripts", root / "data", root):
                if path.exists():
                    path.rmdir()


if __name__ == "__main__":
    unittest.main()
