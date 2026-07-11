"""Ingest raw operational outputs into the trading operations wiki.

MVP scope (schema_version 1): read control_audit_latest.csv and, when a
control miss is present, write one incident page. Rebuilds the index and
appends to the operation log every run. Idempotent: re-running for the same
day and the same set of control reasons does not create duplicate pages.
"""

from __future__ import annotations

from . import common


def _incident_signature(date: str, reasons: list[str]) -> str:
    return f"{date}|{'|'.join(sorted(reasons))}"


def _existing_signatures() -> set[str]:
    signatures: set[str] = set()
    for page_type, _path, data in common.iter_pages():
        if page_type != "incident":
            continue
        signature = data.get("_signature")
        if signature:
            signatures.add(str(signature))
    return signatures


def _next_sequence(date: str) -> int:
    prefix = f"incident_{date}_"
    directory = common.WIKI_DIR / common.SUBDIRS["incident"]
    existing = [p for p in directory.glob(f"{prefix}*.md")] if directory.exists() else []
    return len(existing) + 1


def ingest_control_audit() -> str | None:
    """Write an incident page if the latest control audit shows a miss.

    Returns the page id if a page was written, otherwise None.
    """
    row = common.load_control_latest()
    if row is None:
        common.append_log("ingest: no control_audit_latest.csv found; nothing to do")
        return None

    if not common.as_bool(row.get("control_miss")):
        common.append_log("ingest: control_miss is clear; no incident written")
        return None

    reasons = [r for r in str(row.get("control_miss_reasons", "")).split("|") if r]
    date = common.today_utc()
    signature = _incident_signature(date, reasons)

    if signature in _existing_signatures():
        common.append_log(f"ingest: incident already recorded for {signature}; skipped")
        return None

    is_hard_block = any(reason in common.HARD_BLOCK_REASONS for reason in reasons)
    humanized = [common.humanize_reason(reason) for reason in reasons]
    severity = "hard block" if is_hard_block else "soft hold"

    sequence = _next_sequence(date)
    page_id = f"incident_{date}_{sequence:03d}"

    description = (
        f"Daily control check reported a control miss ({severity}). "
        f"Reasons: {'; '.join(humanized)}. "
        f"Source: control_audit_latest.csv."
    )

    data = {
        "schema_version": common.SCHEMA_VERSION,
        "id": page_id,
        "timestamp": common.iso_now(),
        "incident_type": "control_miss",
        "status": "open",
        "control_reason": "|".join(reasons),
        "description": description,
        "raw_sources": ["outputs/control_audit_latest.csv"],
        "resolution": "",
        "owner": "system",
        "links": [],
        "_signature": signature,
    }

    common.write_page("incident", page_id, data)
    common.append_log(f"ingest: wrote {page_id} ({severity}) for reasons {reasons}")
    return page_id


def _format_movers(movers: list[dict]) -> str:
    parts = []
    for mover in movers:
        symbol = mover.get("underlying_symbol", "?")
        sector = mover.get("sector", "")
        pct = mover.get("exposure_pct", "")
        label = f"{symbol} {pct}%" if pct else symbol
        if sector:
            label += f" ({sector})"
        parts.append(label)
    return "; ".join(parts) if parts else "No tracked exposures"


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _confidence(bullish_ratio: float, bearish_ratio: float, score: float) -> int:
    """Shared, transparent conviction score used across asset classes."""
    confidence = 50 + (bullish_ratio - bearish_ratio) * 50 + score * 5
    return int(max(0, min(100, round(confidence))))


def _options_regime() -> dict | None:
    """Build a normalized regime snapshot for the options/equity book."""
    regime = common.load_csv_row(common.OUTPUTS_DIR / "market_regime.csv")
    if regime is None:
        return None
    bullish = _num(regime.get("bullish_ratio"))
    bearish = _num(regime.get("bearish_ratio"))
    score = _num(regime.get("avg_signal_score"))
    performance = common.load_csv_row(common.OUTPUTS_DIR / "performance_summary.csv") or {}
    action = ""
    unrealized = performance.get("total_unrealized_pl")
    if unrealized not in (None, ""):
        action = f"Open positions unrealized P/L: {unrealized}. No automated position change."
    return {
        "asset_class": "options",
        "regime": str(regime.get("market_regime", "unknown")).strip(),
        "note": str(regime.get("regime_note", "")).strip(),
        "volatility": f"avg signal volatility {regime.get('avg_volatility', 'n/a')} (VIX feed not connected)",
        "breadth": (
            f"bullish {regime.get('bullish_ratio', 'n/a')}, "
            f"bearish {regime.get('bearish_ratio', 'n/a')}, "
            f"avg signal score {regime.get('avg_signal_score', 'n/a')}"
        ),
        "bullish_ratio": bullish,
        "bearish_ratio": bearish,
        "avg_signal_score": score,
        "movers": _format_movers(common.top_exposures(3)),
        "positioning": _format_movers(common.top_exposures(5)),
        "action_taken": action,
        "signals": [
            f"Breadth: bullish {regime.get('bullish_ratio', 'n/a')}, bearish {regime.get('bearish_ratio', 'n/a')}",
            f"Average signal score {regime.get('avg_signal_score', 'n/a')}",
            f"Average volatility {regime.get('avg_volatility', 'n/a')}",
        ],
        "sources": [
            "outputs/market_regime.csv",
            "outputs/exposure_summary.csv",
            "outputs/performance_summary.csv",
        ],
    }


def _crypto_regime() -> dict | None:
    """Build a normalized regime snapshot from the crypto watchlist."""
    rows = common.load_csv_rows(common.OUTPUTS_DIR / "crypto_watchlist.csv")
    if not rows:
        return None
    total = len(rows)
    bullish_ratio = sum(1 for r in rows if r.get("bias") == "bullish") / total
    bearish_ratio = sum(1 for r in rows if r.get("bias") == "bearish") / total
    avg_score = sum(_num(r.get("signal_score")) for r in rows) / total
    avg_vol = sum(_num(r.get("annualized_volatility")) for r in rows) / total

    if bullish_ratio > bearish_ratio and bullish_ratio >= 0.5:
        regime_name = "bullish"
    elif bearish_ratio > bullish_ratio and bearish_ratio >= 0.5:
        regime_name = "bearish"
    else:
        regime_name = "mixed"

    ranked = sorted(rows, key=lambda r: _num(r.get("signal_score")), reverse=True)
    movers = "; ".join(
        f"{r.get('ticker', '?')} ({r.get('bias', '')}, score {r.get('signal_score', '')})"
        for r in ranked[:3]
    )
    return {
        "asset_class": "crypto",
        "regime": regime_name,
        "note": f"{total} names tracked; {int(bullish_ratio * 100)}% bullish, {int(bearish_ratio * 100)}% bearish.",
        "volatility": f"avg annualized volatility {avg_vol:.3f}",
        "breadth": f"bullish ratio {bullish_ratio:.2f}, bearish ratio {bearish_ratio:.2f}, avg score {avg_score:.2f}",
        "bullish_ratio": bullish_ratio,
        "bearish_ratio": bearish_ratio,
        "avg_signal_score": avg_score,
        "movers": movers,
        "positioning": movers,
        "action_taken": "",
        "signals": [
            f"Breadth: bullish ratio {bullish_ratio:.2f}, bearish ratio {bearish_ratio:.2f}",
            f"Average signal score {avg_score:.2f}",
            f"Average annualized volatility {avg_vol:.3f}",
        ],
        "sources": ["outputs/crypto_watchlist.csv"],
    }


def ingest_regime_note(snapshot: dict) -> str:
    """Write today's regime note for one asset class from a normalized snapshot."""
    asset = snapshot["asset_class"]
    date = common.today_utc()
    page_id = f"regime_note_{asset}_{date}"
    regime_name = snapshot["regime"]
    note = snapshot["note"]
    data = {
        "schema_version": common.SCHEMA_VERSION,
        "id": page_id,
        "timestamp": common.iso_now(),
        "asset_class": asset,
        "regime": regime_name,
        "market_state": f"{regime_name}: {note}" if note else regime_name,
        "vix_level": snapshot["volatility"],
        "breadth": snapshot["breadth"],
        "sector_movers": snapshot["movers"],
        "data_sources": snapshot["sources"],
        "action_taken": snapshot["action_taken"],
        "links": [],
    }
    common.write_page("regime_note", page_id, data)
    common.append_log(f"ingest: wrote {page_id} (regime '{regime_name}')")
    return page_id


def ingest_strategy_thesis(snapshot: dict) -> str:
    """Maintain one thesis per asset class and regime from a normalized snapshot."""
    asset = snapshot["asset_class"]
    regime_name = snapshot["regime"]
    date = common.today_utc()
    page_id = f"strategy_thesis_{asset}_{regime_name}"
    path = common.page_path("strategy_thesis", page_id)
    confidence = _confidence(
        snapshot["bullish_ratio"], snapshot["bearish_ratio"], snapshot["avg_signal_score"]
    )

    if path.exists():
        data = common.read_page(path)
        data["confidence"] = confidence
        data["positioning"] = snapshot["positioning"]
        data["key_signals"] = snapshot["signals"]
        data["last_updated"] = date
        data.setdefault("contradictions", f"None identified as of {date}")
        action = "updated"
    else:
        data = {
            "schema_version": common.SCHEMA_VERSION,
            "id": page_id,
            "asset_class": asset,
            "regime": regime_name,
            "confidence": confidence,
            "thesis": (
                f"Auto-generated draft for {asset} from operational signals. "
                f"Current regime '{regime_name}': {snapshot['note']} "
                f"Review and refine with a human market view before relying on it."
            ),
            "key_signals": snapshot["signals"],
            "contradictions": f"None identified as of {date}",
            "positioning": snapshot["positioning"],
            "review_cadence": "daily",
            "last_updated": date,
            "auto_generated": True,
            "needs_review": True,
            "links": [f"regime_note_{asset}_{date}"],
        }
        action = "created"

    common.write_page("strategy_thesis", page_id, data)
    common.append_log(f"ingest: {action} {page_id} (confidence {confidence})")
    return page_id


def main() -> None:
    common.ensure_dirs()
    written: list[str] = []

    incident_id = ingest_control_audit()
    if incident_id:
        written.append(incident_id)

    for provider in (_options_regime, _crypto_regime):
        snapshot = provider()
        if snapshot is None:
            continue
        written.append(ingest_regime_note(snapshot))
        written.append(ingest_strategy_thesis(snapshot))

    common.build_index()
    if written:
        print("Ingested:", ", ".join(written))
    else:
        print("Nothing ingested (no source data found).")


if __name__ == "__main__":
    main()
