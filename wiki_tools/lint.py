"""Lint the trading operations wiki against schema_version 1 rules.

Runs the machine-checkable subset of WIKI_SCHEMA.md: global field presence,
id/filename consistency, timestamp validity, per-type raw_sources and
resolution requirements, and staleness of open incidents. Writes
lint_report.json and prints a summary. Warn-only: always exits 0.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import common

INCIDENT_STALE_HOURS = 24
DECISION_STALE_DAYS = 7
DECISION_TYPES = ("trade", "override_control", "strategy_shift", "investigation_result")
DECISION_VERDICTS = ("as_expected", "better_than_expected", "worse_than_expected", "inconclusive")


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


def _finding(
    page: str, level: str, rule: str, message: str, flag_id: str | None = None
) -> dict[str, str]:
    finding = {"page": page, "level": level, "rule": rule, "message": message}
    if flag_id:
        finding["flag_id"] = flag_id
    return finding


def _check_page(page_type: str, path: Path, data: dict[str, Any]) -> list[dict[str, str]]:
    rel = str(path.relative_to(common.WIKI_DIR))
    findings: list[dict[str, str]] = []

    if data.get("__parse_error__"):
        findings.append(_finding(rel, "error", "parse", "Page is not valid YAML"))
        return findings

    # Global mandatory fields.
    if data.get("schema_version") != common.SCHEMA_VERSION:
        findings.append(
            _finding(rel, "warn", "schema_version", f"Missing or non-current schema_version (expected {common.SCHEMA_VERSION})")
        )
    page_id = data.get("id")
    if not page_id:
        findings.append(_finding(rel, "error", "id", "Missing required 'id' field"))
    elif page_id != path.stem:
        findings.append(_finding(rel, "warn", "id", f"id '{page_id}' does not match filename '{path.stem}'"))
    if not isinstance(data.get("links"), list):
        findings.append(_finding(rel, "error", "links", "Missing required 'links' list"))

    # Timestamp validity (where a timestamp-like field exists).
    now = datetime.now(timezone.utc)
    for field in ("timestamp", "last_updated", "created_date", "last_tested", "last_review"):
        if field in data and data[field]:
            parsed = _parse_iso(data[field])
            if parsed is None:
                findings.append(_finding(rel, "error", "timestamp", f"Field '{field}' is not valid ISO 8601"))
            elif parsed > now:
                findings.append(_finding(rel, "warn", "timestamp", f"Field '{field}' is future-dated"))

    # Type-specific rules.
    if page_type in ("incident", "regime_note"):
        raw_sources = data.get("raw_sources") or data.get("data_sources")
        if not raw_sources:
            findings.append(_finding(rel, "error", "citation", "raw_sources/data_sources must be non-empty"))

    if page_type == "incident":
        status = str(data.get("status", "")).strip()
        if status == "resolved" and not str(data.get("resolution", "")).strip():
            findings.append(_finding(rel, "error", "resolution", "status=resolved requires a non-empty resolution"))
        if status == "open":
            parsed = _parse_iso(data.get("timestamp"))
            if parsed is not None:
                age_hours = (now - parsed).total_seconds() / 3600
                if age_hours > INCIDENT_STALE_HOURS:
                    findings.append(
                        _finding(rel, "warn", "staleness", f"Open incident is {age_hours:.0f}h old (> {INCIDENT_STALE_HOURS}h)")
                    )

    return findings


def _check_contradiction(
    regime_notes: list[dict[str, Any]], theses: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Cheap hard-rule contradiction check (schema global rule 6).

    Per asset class: flags when the most recent regime note's regime has no
    matching strategy thesis for the same asset class. Warn-only; clearing
    requires a decision_log entry.
    """
    findings: list[dict[str, str]] = []
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for note in regime_notes:
        by_asset.setdefault(str(note.get("asset_class", "unknown")), []).append(note)

    for asset, notes in by_asset.items():
        latest = max(notes, key=lambda d: str(d.get("timestamp", "")))
        current_regime = str(latest.get("regime", "")).strip()
        if not current_regime:
            continue
        asset_thesis_regimes = {
            str(t.get("regime"))
            for t in theses
            if str(t.get("asset_class", "unknown")) == asset and t.get("regime")
        }
        if not asset_thesis_regimes:
            continue
        if current_regime not in asset_thesis_regimes:
            findings.append(
                _finding(
                    latest.get("id", "regime_note"),
                    "warn",
                    "contradiction",
                    f"[{asset}] current regime '{current_regime}' has no matching thesis "
                    f"(theses cover: {', '.join(sorted(asset_thesis_regimes))})",
                    flag_id=f"contradiction:{asset}:{current_regime}",
                )
            )
    return findings


def _check_unreviewed_theses(theses: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Flag auto-generated draft theses that no human has accepted yet.

    Auto-drafts should not silently drive trading. The operator clears the flag
    by recording a decision_log (investigation_result or strategy_shift).
    """
    findings: list[dict[str, str]] = []
    for thesis in theses:
        if thesis.get("auto_generated") and thesis.get("needs_review"):
            thesis_id = str(thesis.get("id", "strategy_thesis"))
            findings.append(
                _finding(
                    thesis_id,
                    "warn",
                    "review",
                    f"Auto-generated thesis '{thesis_id}' has not been reviewed by a human",
                    flag_id=f"review:{thesis_id}",
                )
            )
    return findings


def _check_decision_logs(
    decision_logs: list[dict[str, Any]], all_ids: set[str]
) -> list[dict[str, str]]:
    """Validate decision_log links, decision_type, and outcome follow-up."""
    findings: list[dict[str, str]] = []
    now = datetime.now(timezone.utc)
    for data in decision_logs:
        page_id = str(data.get("id", "decision_log"))
        decision_type = str(data.get("decision_type", "")).strip()
        if decision_type not in DECISION_TYPES:
            findings.append(
                _finding(page_id, "error", "decision_type", f"Invalid decision_type '{decision_type}'")
            )
        for linked in data.get("links", []) or []:
            if str(linked) not in all_ids:
                findings.append(
                    _finding(page_id, "warn", "linkage", f"Linked page '{linked}' does not exist")
                )
        parsed = _parse_iso(data.get("timestamp"))
        outcome_text = str(data.get("outcome", "")).strip()
        verdict = str(data.get("verdict", "")).strip()
        if verdict and verdict not in DECISION_VERDICTS:
            findings.append(
                _finding(
                    page_id,
                    "error",
                    "verdict",
                    f"Invalid verdict '{verdict}' (expected one of {', '.join(DECISION_VERDICTS)})",
                )
            )
        if outcome_text and not verdict:
            findings.append(
                _finding(page_id, "warn", "verdict", "Outcome is set but verdict is missing")
            )

        if parsed is not None and not outcome_text:
            age_days = (now - parsed).total_seconds() / 86400
            if age_days > DECISION_STALE_DAYS:
                findings.append(
                    _finding(
                        page_id,
                        "warn",
                        "outcome",
                        f"Decision is {age_days:.0f}d old with no recorded outcome (needs follow-up)",
                    )
                )
    return findings


def _apply_cleared(
    findings: list[dict[str, str]], cleared: dict[str, str]
) -> list[dict[str, str]]:
    """Downgrade flagged findings that a decision_log has explicitly cleared.

    Cleared flags become level 'info' (not counted as warnings) and record the
    decision that acknowledged them, preserving the audit trail.
    """
    for finding in findings:
        flag_id = finding.get("flag_id")
        if flag_id and flag_id in cleared:
            finding["level"] = "info"
            finding["message"] += f" - acknowledged by {cleared[flag_id]}"
    return findings


def run() -> dict[str, Any]:
    common.ensure_dirs()
    findings: list[dict[str, str]] = []
    page_count = 0
    regime_notes: list[dict[str, Any]] = []
    theses: list[dict[str, Any]] = []
    decision_logs: list[dict[str, Any]] = []
    all_ids: set[str] = set()
    cleared: dict[str, str] = {}
    for page_type, path, data in common.iter_pages():
        page_count += 1
        findings.extend(_check_page(page_type, path, data))
        if data.get("id"):
            all_ids.add(str(data.get("id")))
        if page_type == "regime_note":
            regime_notes.append(data)
        elif page_type == "strategy_thesis":
            theses.append(data)
        elif page_type == "decision_log":
            decision_logs.append(data)
            for flag_id in data.get("clears", []) or []:
                cleared[str(flag_id)] = str(data.get("id", "decision_log"))

    findings.extend(_check_contradiction(regime_notes, theses))
    findings.extend(_check_unreviewed_theses(theses))
    findings.extend(_check_decision_logs(decision_logs, all_ids))
    _apply_cleared(findings, cleared)

    errors = sum(1 for f in findings if f["level"] == "error")
    warnings = sum(1 for f in findings if f["level"] == "warn")
    acknowledged = sum(1 for f in findings if f["level"] == "info")
    report = {
        "schema_version": common.SCHEMA_VERSION,
        "generated_at": common.iso_now(),
        "pages_checked": page_count,
        "errors": errors,
        "warnings": warnings,
        "acknowledged": acknowledged,
        "findings": findings,
    }
    (common.WIKI_DIR / "lint_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    common.append_log(
        f"lint: {page_count} pages, {errors} errors, {warnings} warnings, {acknowledged} acknowledged"
    )
    return report


def main() -> None:
    report = run()
    print(
        f"Linted {report['pages_checked']} pages: "
        f"{report['errors']} errors, {report['warnings']} warnings, "
        f"{report.get('acknowledged', 0)} acknowledged"
    )
    for finding in report["findings"]:
        print(f"  [{finding['level']}] {finding['page']} :: {finding['rule']} :: {finding['message']}")


if __name__ == "__main__":
    main()
