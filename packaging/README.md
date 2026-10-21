# scribe Windows installer (preparation only)

Status: build configuration prepared; no installer has been compiled or tested.

The installer creates a per-user installation, desktop shortcut, Start menu entry
and Windows uninstall entry. It does not launch the application automatically.
Uninstall leaves personal recordings, transcripts and settings intact.

## Build prerequisites and later steps

1. Use Windows x64 and the project's Python environment with its dependencies.
2. Install PyInstaller 6.22.3 into the build environment and Inno Setup 6.
3. Select and record a reviewed commit SHA for the public
   `Systran/faster-whisper-base` model. Download the model with:
   `.venv\Scripts\python.exe packaging\prepare_model.py --revision <commit-sha>`.
4. Run `powershell -File packaging\build.ps1`. This builds only; it does not
   install or launch the application or execute tests.
5. The distributable will be `dist\installer\scribe-Setup-0.1.0-windows-x64.exe`.

The build aborts when its application output directory already exists to prevent
stale files entering the installer. Review/remove only that generated directory
before a subsequent build. No automatic recursive cleanup is performed.

## Package boundary

Only imported program code, dependencies, selected assets/locales, the native
audio helper and explicitly listed base-model files are bundled. Never add the
project root, personal AppData/Documents folders, settings.json, recordings,
transcripts, smoke_output, tmp, output or the entire virtual environment as data.
The model is fetched separately without personal authentication; its download
metadata is not bundled. The base model runs from the installed package;
additional models retain the normal per-user download behavior.

## Pending before distribution

- Pin the complete build dependency set and preserve a build manifest.
- Review and include redistribution notices/licenses for dependencies and model.
- Confirm helper/.NET and native-library runtime requirements on clean Windows.
- Compile the executable and installer, inspect the package contents, then test
  install, desktop/start-menu launch, recording, transcription and uninstall.
- App-specific audio requires Windows build 20348 or newer according to the helper.
- No signing configuration is included yet.

None of these checks has been executed as part of the preparation request.

References: https://pyinstaller.org/en/stable/spec-files.html and
https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm
