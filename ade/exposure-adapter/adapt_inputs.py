#!/usr/bin/env python3
"""ADE adapter: normalize sibling-skill JSON for exposure-coach ingestion.

Upstream contract gap (not our bug, don't edit upstream):
  exposure-coach/calculate_exposure.py reads `breadth_score` / `top_risk_score`
  at the TOP LEVEL of the JSON, but:
    - market-breadth-analyzer writes  composite.composite_score   (0-100 health,
      higher = healthier breadth)
    - market-top-detector  writes     composite.composite_score   (0-100 TOP
      RISK, higher = more dangerous) + composite.zone
  So exposure-coach silently ignores both inputs and stays LOW-confidence.

This adapter reads the latest of each and emits top-level-keyed files
exposure-coach can consume. It is additive and ADE-owned (survives
`git pull upstream`). It does NOT modify the source reports or upstream code.

Mappings (documented + transparent):
  breadth_score  = round(breadth.composite.composite_score)
                   (same 0-100 health scale + direction — pass through)
  top_risk_score = zone-mapped from market-top.composite.zone, because the
                   two composites are NOT the same calibrated scale; a zone
                   map preserves the skill's intent better than naive
                   100 - x arithmetic. Falls back to round(100 - score).
                     green  -> 85   (low top risk = safe)
                     yellow -> 65
                     orange -> 40   (elevated -> exposure-coach 'reduce' band)
                     red    -> 20   (severe  -> 'strong cash' band)

Exit codes: 0 ok, 1 error.
"""

from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS = REPO_ROOT / "reports"

_ZONE_TO_SAFETY = {
    "green": 85,
    "yellow": 65,
    "orange": 40,
    "red": 20,
}


def _latest(pattern: str, exclude: str | None = None) -> Path | None:
    files = sorted(glob.glob(str(REPORTS / pattern)))
    if exclude:
        files = [f for f in files if exclude not in f]
    return Path(files[-1]) if files else None


def _composite(doc: dict) -> dict:
    c = doc.get("composite")
    return c if isinstance(c, dict) else {}


def main() -> int:
    bf = _latest("market_breadth_2*.json", exclude="history")
    tf = _latest("market_top_*.json")
    if bf is None and tf is None:
        sys.stderr.write(
            "ERROR: no market_breadth_*/market_top_* reports found in "
            f"{REPORTS}. Run those skills first.\n"
        )
        return 1

    wrote = []

    if bf is not None:
        bc = _composite(json.loads(bf.read_text()))
        score = bc.get("composite_score")
        if score is None:
            sys.stderr.write(f"WARN: no composite_score in {bf.name}\n")
        else:
            out = {
                "breadth_score": int(round(float(score))),
                "_adapted_from": bf.name,
                "_source_zone": bc.get("zone"),
            }
            p = REPORTS / "_adapted_breadth.json"
            p.write_text(json.dumps(out, indent=2))
            wrote.append(
                f"breadth_score={out['breadth_score']} "
                f"(from {bf.name}, zone={bc.get('zone')})"
            )

    if tf is not None:
        tc = _composite(json.loads(tf.read_text()))
        score = tc.get("composite_score")
        zone = str(tc.get("zone") or "").strip().lower()
        zkey = next((z for z in _ZONE_TO_SAFETY if z in zone), None)
        if zkey is not None:
            safety = _ZONE_TO_SAFETY[zkey]
            basis = f"zone='{tc.get('zone')}'"
        elif score is not None:
            safety = max(0, min(100, int(round(100 - float(score)))))
            basis = f"100 - composite {score} (zone unknown)"
        else:
            sys.stderr.write(f"WARN: no zone/score in {tf.name}\n")
            safety = None
            basis = ""
        if safety is not None:
            out = {
                "top_risk_score": safety,
                "_adapted_from": tf.name,
                "_basis": basis,
                "_source_composite": score,
            }
            p = REPORTS / "_adapted_top_risk.json"
            p.write_text(json.dumps(out, indent=2))
            wrote.append(f"top_risk_score={safety} (from {tf.name}, {basis})")

    if not wrote:
        sys.stderr.write("ERROR: nothing adapted.\n")
        return 1
    print("Adapted for exposure-coach:")
    for w in wrote:
        print("  " + w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
