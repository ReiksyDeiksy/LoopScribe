"""Localized help content; German source text is kept alongside translation keys."""

HELP_SECTIONS = (
    ('Hilfe',
     'Audio wird lokal mit faster-whisper transkribiert. Beim ersten Einsatz kann das Modell heruntergeladen werden. Ein größeres Modell kann genauer sein und länger dauern.'),
    ('Systemaudio',
     'Systemaudio nimmt die Windows-Wiedergabe über WASAPI-Loopback auf. Mikrofon nimmt das gewählte Eingabegerät auf. App-Audio benötigt Windows Process Loopback und den mitgelieferten Helper; wähle eine laufende Audio-App. Aktualisiere die Listen, wenn Geräte oder Apps fehlen.'),
    ('Neue Aufnahme',
     'Systemaudio oder App-Audio können mit dem Mikrofon in einer gemeinsamen WAV-Datei aufgenommen werden. Start beginnt die Aufnahme; Stopp startet anschließend automatisch die Transkription. Es kann nur eine Aufnahme oder Transkription gleichzeitig laufen.'),
    ('Aufnahme-Dateien',
     'Aufnahmen liegen unter recordings, automatische TXT-Transkripte unter transcripts im Speicherpfad. Automatisches Löschen entfernt eigene WAV-Aufnahmen nach Transkription, auch bei Fehler oder Abbruch. Importierte Dateien bleiben erhalten. Import unterstützt WAV, MP3, M4A, FLAC, OGG und AAC.'),
    ('Push-to-Transcribe',
     'Start: {start}. Stopp: {stop}. Push-to-Transcribe: {push}. Gleiche Start/Stopp-Tasten schalten um. Zum Diktieren die Push-Tasten halten, ins Mikrofon sprechen und loslassen. Der Text wird im ursprünglichen aktiven Textfeld eingefügt. Bei Fensterwechsel oder Einfügefehler bleibt er für Strg+V in der Zwischenablage.'),
    ('Transkription läuft',
     'Der Fortschritt zeigt den Stand der Transkription. Abbrechen fordert einen Stopp an; der aktuelle Whisper-Block kann noch fertiggestellt werden. Fehler stehen im Hinweis und unter Diagnose. Vor dem Schließen muss die Aufnahme gestoppt oder die Transkription beendet sein.'),
    ('Transkript',
     'Transkripttext kann bearbeitet, vollständig oder als Auswahl kopiert und als TXT, SRT oder VTT exportiert werden. TXT folgt der Zeitstempel-Einstellung. SRT und VTT verwenden gespeicherte Segmentzeiten. Kompakt verbindet Segmente zu Absätzen ohne AI-Umschreibung.'),
    ('Einstellungen',
     'Oberflächensprache und Audio-Erkennungssprache sind unabhängig. Die Oberfläche wechselt sofort; Einstellungen speichern behält Sprache, Pfade, Geräte und Hotkeys dauerhaft bei und registriert Hotkeys neu. Automatisch erkennen bestimmt die gesprochene Sprache.'),
    ('Diagnose',
     'Fehlt ein Gerät, aktualisiere die Liste und wähle ein verfügbares Gerät. Prüfe Mikrofonzugriff und ob andere Apps Geräte oder Hotkeys blockieren. Bei Speicherfehlern prüfe Ordner, Schreibrechte und geöffnete Dateien. Technische Diagnosemeldungen können in ihrer Originalsprache erscheinen.'),
)
