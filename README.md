# LoopScribe

LoopScribe ist eine lokale Windows-Desktop-Anwendung zur Transkription von Systemaudio. Die App nimmt per WASAPI-Loopback den Ton der Standardausgabe auf, speichert die Aufnahme als WAV-Datei und transkribiert sie anschließend lokal mit `faster-whisper`.

## Funktionsumfang

- Aufnahme von Systemaudio statt nur Mikrofon
- Globale Start-/Stopp-Hotkeys
- Automatische Speicherung von Aufnahmen als `.wav`
- Automatische Transkription nach dem Stoppen der Aufnahme
- Import bestehender Audiodateien wie `.wav`, `.mp3`, `.m4a`, `.flac` und `.ogg`
- Fortschrittsanzeige während der Transkription
- Sprachwahl: Auto, Deutsch, Englisch
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

## Optional erweiterbar

Die Architektur ist vorbereitet, um später Live-Transkription oder Speaker-Diarization zu ergänzen. Diese beiden Punkte sind in der aktuellen Version bewusst noch nicht aktiviert, damit die Basis stabil und lokal nutzbar bleibt.
