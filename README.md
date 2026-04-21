# Options Trading AI — Autonomous Research & Execution Dashboard

> **Paper-first autonomous trading system** — signal scanning, options candidate ranking, risk management, broker execution, and a professional multi-tab research dashboard. Built end-to-end in Python with Alpaca broker integration.

---

## What This Does

This project runs a continuous autonomous pipeline that:

1. **Scans the market** — scores equities and ETFs using a multi-factor signal engine (trend, momentum, RSI, volume, relative strength)
2. **Ranks options candidates** — selects structure (long call, bull call spread, bear put spread, etc.) based on signal conviction and volatility
3. **Manages risk** — enforces exposure caps, sector limits, correlation buckets, and stress scenarios before any order enters the queue
4. **Executes paper trades autonomously** — routes approved ideas to an Alpaca paper account on a 15-minute cycle
5. **Monitors exits** — evaluates stop-loss, take-profit, and time-decay thresholds; auto-approves urgent exits
6. **Journals performance** — tracks every open position's P&L, execution style, and decision context
7. **Surfaces intelligence** — congressional disclosures feed, catalyst news, market regime analysis, and a crypto watchlist inside a Streamlit dashboard

---

## Dashboard Preview

| Tab | What's Inside |
|-----|---------------|
| **Command Center** | Ranked options ideas, signal conviction gauge, position timeline |
| **Opportunity Radar** | Multi-factor scatter charts, sector heatmap, short-side watchlist |
| **Risk Lab** | Exposure summary, sector/correlation caps, stress test scenarios |
| **Advisor** | Per-ticker thesis, exit plan, opportunity discovery feed, news & catalyst alerts |
| **Congress** | Congressional disclosure watchlist, large-trade directory, member activity |
| **📰 News** | Breaking news alerts with position impact callouts (manually injectable + automated feed) |
| **Execution Desk** | Broker state, queue control, execution log, approval workflow, monitor status |

---

## Tech Stack

- **Python 3.11+** — signal engine, execution layer, risk guardrails, performance journal
- **Streamlit** — multi-tab research dashboard
- **Plotly** — interactive charts (scatter, gauge, bar, timeline)
- **Alpaca Markets API** — paper broker execution, position sync, order management
- **yfinance** — market data ingestion
- **pandas / numpy** — data pipeline and signal computation
- **pytest** — 39-test suite covering signal logic, exit rules, execution preview, risk guardrails

---

## Architecture

```
options_trading_ai/
├── market_monitor.py          # Autonomous loop — runs every 15 min
├── app.py                     # Signal scanner entry point
├── discover_opportunities.py  # Broader opportunity sourcing
├── evaluate_exit_rules.py     # Exit threshold evaluation
├── execute_exit_trades.py     # Autonomous exit execution
├── execute_paper_trades.py    # Autonomous entry execution
├── paper_trade.py             # Queue builder with guardrails
├── performance_journal.py     # Open position tracking
├── dashboard.py               # Streamlit research dashboard
├── src/
│   ├── signal_engine.py       # RSI, momentum, trend, volume scoring
│   ├── exit_manager.py        # Exit rule logic and option metadata parsing
│   ├── execution.py           # Broker-safe execution layer (paper-first)
│   ├── options_selector.py    # Expiration targeting and structure selection
│   ├── risk_guardrails.py     # Exposure, sector, and stress test controls
│   ├── performance_journal.py # Journal generation and strategy attribution
│   └── ...
└── tests/
    └── test_trading_enhancements.py  # 39 unit tests
```

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/YOUR_USERNAME/options-trading-ai.git
cd options-trading-ai/projects/options_trading_ai
pip install -r requirements.txt

# 2. Configure broker credentials
cp .env.example .env
# Edit .env — add your Alpaca paper API keys

# 3. Run a single monitor cycle
python market_monitor.py

# 4. Start the autonomous loop (every 15 minutes)
python market_monitor.py --loop --interval-seconds 900

# 5. Launch the dashboard
streamlit run dashboard.py
```

---

## Signal Engine

The scoring model evaluates each ticker across 10+ independent factors:

| Factor | Bullish Signal | Bearish Signal |
|--------|---------------|----------------|
| Trend vs SMA-10 / SMA-20 | Price above both | Price below both |
| 5-day momentum | Positive | Negative |
| RSI (14-period) | > 60 | < 40 |
| 1-month relative return | Positive | Negative |
| Distance from 20-day high/low | Near high | Near low |
| Up/down day ratio (10-day) | > 60% up | > 60% down |
| Volume vs 20-day average | Elevated | Elevated (breakdown) |

Scores are aggregated into a net signal score. Structure is selected based on score magnitude and annualized volatility:
- Score ≥ 3 + vol > 25% → **Bull call spread**
- Score ≥ 3 + vol ≤ 25% → **Long call**
- Score ≤ -3 + vol > 25% → **Bear put spread**
- Score ≤ -3 + vol ≤ 25% → **Long put**

---

## Risk Controls

Before any trade enters the paper queue:
- **Single-name exposure cap** — max % of portfolio in one ticker
- **Total portfolio exposure cap** — max % deployed at once
- **Sector concentration limit** — prevents over-weighting one sector
- **Correlation bucket limit** — prevents correlated name overloading
- **Stress test scenarios** — estimates P&L impact of -5%, -10%, -20% shocks

---

## Testing

```bash
cd projects/options_trading_ai
python -m pytest tests/ -v
# 39 tests covering: signal engine, exit rules, execution preview,
# risk guardrails, congressional data, performance journal
```

---

## Paper Trading Status

This system is currently in **paper trading mode** accumulating forward performance data across multiple market regimes. Live deployment will only be considered after sufficient paper trade history validates signal expectancy and drawdown characteristics.

---

## README Index by Category

### Product Overview
- [Quick Start](#quick-start)
- [Latest Additions](#latest-additions)
- [Purpose](#purpose)
- [Honest Competitive Analysis](#honest-competitive-analysis)

### Trading Workflow and Features
- [How To Use The New Features](#how-to-use-the-new-features)
- [Can the Program Buy and Sell Stocks or Options Autonomously?](#can-the-program-buy-and-sell-stocks-or-options-autonomously)
- [Is Something Constantly Monitoring the Market?](#is-something-constantly-monitoring-the-market)
- [Performance Journal](#performance-journal)
- [Immediate Next Steps](#immediate-next-steps)

### Research, Risk, and Model Design
- [Improvement Backlog To Raise The Ceiling](#improvement-backlog-to-raise-the-ceiling)
- [Mathematical Foundations](#mathematical-foundations)
- [Data Sources the System Should Use](#data-sources-the-system-should-use)
- [Risk Management Rules](#risk-management-rules)
- [Validation Plan](#validation-plan)

### Deployment and Product Direction
- [Practical MVP Plan](#practical-mvp-plan)
- [Step-by-Step Deployment Plan Toward Full Functionality](#step-by-step-deployment-plan-toward-full-functionality)
- [Business Model if This Project Were Marketed](#business-model-if-this-project-were-marketed)
- [Can This Become a Web App or Mobile App?](#can-this-become-a-web-app-or-mobile-app)

## Glossary
- Signal Score: the model’s conviction reading for a setup.
- Rank Score: the overall ordering metric used to sort ideas.
- Paper Trading: simulated broker execution using a non-live account.
- Proxy Equity Order: a stock order used to validate the execution pipeline safely.
- Execution Desk: the dashboard area for queue control, broker state, alerts, and monitoring.
- Performance Journal: the running log of open positions, unrealized profit or loss, and recent execution context.
- Exit Guardrails: automated rules that flag or approve urgent exit actions in paper mode.
- Exposure Guardrails: limits that prevent the queue from over-concentrating capital in one name or across the full paper portfolio.

## Quick Start

This project is now started as a working MVP scaffold.

Current starter files:
- [projects/options_trading_ai/app.py](projects/options_trading_ai/app.py) — runs the signal scanner
- [projects/options_trading_ai/run_backtest.py](projects/options_trading_ai/run_backtest.py) — runs the backtest summary
- [projects/options_trading_ai/paper_trade.py](projects/options_trading_ai/paper_trade.py) — builds the paper-trade review queue
- [projects/options_trading_ai/execute_paper_trades.py](projects/options_trading_ai/execute_paper_trades.py) — prepares broker-ready paper execution previews and logs
- [projects/options_trading_ai/dashboard.py](projects/options_trading_ai/dashboard.py) — launches the professional research dashboard
- [projects/options_trading_ai/requirements.txt](projects/options_trading_ai/requirements.txt) — project dependencies
- [projects/options_trading_ai/src/options_selector.py](projects/options_trading_ai/src/options_selector.py) — proposes expiration targets and option structures
- [projects/options_trading_ai/src/execution.py](projects/options_trading_ai/src/execution.py) — broker-safe execution layer for paper-first automation

To begin, the system needs to do three things well:
- pull clean market data
- score directional setups consistently
- test whether those signals had any historical edge

## Latest Additions

New additions now included in the project:
- an options candidate file with projected cost, projected profit, and projected return percentage
- a sizing hint that estimates how much of risk capital to allocate to each idea
- a professional dashboard refresh with risk, radar, and autonomy views
- a paper-trade queue for approved ideas
- a manual-or-automatic approval workflow toggle before broker submission
- broker account and position syncing for operational monitoring
- non-duplicate queue filtering so fresh ideas are surfaced instead of already-open names
- ready-ideas versus ready-ideas-executed dashboard panels with date filtering
- a broker-safe execution layer that defaults to paper mode and preview-only behavior
- a guarded paper-exit workflow with explicit approval state and preview generation
- a lower-intervention urgent-exit guardrail and alerts feed for paper monitoring
- exposure, sector, and correlation guardrails for the paper trade queue
- a position-by-position performance journal for ongoing paper-trade review
- a stronger short-side ranking model for bearish setups
- a congressional disclosure importer that normalizes raw exports into the dashboard summary
- a richer Congress page with a public-interest watchlist, recent big-trade filters, and a member directory
- a cleaner command-center dashboard layout with clearer workflow, top-level desk cards, and faster operator review
- a deeper risk desk with sector and correlation caps, stress scenarios, and escalation-aware alerts
- a multi-asset research layer with crypto watchlists and market-regime classification
- advanced operator analytics covering strategy attribution and execution quality
- a structured mathematical learning guide for deeper study

## Project Update Log

### 2026-04-16
- signal scanner created
- backtest summary created
- options candidate selector added
- projected profit estimator added
- dashboard scaffold launched
- professional dashboard upgrade added
- paper-trade queue added
- approval gate and broker monitoring added
- paper-first execution layer added for broker routing
- educational mathematics guide expanded

### 2026-04-17
- live performance journal added
- priority alerts feed added
- green executed or open indicators added to ranked ideas
- urgent paper exit guardrails added
- exposure and concentration caps added to the queue builder
- Congress page upgraded with a recent large-trade directory and public-interest watchlist
- Phase 1 dashboard polish added through a clearer command-center workflow and top-level desk cards
- Stage 2 risk controls added through sector and correlation guardrails, stress scenarios, and escalation-aware alerts
- Phase 3 multi-asset expansion added through crypto watchlists and market-regime reporting
- Phase 4 analytics added through strategy attribution and execution-quality summaries
- README index and glossary added

## Purpose

Build an AI-assisted research and execution system for options trading that identifies high-probability setups, manages risk tightly, and improves decision quality through speed, discipline, and broad data coverage.

> Important: no system can honestly guarantee profits. The real goal is to create a repeatable statistical edge with strong risk control.

---

## What Is Being Done Now And The Human Job Titles It Maps To

This build is already covering work that an employer would normally assign to several roles:

### 1. Quant Researcher
Work being done:
- defining measurable trade setups
- translating price behavior into signals
- testing hypotheses instead of relying on opinion

### 2. Data Engineer
Work being done:
- creating the data pipeline
- organizing price history and scanner output
- structuring repeatable inputs for analysis

### 3. Machine Learning Engineer
Work being done:
- designing the scoring logic
- preparing the project for predictive modeling
- structuring the system so it can later learn from outcomes

### 4. Trading Systems Developer
Work being done:
- wiring together scanner, signal engine, and backtester
- turning research into usable workflows
- building a repeatable operating system instead of one-off analysis

### 5. Risk Manager
Work being done:
- forcing trade selection rules
- separating actionable setups from low-quality noise
- building the framework for position sizing and exposure limits

---

## Why This Could Work Better Than Discretionary Trading Alone

Most independent traders are limited by:

- finite attention
- emotional decision-making
- slow reaction time
- weak record-keeping
- inconsistent process
- overconfidence and recency bias

An AI system can outperform that process by:

1. **Watching more markets at once**  
   It can scan thousands of tickers, sectors, options chains, news items, and macro signals continuously.

2. **Using the same rules every time**  
   It does not panic, chase, revenge-trade, or ignore stop rules.

3. **Measuring probabilities instead of telling stories**  
   It can rank trades by expected value, volatility conditions, liquidity, and risk-adjusted return.

4. **Learning from history at scale**  
   It can compare current conditions to thousands of prior setups and estimate what tended to happen next.

5. **Matching the option structure to the forecast**  
   Instead of just buying calls or puts, it can choose spreads, calendars, or premium-selling structures when they fit the edge better.

---

## Honest Competitive Analysis

Short answer: **not yet at hedge-fund level**, and saying otherwise would be misleading.

This system is becoming a strong independent trading and research desk, but it does **not** currently match what established hedge funds or elite quant firms operate with. Those firms have larger research teams, deeper data access, stronger execution infrastructure, better slippage modeling, tighter portfolio construction, and formal risk and compliance layers.

### What this build can realistically compete with today
- many discretionary retail workflows on consistency and speed
- a solo trader or small-team research process
- junior-analyst style screening, monitoring, and idea organization
- paper-trading operations where discipline matters more than raw infrastructure

### What it still needs before it can approach professional quant quality
- long out-of-sample forward performance, not just good-looking signals
- stronger portfolio optimization and exposure controls
- better transaction cost and fill-quality modeling
- richer data coverage, including options microstructure and event data
- production monitoring, deployment, audit, and compliance workflows
- narrower strategy specialization instead of trying to beat every market regime at once

The honest position is this: the project can become **very useful and commercially credible** as a disciplined retail or boutique research platform well before it becomes a true hedge-fund competitor.

## Core Idea

The project should not aim to "predict the market" in a vague way. It should break the task into smaller prediction problems:

- direction over the next 1 to 5 days
- volatility expansion or contraction
- earnings reaction size
- trend continuation vs mean reversion
- probability of moving beyond expected move
- best option structure for the current volatility regime

That is much more realistic and testable.

---

## Checklist of What Has Already Been Created

The project already includes the following working components:

- multi-ticker market scanner
- signal scoring for bullish, bearish, and neutral setups
- options structure suggestions and expiration targeting
- ranked trade candidate output with estimated cost, risk, and projected return
- paper trade approval queue
- Alpaca paper broker sync for account, orders, and positions
- real paper routing for stocks and short-dated single-leg options
- exit recommendation engine for open positions
- congressional disclosure import and summary tools
- Streamlit dashboard with research, broker, and autonomy panels
- execution logging for later model evaluation
- a monitoring script for repeated market checks

## How To Use The New Features

### Stronger short-side ranking
Run the scanner as usual and the newest signal and candidate files will now include bearish-conviction fields such as short rank score, bearish score, and thesis strength.

### Guarded paper exits
1. Refresh recommendations with [projects/options_trading_ai/evaluate_exit_rules.py](projects/options_trading_ai/evaluate_exit_rules.py)
2. Approve selected exits with [projects/options_trading_ai/approve_exit_trades.py](projects/options_trading_ai/approve_exit_trades.py)
3. Route the guarded paper preview with [projects/options_trading_ai/execute_exit_trades.py](projects/options_trading_ai/execute_exit_trades.py)

Exit automation remains gated by both the approval column and the environment safety flag.

### Approval workflow modes
- Manual mode keeps ideas pending until you explicitly approve them in the dashboard queue.
- Automatic mode marks eligible non-duplicate ideas as ready to send while still respecting broker safety checks.
- The Autonomy tab now separates Ready Ideas from Ready Ideas Executed so you can track what is still waiting versus what has already been routed.

### Congressional imports
Drop raw CSV or JSON exports into the folder created at [projects/options_trading_ai/data](projects/options_trading_ai/data), inside the congress_raw subfolder, then run [projects/options_trading_ai/import_congressional_trades.py](projects/options_trading_ai/import_congressional_trades.py).

The dashboard now turns those rows into:
- a recent big-trade directory for the last 7, 14, 30, or 60 days
- a public-interest watchlist that includes names such as Nancy Pelosi for easy monitoring
- a normalized ticker summary so you can connect disclosures to actual tradable symbols faster

### Multi-asset and analytics desk
Run [projects/options_trading_ai/multi_asset_report.py](projects/options_trading_ai/multi_asset_report.py) to refresh the crypto watchlist and market-regime views, and run [projects/options_trading_ai/performance_journal.py](projects/options_trading_ai/performance_journal.py) to regenerate the execution-quality and strategy-attribution feeds.

These additions now give the dashboard:
- a crypto research watchlist for symbols such as BTC, ETH, SOL, XRP, and DOGE
- a regime banner that frames the environment as risk-on, mixed, or defensive
- a strategy attribution table to show which playbook categories are adding value
- an execution-quality summary so fills and routing behavior can be reviewed like a real desk

## Can the Program Buy and Sell Stocks or Options Autonomously?

At the current stage, the answer is **yes in paper mode, with guardrails**.

It can already:
- scan for opportunities
- rank ideas automatically
- route approved stock trades to Alpaca paper
- route approved short-dated single-leg option contracts to Alpaca paper
- generate exit suggestions for open holdings

What it does **not** yet do at full production level is:
- run unattended forever unless the monitoring loop is deployed
- route more advanced multi-leg spreads with institutional-grade controls
- automatically graduate itself to live capital without human approval

## Is Something Constantly Monitoring the Market?

A monitoring workflow now exists through [projects/options_trading_ai/market_monitor.py](projects/options_trading_ai/market_monitor.py).

This means the project can be run as a repeated monitoring service that:
- refreshes signals
- updates opportunity feeds
- rebuilds the paper queue
- syncs broker state
- regenerates exit recommendations

For a true always-on setup, this script should be deployed as a background service or scheduled job on a cloud or local server.

## Improvement Backlog To Raise The Ceiling

Based on the competitive analysis, the highest-value additions are:

### Strategy and modeling upgrades
- regime detection that changes behavior in trending versus choppy markets
- walk-forward model validation with real slippage assumptions
- expectancy tracking by setup type so weak ideas are retired automatically
- portfolio-level ranking so the system chooses the best mix of trades, not just the best single names

### Options and execution upgrades
- smarter contract selection using Greeks, spread width, and open-interest thresholds
- multi-leg spread routing rather than only single-leg paper options
- better fill-quality and order-state reconciliation
- profit-taking and stop automation for option positions, not just stock proxies

### Risk and portfolio upgrades
- sector and correlation caps
- daily drawdown brakes and volatility regime throttling
- exposure dashboards for ticker, sector, and strategy concentration
- capital-allocation logic that adapts to current model confidence

A first version of exposure and concentration guardrails is now active in the paper queue so the system reduces oversized clustering with less manual intervention.

### Product and platform upgrades
- alerts to phone, email, or Discord when high-priority setups appear
- a deployable web app or mobile-friendly control panel
- richer audit logs so every model choice can be explained later
- onboarding and reporting views that make it usable beyond a developer workflow

## Dashboard Direction

The dashboard is intended to remain user friendly by emphasizing:
- ranked trade ideas instead of cluttered raw data
- clear estimates for cost, projected profit, and return percentage
- visual comparison of opportunities across tickers
- filters that let the user quickly focus on the highest-conviction setups
- a dedicated short-side watchlist sorted by bearish conviction rather than being buried below bullish names
- import-ready congressional tracking that feeds directly into the research panels

Primary dashboard file: [projects/options_trading_ai/dashboard.py](projects/options_trading_ai/dashboard.py)

## Mathematical Foundations

The current MVP is using a simplified quantitative framework built from:
- moving-average trend alignment
- short-horizon momentum scoring
- RSI-based strength estimation
- annualized volatility calculations
- expected-move projections
- projected profit relative to estimated capital at risk

For a deeper collegiate-style explanation, see [projects/options_trading_ai/LEARNING_GUIDE.md](projects/options_trading_ai/LEARNING_GUIDE.md).

## Data Sources the System Should Use

### Market Data
- OHLCV price history
- intraday momentum and volume spikes
- sector and index relative strength
- breadth and volatility indicators

### Options Data
- implied volatility
- historical volatility
- IV rank and IV percentile
- open interest
- unusual options flow
- Greeks and skew
- expected move into events

### Fundamental and Event Data
- earnings calendar
- analyst revisions
- SEC filings
- macro releases
- interest-rate expectations

### Sentiment and Alternative Data
- financial news sentiment
- social-media momentum
- search trends
- retail attention spikes
- insider buying or selling

---

## What the Model Would Actually Predict

### Model 1: Regime Detection
Classify the market into regimes such as:
- bullish trend
- bearish trend
- high-volatility panic
- range-bound chop
- post-event digestion

This is critical because the same trade setup behaves differently in different regimes.

### Model 2: Short-Horizon Direction
Predict the probability that a stock or ETF moves up or down over a defined time window.

Output example:
- 62% probability of upward move over next 3 sessions
- confidence score: medium

### Model 3: Volatility Forecast
Estimate whether realized volatility is likely to exceed or underperform implied volatility.

This matters because options pricing depends on volatility, not only direction.

### Model 4: Trade Selection Engine
Choose the structure with the best risk/reward for the forecast:
- long call debit spread for bullish trend with elevated IV
- long put debit spread for bearish breakdown
- calendar spread when near-term IV is rich
- iron condor when range-bound conditions dominate
- straddle or strangle only when expected move is underpriced and a catalyst is near

---

## Why It Could Be Successful

A successful version would have these advantages:

- **better data coverage** than a human researcher
- **faster reaction** to new information
- **consistent filtering** for liquidity and risk
- **objective ranking** of trades by expectancy
- **walk-forward validation** instead of guesswork
- **strict position sizing** that protects capital

The edge would not come from magic. It would come from process quality, speed, breadth, and discipline.

---

## Why It Could Be Better Than Independent Traders

### Independent trader weakness
A typical trader may:
- look at 5 to 20 charts a day
- rely too heavily on intuition
- forget prior mistakes
- overtrade during emotional periods
- misuse options by buying expensive premium in bad volatility conditions

### AI system advantage
A well-built system can:
- rank hundreds or thousands of opportunities daily
- skip low-liquidity setups automatically
- compare implied vs realized volatility objectively
- log every decision and outcome
- improve over time from hard evidence
- avoid bias and fatigue

---

## Project Architecture

### Ingestion Layer
Collect and normalize:
- broker or market data feeds
- options chain snapshots
- news and sentiment data
- calendar and macro data

### Feature Layer
Create predictive features such as:
- momentum scores
- realized vs implied vol spread
- gap behavior around catalysts
- trend persistence measures
- flow imbalance
- volume surprise
- sentiment acceleration

### Modeling Layer
Use an ensemble approach:
- gradient-boosted trees for tabular signals
- sequence models for time-series behavior
- regime classifier for context
- calibration model for probability quality

### Decision Layer
Convert forecasts into actual trade candidates by checking:
- minimum liquidity
- spread width
- expected value
- max risk
- portfolio exposure
- correlation to existing positions

### Execution Layer
Route only the top-ranked trades that pass the filters.

The current implementation intentionally starts with a paper-first execution design:
- build signals and rank candidates
- create a paper-trade review queue
- convert approved ideas into proxy equity orders for safe execution testing
- log execution results and later compare them with expectations
- keep the broker adapter separate from the model so the switch from paper to live is operationally clean

This is the better path for autonomous trading because it validates the full decision-to-broker pipeline now without pretending that options contract routing is already production-ready.

---

## Risk Management Rules

This part matters as much as prediction quality.

- risk only a small fraction of capital per trade
- set maximum daily and weekly drawdown limits
- avoid oversized exposure to one ticker or sector
- do not trade illiquid contracts
- model slippage and commissions in all tests
- reduce size during unstable market regimes
- pause the system if live results diverge materially from backtests

Without this, even a decent model can lose money.

---

## Validation Plan

To be credible, the system must be tested honestly.

### Required validation
- train/test split by time, not random shuffle
- walk-forward testing
- transaction costs included
- slippage included
- delisted names handled properly
- out-of-sample regime changes included

### Metrics to track
- win rate
- average win vs average loss
- expectancy per trade
- profit factor
- Sharpe ratio
- max drawdown
- return on allocated risk

The target is not just a high win rate. The target is **positive expectancy** with survivable drawdowns.

---

## Practical MVP Plan

### Current Build Status
The current MVP foundation now includes:
- a multi-ticker market scanner
- a signal engine that labels names as bullish, bearish, or neutral
- a basic options-structure suggestion layer
- an options candidate selector with expiration targeting
- a first-pass backtest summary tool
- a professional Streamlit dashboard with autonomy and execution views
- a paper-first broker execution layer that is designed to evolve toward live trading

This is the correct fast-start version because it creates a working research loop before adding more complexity.

### Planned Dashboard Later
A visual dashboard can be added next using Plotly Dash or Streamlit to show:
- strongest bullish and bearish names
- recommended options setups by ticker
- backtest win rates and signal quality
- portfolio and exposure visuals


### Phase 1
Build a scanner for liquid names only:
- SPY
- QQQ
- IWM
- NVDA
- AAPL
- MSFT
- TSLA
- AMD
- META
- AMZN

### Phase 2
Predict one thing well:
- next-day direction after a volatility compression and breakout setup

### Phase 3
Add options selection:
- choose between call spread, put spread, or no trade

### Phase 4
Backtest and paper trade for at least several months of live forward observation.

### Phase 5
Only then consider small real-money deployment.

## Step-by-Step Deployment Plan Toward Full Functionality

### Stage 1: Clean and stabilize the local product
- continue polishing the dashboard UX
- reduce clutter and improve onboarding text
- standardize table naming, chart labels, and status badges

### Stage 2: Run the system as a monitoring service
- deploy the monitor workflow on a dedicated machine or cloud instance
- run broker sync, scanner refresh, and exit evaluation on a recurring schedule
- alert the user when high-priority actions appear

### Stage 3: Improve model validation
- collect paper trade outcomes across multiple weeks and market regimes
- measure expectancy, drawdown, and slippage realism
- identify which setups perform best and which should be retired

### Stage 4: Harden automation
- add stricter exposure limits
- add more advanced option position handling
- improve error recovery, notifications, and order-state reconciliation

### Stage 5: Controlled live rollout
- move from paper to very small live size only after evidence is stable
- cap risk aggressively

## Latest Paper Options Expansion

The options layer has now been expanded beyond the initial two paper contracts. Verified additional short-dated paper option positions now include SNAP, BAC, DIA, and DIS alongside the earlier AMD and IWM contracts.

## Performance Journal

A live performance journal is now part of the workflow through [projects/options_trading_ai/performance_journal.py](projects/options_trading_ai/performance_journal.py). It records open positions, current unrealized profit or loss, journal status, and recent execution context so the dashboard can act more like an operator desk and less like a static screener.

## Immediate Next Steps

The most practical next actions from here are:

1. keep collecting paper results across more sessions and market regimes
2. add true option exit automation and options-specific stop or take-profit rules
3. improve contract selection with liquidity, open interest, and spread scoring
4. add sector and correlation guardrails so the portfolio is not overly concentrated
5. build alerts and a more polished operator workflow for daily monitoring
6. increase size only when forward results remain positive

The project has already started reducing manual touch through automatic approval mode, urgent exit guardrails, the performance journal, and exposure caps. The next high-value improvement is deeper sector and correlation awareness rather than simply adding more trades.

This is the right order because it improves real trading quality before cosmetic expansion.

## Expected Timeline for Live Readiness

A realistic answer is:
- **initial signal confidence:** around 6 to 8 weeks of paper trading if the system generates a healthy number of trades
- **better confidence across different market conditions:** around 3 to 6 months
- **serious live-readiness review:** after at least 100 to 200 paper trades across multiple regimes

If the paper results remain stable through that window and the drawdowns stay controlled, that is when a small live pilot becomes reasonable.

## Practical Plan to Start Making Money Carefully

1. keep paper trading first
2. monitor expectancy and drawdown instead of chasing a win rate alone
3. identify the setups with the most repeatable edge
4. deploy only the top subset of setups to small live size
5. scale gradually only after live results confirm the same edge

This is the most professional path because it focuses on survival, repeatability, and controlled compounding rather than rushing into size.

## Business Model if This Project Were Marketed

If packaged as a product, the business model could be framed as a premium AI trading intelligence platform.

### Possible revenue streams
- monthly subscriptions for the dashboard and alerts
- higher-tier plans for autonomous paper trading and broker integrations
- white-label dashboards for trading communities or advisors
- premium research feeds for options ideas, exit signals, and event catalysts
- future enterprise licensing for small funds, RIA teams, or prop-style research groups

### Product positioning
The value proposition would not be “magic predictions.” It would be:
- faster idea discovery
- better risk discipline
- integrated research, execution, and monitoring
- clearer explanations for why a trade is being selected and how it should be managed

### Go-to-market angle
The strongest message would be that the platform helps serious traders operate more like a systematic desk, without needing to build the full infrastructure themselves.

---

## Better Autonomous Paper-Trading Path

The best near-term path is not to jump directly into fully automated options execution. The safer and more professional approach is:

1. keep the research and scoring layer separate from the broker layer
2. route only approved ideas into a paper queue
3. execute small proxy equity orders first so the live market plumbing can be validated
4. store execution logs for fill-quality review
5. add true options contract resolution only after the paper workflow is stable

This architecture is already started with:
- [projects/options_trading_ai/paper_trade.py](projects/options_trading_ai/paper_trade.py)
- [projects/options_trading_ai/approve_paper_trades.py](projects/options_trading_ai/approve_paper_trades.py)
- [projects/options_trading_ai/execute_paper_trades.py](projects/options_trading_ai/execute_paper_trades.py)
- [projects/options_trading_ai/sync_broker_state.py](projects/options_trading_ai/sync_broker_state.py)
- [projects/options_trading_ai/src/execution.py](projects/options_trading_ai/src/execution.py)
- [projects/options_trading_ai/.env.example](projects/options_trading_ai/.env.example)

The approval gate is intentional. Trades should remain pending until they are reviewed and explicitly approved for submit. The runtime now reads a local project environment file so paper credentials can stay outside the example template.

---

## First Run Report — 2026-04-16

The first controlled paper execution was completed through the Alpaca paper trading environment using the current proxy-equity execution bridge.

### Trades submitted and confirmed on Alpaca paper

| Symbol | Side | Quantity | Alpaca order status | Notes |
|---|---:|---:|---|---|
| QQQ | Buy | 3 | Filled | First approved ETF proxy order |
| SPY | Buy | 2 | Filled | First approved ETF proxy order |
| QQQ | Buy | 3 | Filled | Second paper fill captured while validating the duplicate-submit guard |
| SPY | Buy | 2 | Filled | Second paper fill captured while validating the duplicate-submit guard |

### What this means
- these trades should appear on the Alpaca paper site under recent orders and positions
- the current build is routing small proxy equity trades while the real options-leg router is still being developed
- broker connectivity, approval gating, and execution logging are now verified with live paper-environment evidence
- the duplicate-submit protection has now been tightened so future reruns will not intentionally recycle already filled ideas

### Current observed paper account state after the first run and validation pass
- connection status: connected
- market status: open during the run
- open positions observed: QQQ long 6 shares, SPY long 4 shares

### Source files for this first-run record
- [projects/options_trading_ai/outputs/broker_orders.csv](projects/options_trading_ai/outputs/broker_orders.csv)
- [projects/options_trading_ai/outputs/broker_positions.csv](projects/options_trading_ai/outputs/broker_positions.csv)
- [projects/options_trading_ai/outputs/execution_log.csv](projects/options_trading_ai/outputs/execution_log.csv)

### Why this gives a smooth future transition to live trading

Because the model, risk checks, and broker submission are decoupled, the future live upgrade mainly becomes a matter of:
- switching the mode from paper to live only after evidence supports it
- tightening liquidity and slippage controls
- replacing proxy share orders with actual options contract routing
- keeping the same dashboard, logs, and risk workflow

---

## How This Program Can Find Better Long and Short Opportunities

A stronger version of the system should pull opportunities from multiple evidence streams, not just price bars.

### Good professional inputs for long and short idea generation
- price and volume screens for breakouts and breakdowns
- earnings calendars and post-event reaction data
- sector-relative strength and relative weakness tables
- analyst revisions and estimate changes
- regulatory filings and company-specific news
- options flow, open interest shifts, and implied-volatility dislocations
- carefully filtered community discussion as hypothesis input only

### Can broader paper exposure help the model?
Yes, within reason. A wider variety of liquid paper trades can help build a richer evidence set for:
- how different setups behave across sectors
- which catalysts create the biggest volatility shifts
- whether long and short ideas behave differently in practice
- how execution quality changes during busy news periods

The important part is that the added exposure should still be controlled by risk limits and not just used to spray trades randomly.

### Congressional disclosures as a secondary signal
The project is now being extended to support House and Senate disclosure intelligence in a dashboard-friendly format. The intended use is:
- surface unusual politician-linked buying or selling activity
- summarize which tickers are appearing repeatedly
- compare those disclosures with price action, news, and volatility
- never treat congressional trades as a guaranteed standalone edge

### How to think about outside sources
News sites and community posts can help generate leads, but they should not be treated as trade signals by themselves. The better workflow is:
1. use them to discover tickers worth screening
2. test the price, volume, and volatility evidence
3. only then consider an actual trade structure

This helps broaden the search universe while still keeping the system disciplined.

### What the current trade types mean
- bullish idea → usually a call or bull call spread
- bearish idea → usually a put or bear put spread
- weak or mixed signal → no trade

### When a trade should be sold
The system should not hold forever. The current advisor layer now emphasizes exits such as:
- take partial profits into strength or weakness when targets are met
- exit when the technical thesis breaks
- exit if time decay starts to work against the option structure
- cut risk when the maximum planned loss is approached

This makes the system closer to a real advisor instead of only an entry screener.

### Decisions and events that often drive volatility
Some of the most important volatility catalysts the system should watch are:
- earnings reports and forward guidance
- analyst upgrades or downgrades
- CPI, jobs, FOMC, and rate commentary
- product launches or major partnerships
- lawsuits, investigations, or regulatory decisions
- mergers, offerings, or capital raises

These are exactly the kinds of events that can make both long and short opportunities far more actionable.

## In-Depth Analysis of the Latest Dashboard and Research Upgrade

The most recent round of work focused on three goals: make the dashboard easier to use, make the research process broader and more realistic, and make the operational side of paper trading easier to understand at a glance.

### 1. Why the user-experience changes matter
A research dashboard can become overwhelming if it shows only raw tables and charts without helping the user prioritize action. The latest layout changes were aimed at reducing cognitive load.

The additions that matter most are:
- a clearer current-investments area tied directly to Alpaca paper positions
- more prominent summary metrics showing exposure, return expectations, and exit alerts
- quick guidance that explains how to interpret the screen rather than assuming the user already thinks like a quant desk

This matters because a useful trading tool should not only compute signals; it should also help the user decide what deserves attention first.

### 2. Why the broader research feeds matter
The model is now being pushed beyond a narrow price-only process. That is strategically important because real market opportunities often arise when price behavior intersects with events.

Examples include:
- earnings surprises
- analyst revisions
- macro announcements
- legal or regulatory headlines
- congressional disclosures that may highlight names worth deeper review

By broadening the sourcing layer, the project becomes less like a toy screener and more like an intelligence dashboard that can surface ideas from multiple angles.

### 3. Why the risk and exit layer matters
Entry quality is only one part of the problem. A system that can enter trades but cannot explain how and when to reduce or close risk is incomplete.

That is why the recent upgrades also emphasize:
- suggested take-profit and stop-loss style thinking
- exit recommendations for current holdings
- broker status, positions, and order history in the same interface

This gives the project a more complete research-to-decision workflow and makes future live deployment far more realistic.

### 4. Why this improves the long-term model
A model improves when it sees more varied but still disciplined observations. A broader liquid-stock universe, event-aware screening, and clearer logging can help generate a more useful feedback loop for future calibration and learning.

In other words, the value of these upgrades is not only visual. They improve the quality of the research process itself.

## Can This Become a Web App or Mobile App?

Yes. The current codebase is already moving in a web-friendly direction.

### Short-term path
- keep using Streamlit for desktop and browser access
- optionally deploy it as a lightweight internal web dashboard

### Medium-term path
- move the trading engine into a backend API service such as FastAPI
- keep the charts and user interface in a React or Next.js frontend
- expose the same data to both a website and a mobile app

### Long-term path
- create a dedicated mobile application in React Native or Flutter
- let the mobile app consume the same backend execution and analytics APIs
- add alerts, approvals, and account monitoring without changing the model core

That means this project can absolutely grow into a website or mobile product later if the strategy quality justifies it.

---

## Why This Project Has Real Potential

A strong system could become more useful than manual research because it:
- never stops watching the market
- remembers every setup
- tests ideas faster than a human can
- adapts to regime shifts with retraining
- avoids emotional mistakes

That said, the real differentiator is not just AI. It is **AI plus disciplined research, proper validation, and ruthless risk control**.

---

## Honest Conclusion

This project can be designed to improve the odds of profitable options trading, but it should be presented honestly:

- it can build a measurable edge
- it can improve consistency
- it can reduce human error
- it cannot promise guaranteed profits

The reason it could beat many independent traders is simple: broader information, faster analysis, better memory, and stricter discipline.

If built carefully, it could become a serious decision-support engine for options trading rather than just another prediction toy.
