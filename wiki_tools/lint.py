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


def _finding(page: str, level: str, rule: str, message: str) -> dict[str, str]:
    return {"page": page, "level": level, "rule": rule, "message": message}


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


def run() -> dict[str, Any]:
    common.ensure_dirs()
    findings: list[dict[str, str]] = []
    page_count = 0
    for page_type, path, data in common.iter_pages():
        page_count += 1
        findings.extend(_check_page(page_type, path, data))

    errors = sum(1 for f in findings if f["level"] == "error")
    warnings = sum(1 for f in findings if f["level"] == "warn")
    report = {
        "schema_version": common.SCHEMA_VERSION,
        "generated_at": common.iso_now(),
        "pages_checked": page_count,
        "errors": errors,
        "warnings": warnings,
        "findings": findings,
    }
    (common.WIKI_DIR / "lint_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    common.append_log(f"lint: {page_count} pages, {errors} errors, {warnings} warnings")
    return report


def main() -> None:
    report = run()
    print(
        f"Linted {report['pages_checked']} pages: "
        f"{report['errors']} errors, {report['warnings']} warnings"
    )
    for finding in report["findings"]:
        print(f"  [{finding['level']}] {finding['page']} :: {finding['rule']} :: {finding['message']}")


if __name__ == "__main__":
    main()
