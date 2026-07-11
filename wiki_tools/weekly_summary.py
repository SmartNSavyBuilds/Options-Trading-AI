"""Generate one weekly summary page from operational outputs.

The summary stays deterministic and model-free: metrics and themes are derived
from CSV outputs and existing wiki pages.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any

from . import common
from . import discovery_quality


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _week_start(dt: datetime) -> datetime:
    return (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def _in_week(ts: str, week_start: datetime, week_end: datetime) -> bool:
    parsed = _parse_iso(ts)
    return parsed is not None and week_start <= parsed < week_end


def generate_weekly_summary(week_of: str | None = None, author: str = "system") -> str:
    """Generate or refresh the weekly summary singleton for the target week."""
    common.ensure_dirs()

    now = datetime.now(timezone.utc)
    monday = _week_start(now)
    if week_of:
        parsed_week = datetime.fromisoformat(f"{week_of}T00:00:00+00:00")
        monday = _week_start(parsed_week)
    week_start = monday
    week_end = monday + timedelta(days=7)
    week_of_str = week_start.strftime("%Y-%m-%d")
    page_id = f"weekly_summary_{week_of_str}"

    performance = common.load_csv_row(common.OUTPUTS_DIR / "performance_summary.csv") or {}
    net_pnl = round(_to_float(performance.get("total_unrealized_pl"), 0.0), 2)

    exec_rows = common.load_csv_rows(common.OUTPUTS_DIR / "execution_log.csv")
    week_exec = [r for r in exec_rows if _in_week(str(r.get("timestamp", "")), week_start, week_end)]
    filled = [r for r in week_exec if str(r.get("status", "")).strip().lower() in {"filled", "executed"}]
    trades_closed = len(filled)
    winning_positions = _to_int(performance.get("winning_positions"), 0)
    win_ratio = round((winning_positions / trades_closed), 3) if trades_closed else 0.0

    journal_rows = common.load_csv_rows(common.OUTPUTS_DIR / "performance_journal.csv")
    week_journal = [
        r
        for r in journal_rows
        if _in_week(str(r.get("journaled_at_utc", "")), week_start, week_end)
    ]
    sorted_by_pl = sorted(week_journal, key=lambda r: _to_float(r.get("unrealized_pl"), 0.0), reverse=True)
    if sorted_by_pl:
        top = sorted_by_pl[0]
        biggest_win = (
            f"{top.get('symbol', 'Unknown')} {top.get('side', '')} position currently leads at "
            f"{_to_float(top.get('unrealized_pl', 0.0), 0.0):.2f} unrealized P/L."
        )
        bottom = sorted_by_pl[-1]
        biggest_loss = (
            f"{bottom.get('symbol', 'Unknown')} {bottom.get('side', '')} position currently trails at "
            f"{_to_float(bottom.get('unrealized_pl', 0.0), 0.0):.2f} unrealized P/L."
        )
    else:
        biggest_win = "No position-level performance rows were captured this week."
        biggest_loss = "No position-level performance rows were captured this week."

    regime_notes = []
    incidents = []
    for page_type, _path, data in common.iter_pages():
        ts = str(data.get("timestamp", ""))
        if not _in_week(ts, week_start, week_end):
            continue
        if page_type == "regime_note":
            regime_notes.append(data)
        elif page_type == "incident":
            incidents.append(data)

    regime_by_asset: dict[str, list[str]] = {}
    for note in sorted(regime_notes, key=lambda d: str(d.get("timestamp", ""))):
        asset = str(note.get("asset_class", "options"))
        regime_by_asset.setdefault(asset, []).append(str(note.get("regime", "unknown")))
    transitions = []
    for asset, values in sorted(regime_by_asset.items()):
        unique = []
        for value in values:
            if not unique or unique[-1] != value:
                unique.append(value)
        if len(unique) > 1:
            transitions.append(f"{asset}: {' -> '.join(unique)}")
        elif unique:
            transitions.append(f"{asset}: stayed {unique[0]}")
    regime_transitions = "; ".join(transitions) if transitions else "No regime-note transitions recorded this week."

    incident_ids = [str(i.get("id", "")) for i in incidents if i.get("id")]
    if incident_ids:
        control_incidents = [f"{iid} (tracked in incidents)" for iid in incident_ids]
    else:
        control_incidents = ["No incident pages were recorded this week."]

    control_rows = common.load_csv_rows(common.OUTPUTS_DIR / "control_audit_history.csv")
    week_controls = [r for r in control_rows if _in_week(str(r.get("checked_at_utc", "")), week_start, week_end)]
    misses = sum(1 for r in week_controls if common.as_bool(r.get("control_miss")))

    learning_rows = common.load_csv_rows(common.OUTPUTS_DIR / "learning_feedback.csv")
    portfolio_learning = next((r for r in learning_rows if str(r.get("ticker", "")) == "__PORTFOLIO__"), {})
    fill_rate_rows = common.load_csv_rows(common.OUTPUTS_DIR / "execution_quality.csv")
    protective = next(
        (r for r in fill_rate_rows if "protective" in str(r.get("execution_style", "")).lower()),
        {},
    )
    discovery_rows = common.load_csv_rows(common.OUTPUTS_DIR / "opportunity_discovery.csv")
    top_discovery = sorted(discovery_rows, key=lambda r: _to_float(r.get("signal_score"), 0.0), reverse=True)[:3]
    top_names = [str(r.get("ticker", "")).strip() for r in top_discovery if r.get("ticker")]

    themes = [
        (
            f"Control cadence logged {misses} control-miss snapshots this week; "
            f"incident tracking remains {'active' if incident_ids else 'clear'}."
        ),
        (
            f"Protective-exit fill rate is {str(protective.get('fill_rate_pct', 'n/a'))}% "
            "in execution quality output, keeping exit reliability as a top operations focus."
        ),
        (
            f"Portfolio learning adjustment is {str(portfolio_learning.get('learning_adjustment', 'n/a'))}; "
            "feedback continues to favor the current playbook."
        ),
        (
            f"Discovery leaders this cycle: {', '.join(top_names) if top_names else 'none captured'}; "
            "research attention should prioritize conviction validation before routing."
        ),
    ]

    upcoming_week = [
        "Re-check pre-live gate freshness and broker authorization before automatic routing windows.",
        "Review protective-exit execution quality daily and escalate if fill-rate remains below target.",
        (
            f"Run thesis review on top discovery names ({', '.join(top_names) if top_names else 'none'}) "
            "before scaling exposure."
        ),
    ]

    link_ids = sorted(set(incident_ids + [str(n.get("id", "")) for n in regime_notes if n.get("id")]))

    quality = discovery_quality.build_discovery_quality()
    blocked_items = [
        i for i in quality.get("items", [])
        if str(i.get("status", "")) == "block"
    ]
    top_blockers = [
        f"{i.get('ticker', '')}: {', '.join(i.get('meta', {}).get('blocked_reasons', [])) or 'below threshold'}"
        for i in blocked_items[:3]
    ]

    data = {
        "schema_version": common.SCHEMA_VERSION,
        "id": page_id,
        "week_of": week_of_str,
        "net_pnl": net_pnl,
        "currency": "USD",
        "win_ratio": win_ratio,
        "trades_closed": trades_closed,
        "biggest_win": biggest_win,
        "biggest_loss": biggest_loss,
        "themes": themes,
        "regime_transitions": regime_transitions,
        "control_incidents": control_incidents,
        "upcoming_week": upcoming_week,
        "discovery_quality_formula_version": quality.get("formula_version"),
        "discovery_quality_avg_score": quality.get("avg_discovery_quality_score"),
        "discovery_quality_pass_count": quality.get("pass_count"),
        "discovery_quality_block_count": quality.get("block_count"),
        "discovery_quality_top_blockers": top_blockers,
        "author": author,
        "links": link_ids,
    }

    common.write_page("weekly_summary", page_id, data)
    common.append_log(f"weekly_summary: wrote {page_id}")
    common.build_index()
    return page_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a weekly summary page from outputs.")
    parser.add_argument("--week-of", default="", help="ISO week anchor date (YYYY-MM-DD). Defaults to current UTC week.")
    parser.add_argument("--author", default="system")
    args = parser.parse_args()

    page_id = generate_weekly_summary(week_of=args.week_of or None, author=args.author)
    print(f"Generated {page_id}")


if __name__ == "__main__":
    main()
