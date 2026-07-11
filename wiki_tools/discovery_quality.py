"""Compute deterministic discovery quality scores for routing decisions.

Writes `wiki/discovery_quality.json` so the score history is part of the wiki
layer (not only a transient dashboard computation).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from . import common

FORMULA_VERSION = "v1"
PASS_THRESHOLD = 60


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _signal_component(signal_score: float) -> float:
    """Map signal_score [-10, 10] into [0, 40]."""
    norm = (signal_score + 10.0) / 20.0
    return round(_clamp(norm, 0.0, 1.0) * 40.0, 2)


def _regime_component(bias: str, regime: str) -> float:
    bias = str(bias or "").strip().lower()
    regime = str(regime or "").strip().lower()
    if not bias or not regime:
        return 8.0
    if bias == "neutral":
        return 10.0
    if bias == regime:
        return 20.0
    if regime == "mixed":
        return 10.0
    return 0.0


def _execution_component(fill_rate_pct: float) -> float:
    """Map protective-exit fill rate [0, 100] into [0, 20]."""
    return round(_clamp(fill_rate_pct, 0.0, 100.0) / 100.0 * 20.0, 2)


def _concentration_component(exposure_after: float) -> float:
    """Full points <=20% projected exposure; linearly decays to 0 by 50%."""
    if exposure_after <= 20.0:
        return 20.0
    if exposure_after >= 50.0:
        return 0.0
    ratio = (50.0 - exposure_after) / 30.0
    return round(_clamp(ratio, 0.0, 1.0) * 20.0, 2)


def _latest_regimes() -> dict[str, str]:
    regimes: dict[str, tuple[str, str]] = {}
    for page_type, _path, data in common.iter_pages():
        if page_type != "regime_note":
            continue
        asset = str(data.get("asset_class", "options"))
        ts = str(data.get("timestamp", ""))
        current = regimes.get(asset)
        if current is None or ts > current[0]:
            regimes[asset] = (ts, str(data.get("regime", "mixed")))
    return {asset: regime for asset, (_ts, regime) in regimes.items()}


def build_discovery_quality() -> dict[str, Any]:
    """Build and persist discovery quality scores for current candidates."""
    common.ensure_dirs()
    discovery_rows = common.load_csv_rows(common.OUTPUTS_DIR / "opportunity_discovery.csv")
    queue_rows = common.load_csv_rows(common.OUTPUTS_DIR / "paper_trade_queue.csv")
    queue_by_ticker = {
        str(row.get("ticker", "")).strip().upper(): row
        for row in queue_rows
        if row.get("ticker")
    }

    quality_rows = common.load_csv_rows(common.OUTPUTS_DIR / "execution_quality.csv")
    protective = next(
        (row for row in quality_rows if "protective" in str(row.get("execution_style", "")).lower()),
        {},
    )
    fill_rate = _to_float(protective.get("fill_rate_pct"), 0.0)
    execution_component = _execution_component(fill_rate)

    regimes = _latest_regimes()
    default_regime = regimes.get("options", "mixed")

    items: list[dict[str, Any]] = []
    for row in discovery_rows:
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        queue = queue_by_ticker.get(ticker, {})
        bias = str(row.get("bias", "neutral"))
        signal_score = _to_float(row.get("signal_score"), 0.0)
        exposure_after = _to_float(queue.get("portfolio_exposure_after_trade_pct"), 0.0)
        guardrail_status = str(queue.get("guardrail_status", "")).strip().lower() or "unknown"

        signal_component = _signal_component(signal_score)
        regime_component = _regime_component(bias, default_regime)
        concentration_component = _concentration_component(exposure_after)
        total = round(signal_component + regime_component + execution_component + concentration_component, 2)

        status = "pass" if total >= PASS_THRESHOLD and guardrail_status == "pass" else "block"
        reasons = []
        if total < PASS_THRESHOLD:
            reasons.append(f"score below threshold {PASS_THRESHOLD}")
        if guardrail_status != "pass":
            reasons.append(f"guardrail_status={guardrail_status}")

        items.append(
            {
                "ticker": ticker,
                "bias": bias,
                "signal_score": signal_score,
                "discovery_quality_score": total,
                "status": status,
                "formula_version": FORMULA_VERSION,
                "components": {
                    "signal": signal_component,
                    "regime_alignment": regime_component,
                    "execution_reliability": execution_component,
                    "concentration": concentration_component,
                },
                "meta": {
                    "regime": default_regime,
                    "guardrail_status": guardrail_status,
                    "projected_exposure_after_trade_pct": exposure_after,
                    "blocked_reasons": reasons,
                },
            }
        )

    items.sort(key=lambda r: (r["status"] != "pass", -float(r["discovery_quality_score"])))
    pass_count = sum(1 for i in items if i["status"] == "pass")
    block_count = sum(1 for i in items if i["status"] == "block")
    avg_score = round(sum(float(i["discovery_quality_score"]) for i in items) / len(items), 2) if items else 0.0

    report = {
        "generated_at": common.iso_now(),
        "formula_version": FORMULA_VERSION,
        "threshold": PASS_THRESHOLD,
        "execution_fill_rate_pct": fill_rate,
        "avg_discovery_quality_score": avg_score,
        "pass_count": pass_count,
        "block_count": block_count,
        "items": items,
    }
    (common.WIKI_DIR / "discovery_quality.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    common.append_log(
        f"discovery_quality: avg {avg_score}, pass {pass_count}, block {block_count}, formula {FORMULA_VERSION}"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Build wiki discovery quality scores.")
    _args = parser.parse_args()
    report = build_discovery_quality()
    print(
        "Discovery quality:",
        f"avg {report['avg_discovery_quality_score']}, pass {report['pass_count']}, block {report['block_count']}",
    )


if __name__ == "__main__":
    main()
