"""Retrieve prior comparable regime decisions from the operations wiki.

The recall layer answers: when we were in a similar regime before, what did we
decide and how did it turn out?
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from . import common


def _parse_iso(value: Any) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_recall(limit: int = 3) -> dict[str, Any]:
    """Build comparable-regime memory from wiki pages.

    Returns the latest regime per asset class and up to `limit` linked
    decisions for the same regime, including outcome/verdict when available.
    """
    common.ensure_dirs()

    regime_notes: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for page_type, _path, data in common.iter_pages():
        if page_type == "regime_note":
            regime_notes.append(data)
        elif page_type == "decision_log":
            decisions.append(data)

    note_by_id = {str(n.get("id", "")): n for n in regime_notes if n.get("id")}

    latest_by_asset: dict[str, dict[str, Any]] = {}
    for note in regime_notes:
        asset = str(note.get("asset_class", "options"))
        current = latest_by_asset.get(asset)
        if current is None or _parse_iso(note.get("timestamp")) > _parse_iso(current.get("timestamp")):
            latest_by_asset[asset] = note

    assets: list[dict[str, Any]] = []
    for asset, latest in sorted(latest_by_asset.items()):
        regime = str(latest.get("regime", "")).strip()
        matches: list[dict[str, Any]] = []
        for decision in decisions:
            for link in decision.get("links", []) or []:
                linked_note = note_by_id.get(str(link))
                if not linked_note:
                    continue
                linked_asset = str(linked_note.get("asset_class", "options"))
                linked_regime = str(linked_note.get("regime", "")).strip()
                if linked_asset == asset and linked_regime == regime:
                    matches.append(
                        {
                            "id": decision.get("id", ""),
                            "timestamp": decision.get("timestamp", ""),
                            "decision_type": decision.get("decision_type", ""),
                            "description": decision.get("description", ""),
                            "outcome": decision.get("outcome", ""),
                            "verdict": decision.get("verdict", ""),
                        }
                    )
                    break

        matches.sort(key=lambda d: _parse_iso(d.get("timestamp")), reverse=True)
        selected = matches[:limit]
        verdict_counts = {
            "as_expected": 0,
            "better_than_expected": 0,
            "worse_than_expected": 0,
            "inconclusive": 0,
            "pending": 0,
        }
        for item in selected:
            verdict = str(item.get("verdict", "")).strip()
            if not verdict:
                verdict_counts["pending"] += 1
            elif verdict in verdict_counts:
                verdict_counts[verdict] += 1

        assets.append(
            {
                "asset_class": asset,
                "current_regime": regime,
                "current_regime_note_id": latest.get("id", ""),
                "decision_count": len(matches),
                "verdict_counts": verdict_counts,
                "recent_decisions": selected,
            }
        )

    return {
        "schema_version": common.SCHEMA_VERSION,
        "generated_at": common.iso_now(),
        "assets": assets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Recall similar-regime decisions from the wiki.")
    parser.add_argument("--limit", type=int, default=3, help="Max decisions per asset class")
    args = parser.parse_args()

    report = build_recall(limit=max(1, args.limit))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
