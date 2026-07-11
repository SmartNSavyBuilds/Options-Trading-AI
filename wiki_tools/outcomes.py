"""Update decision outcomes in the trading operations wiki.

This module closes the decision loop: once enough time has passed, the
operator records what happened (`outcome`) and whether it matched the plan
(`verdict`).

Usage examples:
    python -m wiki_tools.outcomes \
        --id decision_log_2026-07-11_001 \
        --outcome "Held BTC/ETH paper long watch, no added risk." \
        --verdict as_expected

    python -m wiki_tools.outcomes \
        --id decision_log_2026-07-11_001 \
        --from-performance \
        --verdict better_than_expected
"""

from __future__ import annotations

import argparse

from . import common

VERDICTS = ("as_expected", "better_than_expected", "worse_than_expected", "inconclusive")


def _find_decision_page(decision_id: str):
    for page_type, path, data in common.iter_pages():
        if page_type == "decision_log" and str(data.get("id", "")) == decision_id:
            return path, data
    return None, None


def _performance_snapshot_text() -> str:
    row = common.load_csv_row(common.OUTPUTS_DIR / "performance_summary.csv")
    if row is None:
        raise ValueError("outputs/performance_summary.csv not found or empty")

    snapshot_time = row.get("journaled_at_utc", "")
    open_positions = row.get("open_positions", "")
    market_value = row.get("total_market_value", "")
    unrealized = row.get("total_unrealized_pl", "")
    winners = row.get("winning_positions", "")
    losers = row.get("losing_positions", "")
    return (
        f"Performance snapshot {snapshot_time}: "
        f"open positions {open_positions}, market value {market_value}, "
        f"unrealized P/L {unrealized}, winners {winners}, losers {losers}."
    )


def record_outcome(
    decision_id: str,
    outcome: str,
    verdict: str,
    append_performance_snapshot: bool = False,
) -> str:
    """Update an existing decision_log with outcome and verdict."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict '{verdict}' is not one of {', '.join(VERDICTS)}")

    path, data = _find_decision_page(decision_id)
    if path is None or data is None:
        raise ValueError(f"Decision id '{decision_id}' was not found")

    outcome_text = outcome.strip()
    if append_performance_snapshot:
        perf_text = _performance_snapshot_text()
        outcome_text = f"{outcome_text}\n\n{perf_text}".strip()
    if not outcome_text:
        raise ValueError("outcome is required")

    data["outcome"] = outcome_text
    data["verdict"] = verdict
    data["outcome_updated_at"] = common.iso_now()

    common.write_page("decision_log", str(data.get("id", decision_id)), data)
    common.append_log(f"outcome: updated {decision_id} [{verdict}]")
    common.build_index()
    return str(data.get("id", decision_id))


def main() -> None:
    parser = argparse.ArgumentParser(description="Record outcome and verdict on a decision log.")
    parser.add_argument("--id", required=True, help="decision_log page id (for example decision_log_2026-07-11_001)")
    parser.add_argument("--outcome", default="", help="What actually happened")
    parser.add_argument("--verdict", required=True, choices=VERDICTS)
    parser.add_argument(
        "--from-performance",
        action="store_true",
        help="Append a compact snapshot from outputs/performance_summary.csv",
    )
    args = parser.parse_args()

    page_id = record_outcome(
        decision_id=args.id,
        outcome=args.outcome,
        verdict=args.verdict,
        append_performance_snapshot=args.from_performance,
    )
    print(f"Outcome updated for {page_id}")


if __name__ == "__main__":
    main()
