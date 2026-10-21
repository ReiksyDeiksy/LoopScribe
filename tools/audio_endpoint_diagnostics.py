from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recorder_app.audio import WindowsAudioEndpointStateReader  # noqa: E402


def endpoint_payload(endpoint) -> dict[str, object]:
    return {
        "flow": endpoint.flow,
        "role": endpoint.role,
        "endpoint_id": endpoint.endpoint_id,
        "master_volume_scalar": endpoint.master_volume_scalar,
        "master_volume_percent": (
            round(endpoint.master_volume_scalar * 100.0, 1)
            if endpoint.master_volume_scalar is not None
            else None
        ),
        "muted": endpoint.muted,
        "error": endpoint.error,
    }


def snapshot_payload(label: str, reader: WindowsAudioEndpointStateReader) -> dict[str, object]:
    snapshot = reader.read_default_state()
    return {
        "label": label,
        "captured_at": snapshot.captured_at.isoformat(timespec="seconds"),
        "default_render": endpoint_payload(snapshot.default_render),
        "default_capture": endpoint_payload(snapshot.default_capture),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Loggt Windows-Playback- und Mikrofon-Endpunktstatus fuer LoopScribe-Diagnosen."
    )
    parser.add_argument(
        "--watch-seconds",
        type=float,
        default=0.0,
        help="Optional: Dauer fuer eine Vorher/Nachher-Beobachtung.",
    )
    args = parser.parse_args()

    reader = WindowsAudioEndpointStateReader()
    print(json.dumps(snapshot_payload("initial", reader), ensure_ascii=False))
    if args.watch_seconds > 0:
        time.sleep(max(0.0, args.watch_seconds))
        print(json.dumps(snapshot_payload("final", reader), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
