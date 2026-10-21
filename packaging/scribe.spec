from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
package = root / "src" / "recorder_app"
model = root / "build" / "installer-model" / "base"
required_model_files = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
for name in required_model_files:
    if not (model / name).is_file():
        raise SystemExit(f"Missing model file: {model / name}. Run prepare_model.py first.")

# Explicit application data allowlist: never collect the project or user profile.
datas = [(str(package / "native" / "app_audio_capture.exe"), "recorder_app/native")]
for pattern in ("assets/*.ico", "assets/*.png", "assets/*.svg", "locales/*.json"):
    datas.extend((str(path), "recorder_app/" + path.parent.name) for path in package.glob(pattern))
for name in (*required_model_files, "preprocessor_config.json"):
    if (model / name).is_file():
        datas.append((str(model / name), "recorder_app/bundled_models/base"))

binaries = []
hiddenimports = []
for dependency in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "soundcard", "soundfile", "tokenizers"):
    dep_data, dep_binaries, dep_imports = collect_all(dependency)
    datas.extend(dep_data)
    binaries.extend(dep_binaries)
    hiddenimports.extend(dep_imports)

a = Analysis(
    [str(root / "src" / "main.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="scribe",
    console=False, icon=str(package / "assets" / "scribe.ico"), upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="scribe", upx=False)
