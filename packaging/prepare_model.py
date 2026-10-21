"""Download only public base-model assets; never copy a personal model cache."""
import argparse
from pathlib import Path
import re

from huggingface_hub import snapshot_download

parser = argparse.ArgumentParser()
parser.add_argument("--revision", required=True, help="Reviewed 40-character Hugging Face commit SHA")
args = parser.parse_args()
if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
    parser.error("--revision must be a full lowercase commit SHA")

target = Path(__file__).resolve().parents[1] / "build" / "installer-model" / "base"
snapshot_download(
    repo_id="Systran/faster-whisper-base",
    revision=args.revision,
    local_dir=target,
    token=False,
    allow_patterns=["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.txt"],
)
(target / "revision.txt").write_text(args.revision + "\n", encoding="utf-8")
