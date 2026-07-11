# LLM Wiki Schema for Trading Operations

## Overview
This schema defines the structure, fields, and constraints for pages in the trading operations wiki. 
The wiki is organized into seven page types. Pages are **append-only history with a defined mutable surface**: 
once written, a page is versioned in git and never rewritten wholesale, but a small set of named fields may be 
updated in place per page type (see "Mutable Fields" under each type). All other fields are immutable.

---

## Global Mandatory Fields (all page types)

Every page, regardless of type, must include these three fields:

- `schema_version`: Integer schema version this page was written against (current: `1`). Lets the linter evolve rules without breaking old pages.
- `id`: Stable unique identifier for the page, independent of filename (e.g., `incident_2026-07-10_001`). Ingest and lint key off this, never off filename parsing.
- `links`: List of related page ids (may be empty `[]`). Satisfies orphan detection and lets any page point to its thesis, playbook, or source incident.

---

## Page Types

### 1. **Incident** 
*When a control check fails or an execution event requires capture*

**Purpose**: Record what happened, why it mattered, and how it was resolved.

**Mandatory Fields**:
- `timestamp`: ISO 8601 (when the incident occurred)
- `incident_type`: One of `control_miss`, `broker_error`, `market_dislocation`, `decision_override`
- `status`: One of `open`, `resolved`, `acknowledged_risk`
- `control_reason` (if control_miss): Token from daily_control_check.py (e.g., `protective_exit_fill_rate`)
- `description`: 2-3 sentences. Plain language summary.
- `raw_sources`: List of files from raw/ that triggered this incident
- `resolution`: What was done or decided (empty if still open)
- `owner`: Who acknowledged or resolved this

**Lint Rules**:
- Staleness: Flag if `status=open` and `timestamp` > 24h old (red alert)
- Citation: `raw_sources` must be non-empty (machine-checked). Prose-level citation in `description` is a soft/manual check.
- Uniqueness: No duplicate incidents with same timestamp + incident_type (flag as merge)
- Resolution fill: If status=resolved, must have non-empty `resolution` field

**Mutable Fields**: `status`, `resolution`, `links`. All other fields immutable once written.

**Example**:
```yaml
# file: incident_2026-07-10_001.md
schema_version: 1
id: incident_2026-07-10_001
timestamp: 2026-07-10T14:23:45Z
incident_type: control_miss
status: resolved
control_reason: protective_exit_fill_rate
description: |
  Protective exit orders filled at only 45% of target on SPY straddle.
  Dashboard showed monitor_status.csv updating, but pre_live_gate_check.py 
  detected fill rate below 70% threshold at 2026-07-10T14:15:00Z.
  Root cause: Alpaca API rate limit hit during market open.
raw_sources:
  - outputs/execution_log.csv (rows 1204-1208)
  - outputs/monitor_status.csv (snapshot 2026-07-10T14:20:00Z)
resolution: |
  Added exponential backoff to Alpaca order submission.
  Verified fill rate recovered to 92% on next cycle.
  No position loss; protective hedges held.
owner: system
links:
  - control_rule_protective_exit_fill_rate
  - playbook_broker_rate_limit
```

---

### 2. **Control Rule**
*Definition of a single control check and its purpose*

**Purpose**: Make implicit checks explicit. Help future devs understand why a control exists.

**Mandatory Fields**:
- `check_id`: Short identifier (e.g., `protective_exit_fill_rate`, `broker_auth_status`)
- `check_type`: One of `hard_block`, `soft_hold`, `warning`
- `description`: What does this check measure? 1 paragraph.
- `threshold_or_condition`: The exact logic being tested (e.g., "fill rate < 70%" or "response code != 200")
- `consequence_if_triggered`: What happens when this check fails (e.g., "trading halts until resolved")
- `data_source`: Which CSV/JSON file provides the signal (e.g., monitor_status.csv, broker_account_status.csv)
- `last_review`: ISO 8601 date when this rule was last reviewed for relevance

**Lint Rules**:
- Staleness: Flag if `last_review` > 90 days old (quarterly review requirement)
- Source existence: Verify `data_source` file exists in outputs/
- Consequence clarity: Flag vague language in `consequence_if_triggered` (must be specific)

**Mutable Fields**: `last_review`, `threshold_or_condition`, `links`. Core identity fields immutable.

**Example**:
```yaml
# file: control_rule_protective_exit_fill_rate.md
schema_version: 1
id: control_rule_protective_exit_fill_rate
check_id: protective_exit_fill_rate
check_type: soft_hold
description: |
  Protective exit orders are the first line of defense against gap risk.
  If more than 30% of protective orders remain unfilled after 5 minutes,
  the market may be moving faster than our execution, or the spread 
  may have widened beyond profitability. This check detects that condition.
threshold_or_condition: |
  (filled_exit_shares / total_exit_shares) < 0.70
  Measured from monitor_status.csv every 2 minutes.
consequence_if_triggered: |
  Pre-live gate will fail. Trading will not proceed until either:
  (a) remaining protective orders fill within the next cycle, OR
  (b) operator explicitly overrides with decision_log entry explaining rationale
data_source: outputs/monitor_status.csv (columns: filled_exit_shares, total_exit_shares)
last_review: 2026-07-10
links: []
```

---

### 3. **Strategy Thesis**
*Market regime and high-level trading conviction*

**Purpose**: Capture the "why we are taking this bet" in one place. Helps catch drift and contradictions.

**Mandatory Fields**:
- `regime`: Name of current market regime (e.g., "high_volatility_reversion", "earnings_rotation", "vix_crush")
- `confidence`: Numeric 0-100 (our current conviction in this regime)
- `thesis`: 3-5 sentences. The core market view.
- `key_signals`: List of 3-5 observable indicators that support this thesis
- `contradictions`: List of any conflicting signals; how we're handling the conflict
- `positioning`: Current net exposure and asset mix (e.g., "70% long calls, 30% short strangles on QQQ")
- `review_cadence`: How often this thesis is reviewed (daily, weekly, as-needed)
- `last_updated`: ISO 8601 date when this thesis was last reviewed

**Lint Rules**:
- Signal citation: Each signal in `key_signals` must have a corresponding entry in regime_note pages (or be marked "TBD")
- Contradiction handling: Cannot have empty `contradictions`; must either list conflicts or write "none identified as of {date}"
- Freshness: Flag if `last_updated` > 7 days old (requires re-review)

**Mutable Fields**: `confidence`, `positioning`, `contradictions`, `last_updated`, `links`. `regime` and `thesis` immutable (a new regime = a new thesis page).

**Example**:
```yaml
# file: strategy_thesis_vix_crush_2026_q3.md
schema_version: 1
id: strategy_thesis_vix_crush_2026_q3
regime: vix_crush
confidence: 72
thesis: |
  VIX mean-reversion toward 16-18 is likely as July rate cut expectations 
  stabilize. Short volatility positions (iron condors, short strangles) 
  profit from both theta decay and downward VIX moves. We are underweight 
  long calls and tilted short premium on SPY 0DTE and SPX weeklies.
key_signals:
  - VIX term structure is in contango (front month 18, back month 22)
  - Fed fund futures show stable 75bp cuts for remainder of year (vs 100bp priced 2 weeks ago)
  - Put/call ratio on SPY is elevated at 1.3, suggesting fear premium is richening
  - IV rank in SPY options is 65th percentile (elevated but not extreme)
positioning: |
  SPY: 70% short strangles (16-delta collars), 20% short 0DTE calls, 10% long put spreads for tail hedge
  QQQ: 50% short iron condors, 50% long calls for earnings protection
  VIX: 100% short calls (VXX calls, 20+ delta)
contradictions: |
  Earnings season volatility could cause regime flip. We are monitoring
  earnings calendar for large cap tech (MSFT, AAPL, GOOGL in July). 
  If earnings surprise > 3%, we will reduce short call exposure by 50%.
last_updated: 2026-07-09
review_cadence: daily
links:
  - regime_note_2026-07-10
```

---

### 4. **Regime Note**
*Daily or ad-hoc update on market conditions*

**Purpose**: Timestamped snapshot of current market environment. Feed for incident detection.

**Mandatory Fields**:
- `timestamp`: ISO 8601 (when this note was written)
- `market_state`: Short 1-liner describing today (e.g., "Fed pause, earnings calm, VIX stable")
- `vix_level`: Current VIX close and 5-day trend (e.g., "17.2, down 1.1%")
- `sector_movers`: 2-3 biggest movers today with one-sentence rationale
- `data_sources`: List of files/APIs queried to write this note (e.g., monitor_status.csv, Alpaca account endpoint)
- `action_taken`: Any positions adjusted or theses updated? (empty if no change)

**Lint Rules**:
- Freshness: Flag if today's regime_note does not exist by market close
- Data source validation: Verify listed sources exist and were updated within past 4 hours
- Obsolescence: Archive regime_note older than 30 days to history/ folder

**Mutable Fields**: `links` only. Regime notes are point-in-time snapshots; content is otherwise immutable.

**Example**:
```yaml
# file: regime_note_2026-07-10.md
schema_version: 1
id: regime_note_2026-07-10
timestamp: 2026-07-10T16:45:00Z
market_state: Fed patience, earnings calendar light, VIX stable 16.8
vix_level: 16.8 (close), up 0.3% on session
sector_movers: |
  - Energy sector up 2.1% (crude oil rally on supply concerns)
  - Tech sector flat (MSFT -0.2%, AAPL +0.1%, mixed earnings support)
  - Utilities up 0.8% (yield-driven, 10Y stable at 4.1%)
data_sources:
  - Alpaca real-time quote stream (AAPL, MSFT, SPY, QQQ)
  - monitor_status.csv snapshot 2026-07-10T16:30:00Z
  - execution_log.csv (daily close)
action_taken: |
  Reduced QQQ short call exposure by 10% due to earnings calendar compression.
  No position adds or closes. Portfolio remains 68% net short volatility.
links:
  - strategy_thesis_vix_crush_2026_q3
```

---

### 5. **Playbook**
*Standardized response to a known incident class*

**Purpose**: When control X fails, what do we do? Captures tribal knowledge.

**Mandatory Fields**:
- `incident_class`: Which incident types trigger this playbook (e.g., `broker_error`, `control_miss`)
- `trigger_conditions`: When should this playbook activate? (e.g., "broker returns 429 rate limit error")
- `steps`: Numbered list of actions to take in order
- `owner`: Who is responsible for executing this playbook
- `created_date`: ISO 8601 when playbook was written
- `last_tested`: ISO 8601 when this playbook was last verified to work

**Lint Rules**:
- Steps clarity: Each step must be actionable (flag vague language like "investigate")
- Staleness: Flag if `last_tested` > 30 days old (needs re-validation)
- Ownership: `owner` must be a known system account or team role

**Mutable Fields**: `steps`, `last_tested`, `owner`, `links`. `incident_class` and `created_date` immutable.

**Example**:
```yaml
# file: playbook_broker_rate_limit.md
schema_version: 1
id: playbook_broker_rate_limit
incident_class: broker_error
trigger_conditions: |
  Alpaca API returns HTTP 429 (rate limit exceeded) on order submission
  for 3+ consecutive attempts within 60 seconds.
steps:
  1. Pause all new order submissions immediately (canary switch to false)
  2. Log the rate limit event to execution_log.csv with timestamp and retry count
  3. Back off exponentially: wait 2s, then 4s, then 8s (max 60s between retries)
  4. Resume order submissions only after 60s have passed since last 429
  5. If broker still returning 429 after 3 backoff cycles (total ~120s), halt trading via pre_live_gate override
  6. Page on-call engineer via alert system
owner: system
created_date: 2026-06-15
last_tested: 2026-07-08
links: []
```

---

### 6. **Decision Log**
*Record of a deliberate choice to trade, override a control, or change strategy*

**Purpose**: Audit trail for "why did we do this?" Helps identify patterns in decision-making.

**Mandatory Fields**:
- `timestamp`: ISO 8601 (when decision was made)
- `decision_type`: One of `trade`, `override_control`, `strategy_shift`, `investigation_result`
- `description`: What decision was made? 2-3 sentences.
- `rationale`: Why this decision? Reference to regime_note, incident, or thesis if applicable.
- `owner`: Who made this decision?
- `expected_outcome`: What do we expect to happen if this decision plays out as planned?
- `decision_id`: Unique identifier for this decision (e.g., `dec_2026-07-10_001`)

**Lint Rules**:
- Rationale linkage: If rationale references a regime_note or incident, verify that page exists
- Outcome tracking: Flag decisions older than 7 days without an outcome update (needs follow-up)

**Mutable Fields**: `links` only. Decisions are a permanent audit record; add a follow-up decision_log rather than editing.

**Example**:
```yaml
# file: decision_log_2026-07-10_001.md
schema_version: 1
id: dec_2026-07-10_001
timestamp: 2026-07-10T11:30:00Z
decision_type: override_control
description: |
  Pre-live gate failed due to protective_exit_fill_rate control.
  Decision: Override gate and proceed with SPY short strangles.
rationale: |
  Incident_2026-07-10_control_miss_001 root cause was transient API rate limit
  (now resolved via exponential backoff). Fill rate has recovered to 90%+ on
  last 3 cycles. Market timing (VIX inflection point) justifies the risk.
owner: trader
expected_outcome: |
  Protective orders should fill within 4 minutes. Position will scale to target
  70 delta SPY short strangles. P&L expected to be +0.3% to +0.8% by market close
  if SPY stays within 490-495 range.
decision_id: dec_2026-07-10_001
links:
  - incident_2026-07-10_001
```

---

### 7. **Weekly Summary**
*Meta-view of the week: wins, losses, lessons*

**Purpose**: Force reflection. Identify patterns and themes for strategy refinement.

**Mandatory Fields**:
- `week_of`: ISO 8601 Monday date (e.g., "2026-07-07")
- `net_pnl`: Total profit/loss for the week as a number (no sign symbols or units in the value)
- `currency`: Currency/unit for `net_pnl` (e.g., `USD`). Keeps multi-asset expansion unambiguous.
- `win_ratio`: Numeric 0-1 (trades closed profitably / total trades closed)
- `trades_closed`: Integer total number of trades closed this week
- `biggest_win`: Description of the single best trade (1 sentence)
- `biggest_loss`: Description of the single worst trade (1 sentence)
- `themes`: 3-4 bullet points of patterns observed (e.g., "morning gaps are mean-reverting, earnings IV crush is real, broker latency spikes on economic data")
- `regime_transitions`: Did the market regime change mid-week? Document any thesis updates.
- `control_incidents`: Summary of all control misses or broker errors (reference incident pages)
- `upcoming_week`: Key calendar events or regime risks for next week
- `author`: Who wrote this summary

**Lint Rules**:
- Completeness: All required fields must be non-empty
- Freshness: One and only one weekly_summary per calendar week (flag duplicates)
- Incident linkage: All incidents mentioned must have corresponding incident pages

**Mutable Fields**: `links` only. Weekly summaries are a permanent record.

**Example**:
```yaml
# file: weekly_summary_2026-07-07.md
schema_version: 1
id: weekly_summary_2026-07-07
week_of: 2026-07-07
net_pnl: 1240
currency: USD
win_ratio: 0.62
trades_closed: 21
biggest_win: |
  VIX mean-reversion short strangles on Wed 07-09. 
  Entered at VIX 18.5, exited at 16.2. +$480 on 1 contract.
biggest_loss: |
  Long call spread on AAPL earnings Tuesday. 
  AAPL beat but guided down. Lost -$220 on position size.
themes:
  - "Morning gaps remain highly mean-reverting (5 out of 6 reversals within 30 min)"
  - "Broker latency spikes during 8:30-9:00 ET economic data (affected 2 orders)"
  - "Short premium on VIX at 16-18 level is very profitable (theta decay accelerating)"
  - "Earnings IV crush is real but timing the collapse is difficult (1 win, 2 losses)"
regime_transitions: |
  No major regime shift. VIX crush thesis held all week.
  Adjusted positioning from 70% short premium to 75% (added VXX short calls).
control_incidents:
  - incident_2026-07-10_control_miss_001 (protective exit fill rate, resolved)
  - incident_2026-07-08_broker_error_001 (rate limit, resolved)
upcoming_week:
  - FOMC minutes Wed 07-16 (likely volatility spike, will reduce size 1hr before)
  - Tech earnings resume Tue 07-15 (MSFT, AAPL premarket)
  - Jobs report Fri 07-19 (traditional risk event, plan for 2-3% market move)
author: system
links:
  - incident_2026-07-10_001
```

---

## Lint Rules (Global)

Applied to all page types:

1. **Citation Validation**: Machine-checked — `raw_sources` must be non-empty for incident and regime_note pages. Prose-level citation of claims is a soft/manual check, not auto-enforced.
2. **Orphan Detection**: Every page must be referenced from index.md or linked from at least one other page (via `links`)
3. **Timestamp Consistency**: Page timestamps must be valid ISO 8601 and cannot be future-dated
4. **Field Completeness**: All mandatory fields must be non-empty (no null, no "TBD" without date target)
5. **Stale Content Detection**: 
   - Incidents > 24h old with status=open → red flag
   - Theses > 7d old without review → yellow flag
   - Regime notes > 30d old → archive to history/
   - Control rules > 90d since review → schedule review
6. **Contradiction Detection** (layered, escalating cost):
   - Hard rule (every ingest, cheap): If newest `regime_note` regime name differs from the active `strategy_thesis.regime`, flag.
   - Numeric drift (every ingest, cheap): If VIX or positioning moves past a defined threshold vs. thesis assumptions, flag.
   - LLM semantic pass (weekly only, expensive): Semantic contradiction check during the weekly cycle to keep token cost down.
   - Any flag requires a `decision_log` entry to clear. Start warn-only.
7. **Naming Consistency**: Filenames follow these patterns by category:
   - Time-series pages (incident, decision_log): `{type}_{YYYY-MM-DD}_{seq}.md`
   - Daily singletons (regime_note, weekly_summary): `{type}_{YYYY-MM-DD}.md`
   - Named singletons (control_rule, strategy_thesis, playbook): `{type}_{slug}.md`
   - Filenames never embed the record's own type value (e.g., no `control_miss` in an incident filename); the `id` field is the stable key.
8. **Schema Version Check**: Every page must carry `schema_version`; lint rejects pages missing it and warns when a page lags the current version.

---

## Directory Structure

```
wiki/
  index.md                              # Table of contents, entry point
  index.json                            # Generated query index (id, type, timestamp, status, links)
  incidents/                            # incident_YYYY-MM-DD_NNN.md
  control_rules/                        # control_rule_{slug}.md
  strategy_theses/                      # strategy_thesis_{slug}.md
  regime_notes/                         # regime_note_YYYY-MM-DD.md
  playbooks/                            # playbook_{slug}.md
  decision_logs/                        # decision_log_YYYY-MM-DD_NNN.md
  weekly_summaries/                     # weekly_summary_YYYY-MM-DD.md
  history/                              # Archived regime_notes older than 30d
  lint_report.json                      # Latest lint run output (auto-generated)
  log.md                                # Ingest and lint operation log

raw/
  (symlink or direct copy of outputs/)  # Read-only, git-tracked, immutable
```

---

## Sequencing for MVP

**First cycle (Fri 2026-07-10)**:
1. Create wiki/ and raw/ directories
2. Run ingest.py: read control_audit_latest.csv, write one incident page
3. Run lint.py: validate syntax and citations
4. Wire to dashboard: show incident summary on Execution Desk tab

**Second cycle (Mon 2026-07-13)**:
1. Ingest: write regime_note based on market open data
2. Ingest: update strategy_thesis if market conditions changed
3. Lint: check for stale incidents, new contradictions
4. Dashboard: display regime summary and thesis confidence

**Third cycle (Fri 2026-07-17)**:
1. Ingest: archive week's incidents
2. Ingest: write weekly_summary
3. Lint: full pass with contradiction detection
4. Review: operator reviews summary and logs lessons

---

## Review Decisions (Locked, schema_version 1)

Resolved during the schema review. These are settled unless a future version bumps `schema_version`.

1. **Page naming**: Category-based patterns (see Global Lint Rule 7). Filenames never embed the record's type value; `id` is the stable key.
2. **Mandatory fields**: Added `schema_version`, `id`, and `links` as global fields on every page to unblock ingest/lint and orphan detection.
3. **Lint thresholds**: 24h incidents, 7d theses, 90d rules confirmed. Regime notes archive at 30d (was 1d) to preserve trend context.
4. **Contradiction detection**: Layered — cheap hard/numeric rules on every ingest, expensive LLM semantic pass weekly only. Flags clear via `decision_log`. Warn-only to start.
5. **Query performance**: grep over `wiki/` for MVP; generated `index.json` in phase 2; semantic search deferred until corpus size justifies it.

**Immutability model**: Append-only history with a defined mutable surface per page type (see each type's "Mutable Fields"). Citation lint is machine-checked only as `raw_sources` non-empty; prose citation is a manual check.
