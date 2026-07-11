# Immutable Raw Source Layer

This folder is the **read-only, git-tracked snapshot** of operational outputs that
the wiki ingest reads from. It is part of the LLM wiki's three-layer design:

1. **raw/** (this folder) — immutable source snapshots. No automation writes here.
2. **wiki/** — maintained markdown pages produced by `wiki_tools/ingest.py`.
3. **WIKI_SCHEMA.md** — the rules that govern page structure and linting.

## Rules

- Nothing in the automated pipeline writes to this folder. Ingest and lint mount
  it read-only.
- Snapshots are committed to git so every wiki page can cite an immutable source.
- To capture a snapshot, copy the relevant files from `outputs/` here and commit
  them, e.g. `cp outputs/control_audit_latest.csv raw/` then commit.

For the MVP, ingest reads directly from `outputs/` (mounted read-only). This
folder becomes the durable citation target as the wiki matures.
