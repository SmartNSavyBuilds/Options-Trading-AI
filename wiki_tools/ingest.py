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


def main() -> None:
    common.ensure_dirs()
    page_id = ingest_control_audit()
    common.build_index()
    if page_id:
        print(f"Wrote incident page: {page_id}")
    else:
        print("No incident page written (control clear or already recorded).")


if __name__ == "__main__":
    main()
