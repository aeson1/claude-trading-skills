#!/usr/bin/env python3
"""ADE Equity Score — per-holding research-backed composite (yfinance).

Bottom-up complement to the top-down posture. Transparent methodology:

  * Magic Formula (Greenblatt) core — rank by an earnings-yield measure and
    a return-on-capital measure; a low *combined rank* = cheap + high
    quality.
  * Quality/safety overlay — ROE, debt/equity, gross margin, positive FCF
    (Piotroski-lite), because pure Magic Formula misranks leveraged
    vehicles, deep cyclicals, and pre-profit micro-caps.

Composite = 0..100 (higher better): 70% inverted Magic-Formula rank +
30% quality overlay. Sub-scores AND the proxy basis are always shown.

Data: Yahoo Finance via yfinance — free, no key, Nordic-capable. Yahoo does
not expose exact Greenblatt EBIT/EV and ROIC, so documented PROXIES are
used and labelled per name (`ey_basis`, `rc_basis`):
  earnings yield: EBITDA/EV  >  1/PE  >  netIncome/marketCap
  return on cap : ROA (closest to ROIC)  >  ROE
Funds and names Yahoo can't serve are reported **n/d with a reason** —
never fabricated. Coverage thins on micro-caps; that is surfaced.

Exit codes: 0 ok, 2 no-holdings.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Montrose ticker -> Yahoo symbol (OMX Stockholm = .ST, Oslo = .OL).
_SYMBOL: dict[str, str | None] = {
    "SAAB B": "SAAB-B.ST",
    "PEAB B": "PEAB-B.ST",
    "CLAS B": "CLAS-B.ST",
    "SSAB B": "SSAB-B.ST",
    "INSTAL": "INSTAL.ST",
    "EPIS B": "EPIS-B.ST",
    "MEDI": "MEDI.OL",
    "INVE A": "INVE-A.ST",
    "NDA SE": "NDA-SE.ST",
    "MONTLEV": None,  # leveraged fund — no issuer fundamentals
}


def _latest(pat: str) -> Path | None:
    fs = sorted(glob.glob(str(REPO_ROOT / pat)))
    return Path(fs[-1]) if fs else None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def _fundamentals(sym: str) -> dict[str, Any]:
    """Pull a sparse fundamentals dict from Yahoo; {} on failure."""
    import yfinance as yf
    try:
        info = yf.Ticker(sym).info or {}
    except Exception:  # noqa: BLE001 - yfinance raises many shapes
        return {}
    if not info or info.get("regularMarketPrice") is None and not info.get(
        "enterpriseValue"
    ):
        return {}
    ev = _num(info.get("enterpriseValue"))
    ebitda = _num(info.get("ebitda"))
    pe = _num(info.get("trailingPE"))
    ni = _num(info.get("netIncomeToCommon"))
    mc = _num(info.get("marketCap"))

    ey = ey_basis = None
    if ebitda is not None and ev:
        ey, ey_basis = ebitda / ev, "EBITDA/EV"
    elif pe and pe > 0:
        ey, ey_basis = 1.0 / pe, "1/PE"
    elif ni is not None and mc:
        ey, ey_basis = ni / mc, "NI/MC"

    roa = _num(info.get("returnOnAssets"))
    roe = _num(info.get("returnOnEquity"))
    rc = rc_basis = None
    if roa is not None:
        rc, rc_basis = roa, "ROA"
    elif roe is not None:
        rc, rc_basis = roe, "ROE"

    return {
        "ey": ey, "ey_basis": ey_basis, "rc": rc, "rc_basis": rc_basis,
        "roe": roe, "debt_equity": _num(info.get("debtToEquity")),
        "gross_margin": _num(info.get("grossMargins")),
        "fcf": _num(info.get("freeCashflow")),
    }


def score(holdings: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for h in holdings:
        tk = h.get("ticker") or h.get("instrumentName") or "?"
        sym = _SYMBOL.get(tk, "__none__")
        rec: dict[str, Any] = {"ticker": tk, "name": h.get("instrumentName")}
        if sym is None:
            rec.update(score=None, reason="fund — no issuer fundamentals")
            rows.append(rec)
            continue
        if sym == "__none__":
            rec.update(score=None, reason="no Yahoo symbol mapping")
            rows.append(rec)
            continue
        rec["yahoo_symbol"] = sym
        f = _fundamentals(sym)
        if not f or f.get("ey") is None or f.get("rc") is None:
            rec.update(
                score=None,
                reason="Yahoo lacks usable fundamentals (likely thin "
                "micro-cap coverage)" if f else "no Yahoo data for symbol",
            )
            rows.append(rec)
            continue
        q = 0
        if f["roe"] is not None and f["roe"] > 0.12:
            q += 1
        if f["debt_equity"] is not None and f["debt_equity"] < 100:
            q += 1
        if f["gross_margin"] is not None and f["gross_margin"] > 0.30:
            q += 1
        if f["fcf"] is not None and f["fcf"] > 0:
            q += 1
        rec.update(
            earnings_yield=round(f["ey"], 4), ey_basis=f["ey_basis"],
            return_on_cap=round(f["rc"], 4), rc_basis=f["rc_basis"],
            quality_sub=q, quality_max=4, roe=f["roe"],
            debt_equity=f["debt_equity"], gross_margin=f["gross_margin"],
        )
        rows.append(rec)

    covered = [r for r in rows if r.get("earnings_yield") is not None]
    n = len(covered)
    if n:
        for i, r in enumerate(
            sorted(covered, key=lambda r: -r["earnings_yield"])
        ):
            r["_rey"] = i + 1
        for i, r in enumerate(
            sorted(covered, key=lambda r: -r["return_on_cap"])
        ):
            r["_rrc"] = i + 1
        for r in covered:
            mf = r["_rey"] + r["_rrc"]
            mf_norm = 100 * (1 - (mf - 2) / max(1, (2 * n - 2)))
            q_norm = 100 * r["quality_sub"] / r["quality_max"]
            r["magic_formula_rank"] = mf
            r["score"] = round(0.70 * mf_norm + 0.30 * q_norm, 1)
            r.pop("_rey", None)
            r.pop("_rrc", None)
    return rows


def main(argv: list[str] | None = None) -> int:
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--holdings")
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    hp = Path(a.holdings) if a.holdings else _latest(
        "state/montrose/holdings_*.json")
    if hp is None or not hp.is_file():
        sys.stderr.write("ERROR: no holdings snapshot found.\n")
        return 2
    snap = json.loads(hp.read_text())
    as_of = snap.get("as_of") or date.today().isoformat()
    rows = score(snap.get("holdings") or [])

    out = Path(a.out) if a.out else (
        REPO_ROOT / "reports" / f"equity_scores_{as_of}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"as_of": as_of,
         "method": "magic_formula(yfinance proxies) 0.7 + quality 0.3",
         "scores": rows}, indent=2))

    covered = sorted(
        (r for r in rows if r.get("score") is not None),
        key=lambda r: -r["score"],
    )
    print(f"wrote {out}")
    print(f"coverage: {len(covered)}/{len(rows)} holdings scored")
    for r in covered:
        print(f"  {r['score']:5.1f}  {r['ticker']:7s} "
              f"EY={r['earnings_yield']:.3f}({r['ey_basis']}) "
              f"RC={r['return_on_cap']:.3f}({r['rc_basis']}) "
              f"Q={r['quality_sub']}/4")
    for r in rows:
        if r.get("score") is None:
            print(f"   n/d   {r['ticker']:7s} — {r['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
