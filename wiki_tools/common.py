"""Shared helpers for the trading operations wiki tooling.

Paths default to locations relative to the project directory but can be
overridden with environment variables so the same code runs inside a
container with mounted volumes:

- WIKI_OUTPUTS_DIR  (raw operational CSV/JSON, read-only)
- WIKI_RAW_DIR      (immutable git-tracked source snapshot, read-only)
- WIKI_DIR          (maintained wiki pages, writable)
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

SCHEMA_VERSION = 1

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _dir_from_env(var: str, default: Path) -> Path:
    return Path(os.environ.get(var, str(default)))


OUTPUTS_DIR = _dir_from_env("WIKI_OUTPUTS_DIR", PROJECT_DIR / "outputs")
RAW_DIR = _dir_from_env("WIKI_RAW_DIR", PROJECT_DIR / "raw")
WIKI_DIR = _dir_from_env("WIKI_DIR", PROJECT_DIR / "wiki")

CONTROL_LATEST_FILE = OUTPUTS_DIR / "control_audit_latest.csv"

# Maps a page type to its wiki subdirectory (mirrors WIKI_SCHEMA.md).
SUBDIRS: dict[str, str] = {
    "incident": "incidents",
    "control_rule": "control_rules",
    "strategy_thesis": "strategy_theses",
    "regime_note": "regime_notes",
    "playbook": "playbooks",
    "decision_log": "decision_logs",
    "weekly_summary": "weekly_summaries",
}

# Plain-language labels for control tokens. Kept in sync with dashboard.py so
# the wiki and the operator dashboard describe holds identically.
CONTROL_REASON_LABELS: dict[str, str] = {
    "monitor_stale": "Worker heartbeat is stale",
    "monitor_stale_hold": "Trading held because the worker heartbeat is stale",
    "performance_stale": "Performance data is stale",
    "broker_auth_unauthorized": "Broker authentication failed (401 Unauthorized)",
    "concentration_breach": "Position concentration limit breached",
    "error_count_24h": "Elevated execution errors in the last 24 hours",
    "blocked_count_24h": "Too many blocked orders in the last 24 hours",
    "recent_expired_48h": "Recently expired contracts detected",
    "protective_exit_fill_rate": "Protective exit fill rate is below the minimum",
    "missing_required_inputs": "Required input files are missing or empty",
}

# Reasons that must block trading rather than act as soft warnings.
HARD_BLOCK_REASONS = {
    "broker_auth_unauthorized",
    "concentration_breach",
    "missing_required_inputs",
}


def humanize_reason(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    return CONTROL_REASON_LABELS.get(token, token.replace("_", " ").capitalize())


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ensure_dirs() -> None:
    for sub in SUBDIRS.values():
        (WIKI_DIR / sub).mkdir(parents=True, exist_ok=True)
    (WIKI_DIR / "history").mkdir(parents=True, exist_ok=True)


def page_path(page_type: str, page_id: str) -> Path:
    return WIKI_DIR / SUBDIRS[page_type] / f"{page_id}.md"


def next_sequence(page_type: str, date: str) -> int:
    """Next 1-based sequence number for a time-series page on a given date."""
    prefix = f"{page_type}_{date}_"
    directory = WIKI_DIR / SUBDIRS[page_type]
    existing = list(directory.glob(f"{prefix}*.md")) if directory.exists() else []
    return len(existing) + 1


def write_page(page_type: str, page_id: str, data: dict[str, Any]) -> Path:
    """Write a wiki page as YAML with a leading provenance comment."""
    target = page_path(page_type, page_id)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    target.write_text(f"# file: {target.name}\n{body}", encoding="utf-8")
    return target


def read_page(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Page is not a YAML mapping: {path}")
    return data


def iter_pages() -> Iterator[tuple[str, Path, dict[str, Any]]]:
    """Yield (page_type, path, data) for every wiki page on disk."""
    for page_type, sub in SUBDIRS.items():
        directory = WIKI_DIR / sub
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                data = read_page(path)
            except Exception:
                data = {"__parse_error__": True}
            yield page_type, path, data


def load_control_latest() -> dict[str, str] | None:
    """Read the single-row control_audit_latest.csv as a dict, or None."""
    return load_csv_row(CONTROL_LATEST_FILE)


def load_csv_row(path: Path) -> dict[str, str] | None:
    """Read the first row of a CSV as a dict, or None if missing/empty."""
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows[0] if rows else None


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read all rows of a CSV as a list of dicts (empty list if missing)."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def top_exposures(limit: int = 3) -> list[dict[str, str]]:
    """Return the largest holdings from exposure_summary.csv by exposure_pct."""
    rows = load_csv_rows(OUTPUTS_DIR / "exposure_summary.csv")

    def _pct(row: dict[str, str]) -> float:
        try:
            return float(row.get("exposure_pct", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    return sorted(rows, key=_pct, reverse=True)[:limit]


def as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def append_log(message: str) -> None:
    log_file = WIKI_DIR / "log.md"
    line = f"- {iso_now()} {message}\n"
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(line)


def build_index() -> dict[str, Any]:
    """Rebuild index.json and index.md from pages on disk. Returns the index."""
    ensure_dirs()
    entries: list[dict[str, Any]] = []
    for page_type, path, data in iter_pages():
        summary_text = (
            data.get("description")
            or data.get("market_state")
            or data.get("thesis")
            or ""
        )
        summary_text = " ".join(str(summary_text).split())
        if len(summary_text) > 220:
            summary_text = summary_text[:217] + "..."
        entry = {
            "id": data.get("id", path.stem),
            "type": page_type,
            "timestamp": data.get("timestamp") or data.get("last_updated") or data.get("week_of"),
            "status": data.get("status", ""),
            "summary": summary_text,
            "links": data.get("links", []) or [],
            "path": str(path.relative_to(WIKI_DIR)).replace(os.sep, "/"),
        }
        # Pass through a couple of typed fields the dashboard reads directly.
        if "regime" in data:
            entry["regime"] = data.get("regime")
        if "confidence" in data:
            entry["confidence"] = data.get("confidence")
        if "asset_class" in data:
            entry["asset_class"] = data.get("asset_class")
        if "decision_type" in data:
            entry["decision_type"] = data.get("decision_type")
        entries.append(entry)

    index = {"schema_version": SCHEMA_VERSION, "generated_at": iso_now(), "pages": entries}
    (WIKI_DIR / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")

    lines = ["# Wiki Index", "", f"Generated {index['generated_at']}", ""]
    for page_type in SUBDIRS:
        group = [entry for entry in entries if entry["type"] == page_type]
        if not group:
            continue
        lines.append(f"## {page_type.replace('_', ' ').title()}")
        for entry in group:
            stamp = f" ({entry['timestamp']})" if entry["timestamp"] else ""
            lines.append(f"- [{entry['id']}]({entry['path']}){stamp}")
        lines.append("")
    (WIKI_DIR / "index.md").write_text("\n".join(lines), encoding="utf-8")
    return index
