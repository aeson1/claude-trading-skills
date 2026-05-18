#!/usr/bin/env bash
# ADE market-signal driver: regime trio + ADE adapter + exposure-coach.
# FMP key is read from the macOS Keychain (ade-fmp/fmp) at runtime — never
# on the command line, never in a file. Resilient: one failing skill does
# not abort the rest (exposure-coach tolerates partial inputs).
set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 1
PY=./.venv/bin/python
flt() { grep -v NotOpenSSL | grep -v warnings.warn || true; }

FMP="$(security find-generic-password -s ade-fmp -a fmp -w 2>/dev/null || true)"
[ -n "$FMP" ] && export FMP_API_KEY="$FMP" || \
  echo "WARN: no FMP key in Keychain (ade-fmp/fmp) — regime/top will skip"

echo "== breadth ==";  $PY skills/market-breadth-analyzer/scripts/market_breadth_analyzer.py --output-dir reports/ 2>&1 | flt | tail -1
echo "== uptrend ==";  $PY skills/uptrend-analyzer/scripts/uptrend_analyzer.py --output-dir reports/ 2>&1 | flt | tail -1
if [ -n "${FMP_API_KEY:-}" ]; then
  echo "== macro-regime =="; $PY skills/macro-regime-detector/scripts/macro_regime_detector.py --output-dir reports/ 2>&1 | flt | tail -1
  echo "== market-top ==";   $PY skills/market-top-detector/scripts/market_top_detector.py --output-dir reports/ 2>&1 | flt | tail -1
fi
unset FMP_API_KEY FMP

echo "== ade adapter =="; $PY ade/exposure-adapter/adapt_inputs.py 2>&1 | flt
U=$(ls -t reports/uptrend_analysis_*.json 2>/dev/null | head -1)
R=$(ls -t reports/macro_regime_*.json 2>/dev/null | head -1)
ARGS=()
[ -f reports/_adapted_breadth.json ]  && ARGS+=(--breadth reports/_adapted_breadth.json)
[ -f reports/_adapted_top_risk.json ] && ARGS+=(--top-risk reports/_adapted_top_risk.json)
[ -n "${U:-}" ] && ARGS+=(--uptrend "$U")
[ -n "${R:-}" ] && ARGS+=(--regime "$R")
echo "== exposure-coach =="
$PY skills/exposure-coach/scripts/calculate_exposure.py "${ARGS[@]}" --output-dir reports/ 2>&1 | flt | grep -E 'Exposure Ceiling|Recommendation|Confidence' || true
echo "done."
