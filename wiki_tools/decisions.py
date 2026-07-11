"""Record human decisions into the trading operations wiki.

Unlike ingest.py (which derives pages from operational CSVs), decision logs
capture deliberate operator choices: trades, control overrides, strategy
shifts, and investigation results. A decision may also clear one or more lint
flags (see lint.py flag ids), recording the human judgement that resolves a
warning so it stops nagging while staying auditable.

Usage:
    python -m wiki_tools.decisions \
        --type investigation_result \
        --description "Reviewed the crypto mixed draft thesis." \
        --rationale "Signals match the regime note; positioning is conservative." \
        --owner trader \
        --expected "Hold BTC/ETH paper longs, monitor XRP/SOL/DOGE." \
        --links regime_note_crypto_2026-07-11 strategy_thesis_crypto_mixed \
        --clears review:strategy_thesis_crypto_mixed
"""

from __future__ import annotations

import argparse

from . import common

DECISION_TYPES = ("trade", "override_control", "strategy_shift", "investigation_result")
DECISION_VERDICTS = ("as_expected", "better_than_expected", "worse_than_expected", "inconclusive")


def record_decision(
    decision_type: str,
    description: str,
    rationale: str,
    owner: str,
    expected_outcome: str = "",
    links: list[str] | None = None,
    clears: list[str] | None = None,
    outcome: str = "",
    verdict: str = "",
) -> str:
    """Write a decision_log page and rebuild the index. Returns the page id."""
    if decision_type not in DECISION_TYPES:
        raise ValueError(
            f"decision_type '{decision_type}' is not one of {', '.join(DECISION_TYPES)}"
        )
    if not description.strip():
        raise ValueError("description is required")
    if not rationale.strip():
        raise ValueError("rationale is required")
    if not owner.strip():
        raise ValueError("owner is required")
    if verdict and verdict not in DECISION_VERDICTS:
        raise ValueError(
            f"verdict '{verdict}' is not one of {', '.join(DECISION_VERDICTS)}"
        )
    if outcome.strip() and not verdict:
        raise ValueError("verdict is required when outcome is set")

    common.ensure_dirs()
    date = common.today_utc()
    sequence = common.next_sequence("decision_log", date)
    page_id = f"decision_log_{date}_{sequence:03d}"

    data = {
        "schema_version": common.SCHEMA_VERSION,
        "id": page_id,
        "decision_id": f"dec_{date}_{sequence:03d}",
        "timestamp": common.iso_now(),
        "decision_type": decision_type,
        "description": description.strip(),
        "rationale": rationale.strip(),
        "owner": owner.strip(),
        "expected_outcome": expected_outcome.strip(),
        "clears": list(clears or []),
        "outcome": outcome.strip(),
        "verdict": verdict.strip(),
        "links": list(links or []),
    }

    common.write_page("decision_log", page_id, data)
    cleared_note = f" (clears {', '.join(data['clears'])})" if data["clears"] else ""
    common.append_log(f"decision: wrote {page_id} [{decision_type}]{cleared_note}")
    common.build_index()
    return page_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a decision log into the wiki.")
    parser.add_argument("--type", dest="decision_type", required=True, choices=DECISION_TYPES)
    parser.add_argument("--description", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--expected", dest="expected_outcome", default="")
    parser.add_argument("--outcome", default="")
    parser.add_argument("--verdict", default="", choices=("",) + DECISION_VERDICTS)
    parser.add_argument("--links", nargs="*", default=[])
    parser.add_argument(
        "--clears",
        nargs="*",
        default=[],
        help="Lint flag ids this decision resolves (from the lint report).",
    )
    args = parser.parse_args()

    page_id = record_decision(
        decision_type=args.decision_type,
        description=args.description,
        rationale=args.rationale,
        owner=args.owner,
        expected_outcome=args.expected_outcome,
        links=args.links,
        clears=args.clears,
        outcome=args.outcome,
        verdict=args.verdict,
    )
    print(f"Recorded {page_id}")


if __name__ == "__main__":
    main()
