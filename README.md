# scribe

LoopScribe ist eine lokale Windows-Desktop-Anwendung zur Transkription von Systemaudio. Die App nimmt per WASAPI-Loopback den Ton der Standardausgabe auf, speichert die Aufnahme als WAV-Datei und transkribiert sie anschließend lokal mit `faster-whisper`.

## Funktionsumfang

- Aufnahme von Systemaudio statt nur Mikrofon
- Globale Start-/Stopp-Hotkeys
- Automatische Speicherung von Aufnahmen als `.wav`
- Automatische Transkription nach dem Stoppen der Aufnahme
- Import bestehender Audiodateien wie `.wav`, `.mp3`, `.m4a`, `.flac` und `.ogg`
- Fortschrittsanzeige während der Transkription
- Getrennte Oberflächen- und Audio-Erkennungssprache: Deutsch, Englisch, Rumänisch,
  Französisch, Spanisch, Italienisch, Portugiesisch, Niederländisch, Polnisch,
  Russisch, Ukrainisch, Türkisch, Arabisch, Chinesisch, Japanisch und Koreanisch.
  Für Audio steht zusätzlich automatische Spracherkennung zur Verfügung.
- Export des Transkripts als `.txt`
- Einstellungsbereich für Hotkeys, Modellauswahl und Speicherpfad

## Architektur

Die Codebasis ist in klar getrennte Module aufgeteilt:

- `config.py`: Persistente App-Einstellungen und Pfadverwaltung
- `audio.py`: Aufnahme von Systemaudio via WASAPI-Loopback
- `hotkeys.py`: Registrierung globaler Hotkeys
- `transcription.py`: Whisper-Transkriptionslogik und Worker für die GUI
- `ui.py`: PySide6-Oberfläche
- `main.py`: Einstiegspunkt

## Voraussetzungen

- Windows 10 oder Windows 11
- Python 3.10+
- Eine funktionierende Standard-Audioausgabe

## Installation

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

## Starten

```powershell
python -m recorder_app.main
```

Alternativ nach der Installation:

```powershell
loopscribe
```

## Hinweise

- Das erste Transkript lädt das gewählte Whisper-Modell einmalig lokal herunter.
- Standardmäßig wird `faster-whisper` auf der CPU mit `int8` ausgeführt, damit die App ohne CUDA-Zwang stabil lokal läuft.
- Die Aufnahmen landen unter dem gewählten Speicherpfad in `recordings/`, die automatisch erzeugten Transkripte in `transcripts/`.
- Auf Windows empfiehlt sich Stereo-Loopback, da `soundcard` bei WASAPI mit Einzelkanal-Aufnahmen bekannte Einschränkungen hat.

## Troubleshooting: Discord-Browser-Calls und Mikrofonpegel

LoopScribe verändert die Windows-Mikrofonlautstärke nicht aktiv. Beim App-Audio-Modus öffnet LoopScribe keine Mikrofonaufnahme, sondern nutzt den nativen Windows Process-Loopback-Helper für die ausgewählte App-Session. Zum Eingrenzen von Discord-/Browser-Problemen protokolliert LoopScribe beim Start und Stop der App-Audio-Aufnahme den Standard-Playback-Endpunkt sowie den Standard-Mikrofon-Endpunkt inklusive Master-Volume und Mute-State. Wenn sich der Mikrofonzustand während der App-Audio-Aufnahme ändert, schreibt LoopScribe eine Warnung ins Log.

Wenn der Mikrofonpegel in einem Discord-Browser-Call sinkt, prüfe zusätzlich Discord/WebRTC-Automatic-Gain-Control im Browser, Windows-Kommunikations-Ducking unter **Sound > Weitere Soundeinstellungen > Kommunikation** und herstellerspezifische Audio-Enhancements wie Noise Suppression oder Echo Cancellation. Ein Task-Manager-Force-Kill kann normalen Cleanup nicht garantieren; der App-Audio-Helper schreibt deshalb keine Mikrofon- oder Endpoint-Lautstärke, verwendet eine eigene WASAPI-Session-GUID und versucht eine nicht-persistente WASAPI-Session, sofern Windows dieses Stream-Flag für Process-Loopback akzeptiert.

## Optional erweiterbar

Die Architektur ist vorbereitet, um später Live-Transkription oder Speaker-Diarization zu ergänzen. Diese beiden Punkte sind in der aktuellen Version bewusst noch nicht aktiviert, damit die Basis stabil und lokal nutzbar bleibt.

### App-Audio mit eigenem Mikrofon

Als Aufnahmequelle **App-Audio** und die App (z. B. Discord) auswählen.
**Mikrofon mit aufnehmen** aktivieren und das passende Mikrofon-Gerät wählen.
Beim Stoppen werden beide Quellen in eine gemeinsame WAV-Datei gemischt und
anschließend gemeinsam transkribiert. Die Option ist standardmäßig aus.
Die bestehende Einstellung zum Löschen der Aufnahme nach Ende der
Transkription gilt auch für diese Datei, einschließlich Abbruch und Fehlern.
Importierte Audiodateien bleiben erhalten.

### Dark-Theme-Oberfläche

Im Arbeitsbereich stehen Aufnahmequelle, Geräte und Mikrofon-Schalter direkt
über dem Transkript. Systemaudio plus Mikrofon wird über den Mikrofon-Schalter
bei Systemaudio gewählt. Während der Aufnahme ersetzt Stop den Startknopf.
Speicherpfad, Modell, Sprache und Hotkeys befinden sich unter Einstellungen.
Laufzeitmeldungen lassen sich unten über Diagnose einblenden.

Die App heißt jetzt **scribe**. Das Icon zeigt eine Füllerfeder mit Audiospur.
Bestehende LoopScribe-Speicher- und Konfigurationsordner bleiben kompatibel.

### Direkt in andere Programme diktieren

Scribe öffnen und im Zielprogramm ein Textfeld anklicken. **Strg+Alt+Leertaste
halten**, ins Mikrofon sprechen und die Tasten loslassen. Scribe transkribiert
lokal und fügt den Text mit Strg+V in das ursprüngliche, weiterhin aktive Feld
ein. Push-to-Transcribe nutzt immer das gewählte Mikrofon; die Aufnahmequelle
für normale Aufnahmen bleibt separat eingestellt. Der Hotkey ist anpassbar.

Eine kleine schwebende Leiste zeigt Bereitschaft, Aufnahmepegel und Verarbeitung,
auch bei minimiertem Hauptfenster. Nach dem Einfügen wird die vorherige
Zwischenablage nach etwa drei Sekunden wiederhergestellt, solange zwischenzeitlich
nichts Neues kopiert wurde. Bei Fensterwechsel oder fehlgeschlagenem Einfügen
bleibt das Diktat in der Zwischenablage für manuelles Strg+V. Anwendungen mit
Administratorrechten können das automatische Einfügen aus einem normal gestarteten
Scribe blockieren. Es wird keine Eingabetaste zum Absenden gesendet.

Beim ersten Diktat muss das Modell gegebenenfalls geladen oder heruntergeladen
werden. Weitere Diktate verwenden das bereits geladene Modell. Erneutes Öffnen
aktiviert das vorhandene Scribe-Fenster, statt weitere Hotkey-Instanzen zu starten.

Entwicklungsprüfung unter Windows:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -q
# Lokale IPC muss im ausführenden Benutzerkontext erlaubt sein:
$env:SCRIBE_TEST_LOCAL_IPC = "1"
.venv\Scripts\python.exe -m unittest discover -s tests -p test_single_instance.py -q
# Öffnet ein eigenes Testfeld; Mikrofon und Spracherkennung sind simuliert:
.venv\Scripts\python.exe tools\verify_dictation_windows.py
```

### Oberflächensprachen

Unter **Einstellungen > Oberflächensprache** lässt sich die Oberfläche sofort
umschalten. **Einstellungen speichern** übernimmt die Auswahl für den nächsten
Start. Alte Konfigurationen starten weiterhin auf Deutsch. Die separate
**Erkennungssprache (Audio)** beeinflusst ausschließlich die Transkription.
Sprachnamen erscheinen in ihrer Eigenbezeichnung. Arabisch verwendet ein
Rechts-nach-links-Layout; Dateipfade und Tastenkombinationen bleiben links nach rechts.
Bearbeitete Transkripte werden beim Sprachwechsel nicht verändert.

Die UTF-8-Kataloge liegen in `src/recorder_app/locales/`; Deutsch ist die
Quellsprache. Neue UI-Texte müssen in allen 15 Übersetzungskatalogen ergänzt
werden. Platzhalter in geschweiften Klammern müssen unverändert erhalten bleiben.
Technische Diagnosen und Meldungen von Windows oder Drittbibliotheken können in
der Originalsprache erscheinen. Native Windows-Dateidialoge folgen der Systemsprache.
Vor einer kommerziellen Veröffentlichung sollten Muttersprachler die Übersetzungen
und die Darstellung im tatsächlichen Betrieb prüfen.

Validierung: `.venv\Scripts\python.exe -m unittest discover -s tests -q`
