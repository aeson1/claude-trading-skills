#!/usr/bin/env python3
"""ADE Portfolio Analysis — deterministic report assembler.

Combines what the other ADE/skill pieces already produce into one
decision-grade markdown document:

  state/montrose/holdings_<date>.json   (Montrose snapshot)
  reports/exposure_posture_*.json       (exposure-coach, via ade adapter)
  state/montrose/forces_digest_*.md     (macro forces digest)

Emits reports/portfolio_analysis_<as_of>.md, always closing with the
mandated Next Steps / Decisions block. Deterministic + idempotent: same
inputs -> same report. No upstream edits, no network.

Sector classification is a small editable heuristic map (Montrose holdings
carry no sector); refine `_SECTOR` as the book changes.

Exit codes: 0 ok, 2 missing inputs.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Editable heuristic — ticker -> (sector, is_leveraged)
_SECTOR: dict[str, tuple[str, bool]] = {
    "SAAB B": ("Defense", False),
    "PEAB B": ("Construction/Industrials", False),
    "SSAB B": ("Materials", False),
    "INSTAL": ("Industrials", False),
    "CLAS B": ("Consumer/Retail", False),
    "EPIS B": ("Healthcare/Medtech", False),
    "MEDI": ("Healthcare/Medtech", False),
    "INVE A": ("Financials/Holding", False),
    "NDA SE": ("Financials", False),
    "MONTLEV": ("Diversified fund (leveraged)", True),
}
_LEVERAGE_FACTOR = 1.25  # Montrose Global Leverage 125
SINGLE_NAME_CAP = 10.0   # % of invested
SECTOR_CAP = 25.0        # % of invested
WINNER_PCT = 50.0        # unrealized % that flags "extended"


def _latest(pattern: str, exclude: str | None = None) -> Path | None:
    fs = sorted(glob.glob(str(REPO_ROOT / pattern)))
    if exclude:
        fs = [f for f in fs if exclude not in f]
    return Path(fs[-1]) if fs else None


def _sek(x: float) -> str:
    return f"{x:,.0f} SEK"


def _load(p: Path | None) -> Any:
    return json.loads(p.read_text()) if p and p.is_file() else None


def build(holdings_p, posture_p, forces_p, eq_p=None) -> tuple[str, str]:
    snap = _load(holdings_p)
    if not snap:
        raise SystemExit(2)
    as_of = snap.get("as_of") or date.today().isoformat()
    accounts = snap.get("accounts") or []
    hold = snap.get("holdings") or []

    invested = sum(h.get("marketValue") or 0 for h in hold)
    cash = sum(
        (a.get("summary") or {}).get("availableForPurchase") or 0
        for a in accounts
    )
    total = invested + cash

    # per-holding enriched
    rows = []
    for h in hold:
        mv = h.get("marketValue") or 0.0
        tk = h.get("ticker") or h.get("instrumentName") or "?"
        sec, lev = _SECTOR.get(tk, ("Unclassified", False))
        rows.append({
            "name": h.get("instrumentName") or tk,
            "ticker": tk,
            "mv": mv,
            "w": (mv / invested * 100) if invested else 0.0,
            "ur_pct": h.get("unrealizedResultPercent"),
            "ccy": h.get("instrumentCurrency") or h.get("currency"),
            "sector": sec,
            "lev": lev,
        })
    rows.sort(key=lambda r: r["mv"], reverse=True)

    top5 = sum(r["w"] for r in rows[:5])
    over = [r for r in rows if r["w"] > SINGLE_NAME_CAP]
    winners = [
        r for r in rows
        if isinstance(r["ur_pct"], (int, float)) and r["ur_pct"] >= WINNER_PCT
    ]
    sectors: dict[str, float] = {}
    for r in rows:
        sectors[r["sector"]] = sectors.get(r["sector"], 0.0) + r["w"]
    sectors_sorted = sorted(sectors.items(), key=lambda kv: -kv[1])
    over_sectors = [(s, w) for s, w in sectors_sorted if w > SECTOR_CAP]
    ccy: dict[str, float] = {}
    for r in rows:
        ccy[r["ccy"] or "?"] = ccy.get(r["ccy"] or "?", 0.0) + r["mv"]
    funded_accts = [
        a for a in accounts
        if (a.get("summary") or {}).get("totalMarketValue")
    ]

    # posture
    P = _load(posture_p) or {}
    rec = P.get("recommendation", "n/a")
    ceil = P.get("exposure_ceiling_pct",
                  P.get("exposure_ceiling", P.get("ceiling")))
    conf = P.get("confidence", "n/a")
    part = P.get("participation", "n/a")

    # equity scores (bottom-up; optional)
    EQ = _load(eq_p) or {}
    eq_rows = EQ.get("scores") or []
    eq_by_tk = {e.get("ticker"): e for e in eq_rows}

    # forces (condensed: keep the implication section)
    forces_txt = ""
    if forces_p and forces_p.is_file():
        ft = forces_p.read_text()
        m = re.search(
            r"## Equity-exposure implication.*?(?=\n## |\Z)", ft, re.S
        )
        forces_txt = (m.group(0).strip() if m else ft[:1200].strip())

    nxt = (datetime.fromisoformat(as_of) + timedelta(days=7)).date().isoformat()

    L: list[str] = []
    add = L.append
    add(f"# Portfolio Analysis — {as_of}")
    add("")
    add(f"**Account book:** Montrose · **Base currency:** SEK · "
        f"**Cadence:** weekly + event-driven · **Next scheduled review:** "
        f"{nxt}")
    add(f"**Generated:** {datetime.now().isoformat(timespec='seconds')} · "
        f"deterministic from mirrored data")
    add("")
    add("> Analysis, not advice. Nothing was executed; no trade tickets "
        "generated.")
    add("")

    add("## 1. Snapshot")
    add("")
    add(f"- **Total capital:** {_sek(total)}")
    add(f"- **Invested:** {_sek(invested)} "
        f"({invested/total*100:.0f}%) · **Cash:** {_sek(cash)} "
        f"({cash/total*100:.0f}%)")
    add(f"- **Funded accounts:** "
        f"{', '.join(a.get('accountName') or a.get('accountType') for a in funded_accts) or 'n/a'} "
        f"(invested book is single-account)")
    add("")

    add("## 2. Holdings & weights")
    add("")
    add("| Holding | Sector | Weight | Value | Unrealized | Score |")
    add("|---|---|--:|--:|--:|--:|")
    for r in rows:
        up = (f"{r['ur_pct']:+.0f}%"
              if isinstance(r["ur_pct"], (int, float)) else "n/a")
        e = eq_by_tk.get(r["ticker"]) or {}
        sc = (f"{e['score']:.0f}"
              if isinstance(e.get("score"), (int, float)) else "n/d")
        add(f"| {r['name']}{' ⚠lev' if r['lev'] else ''} | {r['sector']} | "
            f"{r['w']:.1f}% | {_sek(r['mv'])} | {up} | {sc} |")
    add("")

    add("## 3. Concentration & risk")
    add("")
    over_str = ", ".join(
        f"{r['ticker']} {r['w']:.1f}%" for r in over) or "none"
    add(f"- **Top-5 weight:** {top5:.1f}%  ·  "
        f"**Names >{SINGLE_NAME_CAP:.0f}%:** {over_str}")
    add(f"- **Sector sleeves:** "
        + " · ".join(f"{s} {w:.1f}%" for s, w in sectors_sorted))
    if over_sectors:
        add(f"- **Sector breach (> {SECTOR_CAP:.0f}%):** "
            + ", ".join(f"{s} {w:.1f}%" for s, w in over_sectors))
    add("- **Currency:** "
        + " · ".join(
            f"{c} {v/invested*100:.0f}%" for c, v in
            sorted(ccy.items(), key=lambda kv: -kv[1])))
    lev_rows = [r for r in rows if r["lev"]]
    for r in lev_rows:
        be = r["w"] * _LEVERAGE_FACTOR
        add(f"- **Leverage:** {r['ticker']} {r['w']:.1f}% × "
            f"{_LEVERAGE_FACTOR} ≈ **{be:.1f}% beta-equivalent** "
            f"(largest single risk)")
    add("- **Structural:** invested book 100% one account; ~single-currency "
        "SEK; geography ~all Nordic.")
    add("")

    add("## 4. Market posture")
    add("")
    add(f"- **Recommendation:** **{rec}** · Exposure ceiling: "
        f"{ceil if ceil is not None else 'n/a'}% · Participation: {part}")
    add(f"- **Confidence:** {conf} "
        f"(exposure-coach via ADE adapter; FMP-gated inputs partial)")
    cs = P.get("component_scores") or {}
    if cs:
        add("- **Signals:** "
            + " · ".join(f"{k}={v}" for k, v in cs.items()
                         if not str(k).startswith("_")))
    add("")

    add("## 5. Macro forces (Growth Strategy KB)")
    add("")
    add(forces_txt or "_Forces digest not available this run._")
    add("")

    add("## 6. Recommendations")
    add("")
    if rec == "REDUCE_ONLY" or (isinstance(ceil, (int, float)) and ceil < 50):
        add("- Posture caps gross exposure — **diversify by trimming "
            "over-weights (raise cash), not by adding new positions.**")
    min_trim = max(2000.0, invested * 0.01)
    trims: list[dict[str, Any]] = []
    for r in over:
        px = None
        for h in hold:
            if (h.get("ticker") or h.get("instrumentName")) == r["ticker"]:
                q = h.get("quantity") or 0
                px = (h.get("marketValue") / q) if q else None
        tgt_cap = 7.5 if r["lev"] else SINGLE_NAME_CAP
        sell_val = max(0.0, r["mv"] - invested * tgt_cap / 100)
        if sell_val < min_trim:
            continue
        trims.append({
            "ticker": r["ticker"], "w": r["w"], "lev": r["lev"],
            "tgt_cap": tgt_cap, "sell_val": sell_val,
            "units": (f"~{sell_val/px:,.0f} units" if px else ""),
        })
    for t in trims:
        add(f"- **Trim {t['ticker']}** {t['w']:.1f}% → {t['tgt_cap']:.1f}% "
            f"cap: sell {t['units']} (~{_sek(t['sell_val'])})"
            + (" — leverage-adjusted" if t["lev"] else ""))
    near = [
        r["ticker"] for r in over
        if not any(t["ticker"] == r["ticker"] for t in trims)
    ]
    if near:
        add(f"- _At/near cap — immaterial trim, monitor only: "
            f"{', '.join(near)}._")
    for s, w in over_sectors:
        add(f"- **Reduce {s}** sleeve {w:.1f}% → ≤{SECTOR_CAP:.0f}%.")
    for r in winners:
        add(f"- **Profit-take candidate:** {r['ticker']} "
            f"(+{r['ur_pct']:.0f}% unrealized, {r['w']:.1f}%).")
    add("")

    if eq_rows:
        scored = sorted(
            (e for e in eq_rows if isinstance(e.get("score"), (int, float))),
            key=lambda e: -e["score"],
        )
        nd = [e for e in eq_rows if e.get("score") is None]
        add("## 7. Equity scores (bottom-up)")
        add("")
        add(f"Magic Formula (Greenblatt) 70% + quality overlay 30%, "
            f"0–100. Method: `{EQ.get('method', 'n/a')}`. "
            f"Coverage {len(scored)}/{len(eq_rows)}.")
        add("")
        if scored:
            add("| Ticker | Score | Earnings yield | Return on cap | "
                "Quality |")
            add("|---|--:|--:|--:|--:|")
            for e in scored:
                add(f"| {e['ticker']} | **{e['score']:.0f}** | "
                    f"{e['earnings_yield']:.3f} ({e['ey_basis']}) | "
                    f"{e['return_on_cap']:.3f} ({e['rc_basis']}) | "
                    f"{e['quality_sub']}/{e['quality_max']} |")
        if nd:
            add("")
            add("_Not scored: "
                + "; ".join(f"{e['ticker']} ({e['reason']})" for e in nd)
                + "._")
        add("")
        add("> Caveats: yfinance proxies (EBITDA/EV or 1/PE for earnings "
            "yield; ROA/ROE for return on capital) — not exact Greenblatt "
            "inputs. Magic Formula is unreliable for **financials/banks** "
            "(structurally low ROA — read Nordea's score with that in mind) "
            "and **pre-profit names** (Episurf's negative score correctly "
            "reflects losses but is not a 'cheapness' signal). Bottom-up "
            "score, not a recommendation.")
        add("")

    add("## Next Steps / Decisions")
    add("")
    add("1. **Do now**")
    for t in trims:
        tag = " (leverage-adjusted 7.5%)" if t["lev"] else " (10% cap)"
        add(f"   - Trim **{t['ticker']}**{tag} — see §6 for size.")
    if not trims:
        add("   - No material single-name trims; hold and monitor.")
    if over_sectors:
        add(f"   - Reduce the {over_sectors[0][0]} sleeve toward "
            f"≤{SECTOR_CAP:.0f}%.")
    add("   - Route trim proceeds to cash (raises dry powder; "
        "posture-compliant).")
    add("2. **Decide**")
    add("   - Apply Supabase posture persistence (admin-gated `0004`)? "
        "y/n")
    add("   - Generate human-confirmed Montrose trade ticket(s) for the "
        "trims? y/n")
    add("3. **Then / when**")
    add("   - Hold cash; stage diversifying redeployment only when posture "
        "leaves REDUCE_ONLY. Re-run this doc weekly or on a posture flip.")
    add("4. **Would change this**")
    add("   - Breadth >50 **and** uptrend out of 'Bear' → posture flips "
        "toward NEW_ENTRY_ALLOWED, unlocking redeployment of the cash.")
    add("")

    add("## Caveats & sources")
    add("")
    add(f"- Holdings: `{holdings_p.name}` · Posture: "
        f"`{posture_p.name if posture_p else 'n/a'}` · Forces: "
        f"`{forces_p.name if forces_p else 'n/a'}`")
    add("- Posture confidence is MEDIUM at best (macro-regime weak on "
        "free-tier FMP; institutional-flow premium-gated).")
    add("- Sector map is an editable heuristic in `build_report.py`.")
    add("- Analysis, not advice. Nothing executed; no tickets generated.")
    add("")

    return as_of, "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--holdings")
    ap.add_argument("--posture")
    ap.add_argument("--forces")
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    hp = Path(a.holdings) if a.holdings else _latest(
        "state/montrose/holdings_*.json")
    pp = Path(a.posture) if a.posture else _latest(
        "reports/exposure_posture_*.json")
    fp = Path(a.forces) if a.forces else _latest(
        "state/montrose/forces_digest_*.md")
    ep = _latest("reports/equity_scores_*.json")
    if hp is None:
        import sys
        sys.stderr.write(
            "ERROR: no holdings snapshot (run the montrose-portfolio "
            "refresh first)\n")
        return 2

    as_of, md = build(hp, pp, fp, ep)
    out = Path(a.out) if a.out else (
        REPO_ROOT / "reports" / f"portfolio_analysis_{as_of}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"wrote {out}  ({len(md.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
