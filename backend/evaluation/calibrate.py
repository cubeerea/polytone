"""Calibration / sanity report for the Style Distance Score.

    uv run python -m backend.evaluation.calibrate            # feature-only (default)
    POLYTONE_EMBEDDINGS=qwen uv run python -m backend.evaluation.calibrate --embeddings

Uses the two-voice fixture (backend/tests/fixtures/eval/voices.json): a terse lowercase
Slack voice and a formal long-sentence prose voice. For each voice as baseline it scores the
same voice's held-out texts, the same voice on the other voice's topic, and the other voice.
The gap (same-author mean − different-author mean) is the number that matters.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from backend.evaluation import embeddings
from backend.evaluation.style_distance import score

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "eval" / "voices.json"


def load_voices(path: Path = FIXTURE) -> dict[str, Any]:
    data = json.loads(path.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def run(use_embeddings: bool = False) -> dict[str, Any]:
    voices = load_voices()
    names = sorted(voices)
    report: dict[str, Any] = {"mode": "qwen" if use_embeddings else "features", "voices": {}}
    for me in names:
        base = voices[me]["baseline"]
        rows: dict[str, list[dict]] = {"same": [], "same_cross_topic": [], "other": []}
        for t in voices[me]["heldout"]:
            rows["same"].append(score(t, base, me, use_embeddings=use_embeddings))
        for t in voices[me].get("heldout_cross_topic", []):
            rows["same_cross_topic"].append(score(t, base, me, use_embeddings=use_embeddings))
        for other in names:
            if other == me:
                continue
            for t in voices[other]["heldout"] + voices[other].get("heldout_cross_topic", []):
                rows["other"].append(score(t, base, me, use_embeddings=use_embeddings))
        summary = {k: round(statistics.mean(r["score"] for r in v), 1) for k, v in rows.items() if v}
        summary["gap"] = round(summary["same"] - summary["other"], 1)
        summary["identical"] = score(base[0], [base[0]], me, use_embeddings=use_embeddings)["score"]
        report["voices"][me] = {"summary": summary, "rows": rows}
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embeddings", action="store_true", help="include the Qwen embedding component")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    t0 = time.perf_counter()
    rep = run(use_embeddings=args.embeddings)
    elapsed = time.perf_counter() - t0
    print(f"mode={rep['mode']}  elapsed={elapsed:.2f}s  model_load={embeddings.load_seconds()}")
    for name, v in rep["voices"].items():
        print(f"baseline={name:7s} {v['summary']}")
        if args.verbose:
            for kind, rows in v["rows"].items():
                for r in rows:
                    print(f"   {kind:17s} {r['score']:5.1f} {r['components']} {r['confidence']}")


if __name__ == "__main__":
    main()
