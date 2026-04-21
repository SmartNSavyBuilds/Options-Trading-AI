from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from paper_trade import build_paper_trade_queue
from src.congressional_disclosures import import_raw_trade_data, build_public_interest_watchlist, build_recent_large_trades
from src.execution import (
    TradingConfig,
    choose_best_option_contract,
    prepare_equity_execution_preview,
    prepare_exit_execution_preview,
    prepare_option_execution_preview,
)
from evaluate_exit_rules import apply_automatic_exit_guardrails
from src.exit_manager import build_exit_recommendations
from src.multi_asset import build_crypto_watchlist, build_market_regime_summary
from src.options_selector import build_options_candidates
from src.performance_journal import add_execution_status_to_candidates, build_execution_quality_report, build_open_trade_timeline, build_performance_journal, build_priority_alerts, build_strategy_attribution, humanize_display_text
from src.risk_guardrails import build_exposure_summary, build_risk_overview, build_stress_test_table
from src.signal_engine import build_signal_report


def _make_market_frame(prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            'Close': prices,
            'Volume': [1_000_000 + i * 5_000 for i in range(len(prices))],
        }
    )


def test_bearish_breakdown_gets_ranked_for_shorts() -> None:
    market_data = {
        'BEAR': _make_market_frame([120, 118, 117, 116, 115, 114, 112, 111, 109, 108, 107, 106, 105, 104, 103, 101, 100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 89, 88, 87, 86, 85, 84, 83, 82]),
    }

    report = build_signal_report(market_data)
    row = report.iloc[0]

    assert row['bias'] == 'bearish'
    assert float(row['short_rank_score']) > float(row['long_rank_score'])
    assert row['suggested_structure'] in {'Bear put spread', 'Long put'}


def test_prepare_exit_execution_preview_requires_guardrails() -> None:
    recommendations = pd.DataFrame(
        [
            {
                'symbol': 'SPY',
                'side': 'long',
                'qty': 4,
                'action': 'stop_out',
                'reason': 'Loss breached threshold.',
                'exit_approved': True,
                'order_status': 'monitor',
            }
        ]
    )
    positions = pd.DataFrame(
        [
            {
                'symbol': 'SPY',
                'side': 'long',
                'qty': 4,
                'market_value': 2000.0,
                'unrealized_pl': -60.0,
            }
        ]
    )

    config = TradingConfig(auto_submit=True, trading_mode='paper')
    preview = prepare_exit_execution_preview(recommendations, positions, config)

    assert not preview.empty
    row = preview.iloc[0]
    assert row['exit_side'] == 'sell'
    assert int(row['exit_qty']) == 4
    assert bool(row['ready_for_broker_submit']) is False


def test_import_raw_trade_data_normalizes_common_columns(tmp_path: Path) -> None:
    raw_dir = tmp_path / 'congress_raw'
    raw_dir.mkdir()
    pd.DataFrame(
        [
            {
                'Representative': 'Jane Doe',
                'Ticker': 'NVDA',
                'Transaction': 'Purchase',
                'Transaction Date': '2026-04-10',
                'Notification Date': '2026-04-15',
                'Amount': '$15,001 - $50,000',
            }
        ]
    ).to_csv(raw_dir / 'house_sample.csv', index=False)

    imported = import_raw_trade_data(tmp_path)

    assert len(imported) == 1
    row = imported.iloc[0]
    assert row['member'] == 'Jane Doe'
    assert row['ticker'] == 'NVDA'
    assert row['transaction_type'] == 'Purchase'


def test_build_recent_large_trades_filters_by_window_and_size() -> None:
    disclosures = pd.DataFrame(
        [
            {'chamber': 'House', 'member': 'Nancy Pelosi', 'ticker': 'NVDA', 'transaction_type': 'Purchase', 'transaction_date': '2026-04-10', 'disclosed_date': '2026-04-15', 'amount_range': '$100,001 - $250,000', 'source_note': 'test'},
            {'chamber': 'House', 'member': 'Jane Doe', 'ticker': 'AAPL', 'transaction_type': 'Purchase', 'transaction_date': '2026-03-01', 'disclosed_date': '2026-03-10', 'amount_range': '$1,001 - $15,000', 'source_note': 'test'},
        ]
    )

    recent = build_recent_large_trades(disclosures, lookback_days=14, min_amount_usd=50000, reference_date='2026-04-17')

    assert len(recent) == 1
    assert recent.iloc[0]['member'] == 'Nancy Pelosi'
    assert recent.iloc[0]['ticker'] == 'NVDA'


def test_build_public_interest_watchlist_surfaces_pelosi() -> None:
    disclosures = pd.DataFrame(
        [
            {'chamber': 'House', 'member': 'Nancy Pelosi', 'ticker': 'NVDA', 'transaction_type': 'Purchase', 'transaction_date': '2026-04-10', 'disclosed_date': '2026-04-15', 'amount_range': '$100,001 - $250,000', 'source_note': 'test'},
        ]
    )

    watchlist = build_public_interest_watchlist(disclosures, reference_date='2026-04-17')

    assert not watchlist.empty
    assert 'Nancy Pelosi' in watchlist['member'].tolist()
    assert int(watchlist.loc[watchlist['member'] == 'Nancy Pelosi', 'recent_trade_count'].iloc[0]) == 1


def test_choose_best_option_contract_prefers_short_dated_tradable_match() -> None:
    contracts = pd.DataFrame(
        [
            {'symbol': 'AMD260417C00270000', 'underlying_symbol': 'AMD', 'type': 'call', 'tradable': True, 'expiration_date': '2026-04-17', 'strike_price': '270'},
            {'symbol': 'AMD260424C00280000', 'underlying_symbol': 'AMD', 'type': 'call', 'tradable': True, 'expiration_date': '2026-04-24', 'strike_price': '280'},
            {'symbol': 'AMD260515C00290000', 'underlying_symbol': 'AMD', 'type': 'call', 'tradable': True, 'expiration_date': '2026-05-15', 'strike_price': '290'},
        ]
    )

    chosen = choose_best_option_contract(contracts, underlying_symbol='AMD', option_type='call', target_strike=279, max_days=10)

    assert chosen is not None
    assert chosen['symbol'] == 'AMD260424C00280000'


def test_prepare_option_execution_preview_uses_contract_symbol() -> None:
    queue = pd.DataFrame(
        [
            {
                'ticker': 'AMD',
                'bias': 'bullish',
                'options_setup': 'Buy 278C / Sell 287C',
                'signal_score': 10,
                'projected_return_pct': 117.86,
                'max_risk_usd': 413.10,
                'allocation_pct': 1.2,
                'approval_status': 'approved',
                'approved_for_submit': True,
                'order_status': 'queued_for_review',
            }
        ]
    )
    contracts = {
        'AMD': pd.DataFrame(
            [
                {'symbol': 'AMD260424C00280000', 'underlying_symbol': 'AMD', 'type': 'call', 'tradable': True, 'expiration_date': '2026-04-24', 'strike_price': '280'}
            ]
        )
    }

    config = TradingConfig(auto_submit=True, trading_mode='paper', use_proxy_equities=False)
    preview = prepare_option_execution_preview(queue, config, contracts_map=contracts, max_days=10)

    assert not preview.empty
    row = preview.iloc[0]
    assert row['ticker'] == 'AMD260424C00280000'
    assert row['execution_style'] == 'single_leg_option_paper'
    assert bool(row['ready_for_broker_submit']) is True


def test_prepare_equity_execution_preview_auto_approves_in_automatic_mode() -> None:
    queue = pd.DataFrame(
        [
            {
                'ticker': 'SNAP',
                'bias': 'bullish',
                'options_setup': 'Buy 6C',
                'signal_score': 8,
                'projected_return_pct': 42.0,
                'max_risk_usd': 200.0,
                'allocation_pct': 1.0,
                'approval_status': 'pending',
                'approved_for_submit': False,
                'order_status': 'queued_for_review',
            }
        ]
    )
    signals = pd.DataFrame([{'ticker': 'SNAP', 'last_close': 10.0}])

    config = TradingConfig(auto_submit=True, trading_mode='paper', approval_mode='automatic')
    preview = prepare_equity_execution_preview(queue, signals, config)

    assert not preview.empty
    row = preview.iloc[0]
    assert row['approval_status'] == 'auto_approved'
    assert bool(row['approved_for_submit']) is True
    assert bool(row['ready_for_broker_submit']) is True


def test_build_paper_trade_queue_skips_existing_positions() -> None:
    candidates = pd.DataFrame(
        [
            {'ticker': 'QQQ', 'options_setup': 'Buy 650C', 'signal_score': 9, 'projected_return_pct': 40.0, 'allocation_pct': 1.0, 'rank_score': 99.0, 'max_risk_usd': 500.0, 'bias': 'bullish'},
            {'ticker': 'SNAP', 'options_setup': 'Buy 6C', 'signal_score': 8, 'projected_return_pct': 35.0, 'allocation_pct': 1.0, 'rank_score': 95.0, 'max_risk_usd': 300.0, 'bias': 'bullish'},
        ]
    )

    queue = build_paper_trade_queue(candidates, existing_tickers={'QQQ'}, max_positions=5)

    assert list(queue['ticker']) == ['SNAP']


def test_build_exit_recommendations_includes_option_close_plan() -> None:
    positions = pd.DataFrame(
        [
            {
                'symbol': 'AMD260424C00277500',
                'side': 'long',
                'qty': 1,
                'market_value': 755.0,
                'unrealized_pl': -10.0,
            }
        ]
    )
    signals = pd.DataFrame(
        [
            {
                'ticker': 'AMD',
                'signal_score': 8,
                'bias': 'bullish',
            }
        ]
    )

    recommendations = build_exit_recommendations(positions, signals)

    assert len(recommendations) == 1
    row = recommendations.iloc[0]
    assert row['asset_class'] == 'option'
    assert row['underlying_symbol'] == 'AMD'
    assert 'close' in str(row['close_or_exercise_plan']).lower() or 'exercise' in str(row['close_or_exercise_plan']).lower()


def test_build_performance_journal_creates_open_position_rows() -> None:
    positions = pd.DataFrame(
        [
            {'symbol': 'SNAP', 'side': 'long', 'qty': 100, 'market_value': 1000.0, 'unrealized_pl': 35.0},
            {'symbol': 'SNAP260424C00006000', 'side': 'long', 'qty': 2, 'market_value': 54.0, 'unrealized_pl': -4.0},
        ]
    )
    execution_log = pd.DataFrame(
        [
            {'submitted_at_utc': '2026-04-17T17:58:18+00:00', 'ticker': 'SNAP260424C00006000', 'underlying_ticker': 'SNAP', 'status': 'pending_new', 'execution_style': 'single_leg_option_paper'},
        ]
    )

    journal = build_performance_journal(positions, execution_log)

    assert len(journal) == 2
    assert set(journal['asset_class']) == {'stock', 'option'}
    assert 'open_winner' in set(journal['journal_status']) or 'open_loser' in set(journal['journal_status'])


def test_add_execution_status_to_candidates_marks_open_names() -> None:
    candidates = pd.DataFrame(
        [
            {'ticker': 'SNAP', 'bias': 'bullish', 'options_setup': 'Buy 6C', 'signal_score': 9},
            {'ticker': 'META', 'bias': 'bullish', 'options_setup': 'Buy 673C', 'signal_score': 8},
        ]
    )
    positions = pd.DataFrame(
        [
            {'symbol': 'SNAP260424C00006000', 'side': 'long', 'qty': 2, 'market_value': 54.0, 'unrealized_pl': -4.0},
        ]
    )
    execution_log = pd.DataFrame(
        [
            {'submitted_at_utc': '2026-04-17T17:58:18+00:00', 'ticker': 'SNAP260424C00006000', 'underlying_ticker': 'SNAP', 'status': 'pending_new', 'execution_style': 'single_leg_option_paper'},
        ]
    )

    marked = add_execution_status_to_candidates(candidates, positions, execution_log)

    snap_state = marked.loc[marked['ticker'] == 'SNAP', 'execution_state'].iloc[0]
    meta_state = marked.loc[marked['ticker'] == 'META', 'execution_state'].iloc[0]
    assert snap_state == 'Open / Executed'
    assert meta_state == 'Research Queue'
    assert bool(marked.loc[marked['ticker'] == 'SNAP', 'is_open_executed'].iloc[0]) is True


def test_build_priority_alerts_flags_urgent_positions() -> None:
    journal = pd.DataFrame(
        [
            {
                'symbol': 'IWM260420C00270000',
                'underlying_symbol': 'IWM',
                'asset_class': 'option',
                'unrealized_pl': -148.0,
                'market_value': 1226.0,
                'journal_status': 'open_loser',
            },
            {
                'symbol': 'QQQ',
                'underlying_symbol': 'QQQ',
                'asset_class': 'stock',
                'unrealized_pl': 53.0,
                'market_value': 3889.0,
                'journal_status': 'open_winner',
            },
        ]
    )
    exits = pd.DataFrame(
        [
            {'symbol': 'IWM260420C00270000', 'action': 'stop_out', 'priority': 1, 'days_to_expiration': 3, 'reason': 'Loss breached threshold.'}
        ]
    )

    alerts = build_priority_alerts(journal, exits)

    assert not alerts.empty
    assert 'IWM260420C00270000' in alerts['symbol'].tolist()
    assert 'urgent_risk' in alerts['alert_type'].tolist()
    assert alerts.loc[alerts['symbol'] == 'IWM260420C00270000', 'escalation_action'].iloc[0] == 'page_operator_now'


def test_apply_automatic_exit_guardrails_approves_urgent_rows() -> None:
    exits = pd.DataFrame(
        [
            {'symbol': 'IWM260420C00270000', 'asset_class': 'option', 'action': 'stop_out', 'priority': 1, 'days_to_expiration': 3, 'exit_approved': False, 'order_status': 'monitor'},
            {'symbol': 'QQQ', 'asset_class': 'stock', 'action': 'hold', 'priority': 9, 'days_to_expiration': '', 'exit_approved': False, 'order_status': 'monitor'},
        ]
    )

    updated = apply_automatic_exit_guardrails(exits, enable_auto_approve=True)

    assert bool(updated.loc[updated['symbol'] == 'IWM260420C00270000', 'exit_approved'].iloc[0]) is True
    assert updated.loc[updated['symbol'] == 'IWM260420C00270000', 'order_status'].iloc[0] == 'auto_approved'
    assert bool(updated.loc[updated['symbol'] == 'QQQ', 'exit_approved'].iloc[0]) is False


def test_build_exposure_summary_groups_by_underlying() -> None:
    positions = pd.DataFrame(
        [
            {'symbol': 'SNAP', 'market_value': 1200.0},
            {'symbol': 'SNAP260424C00006000', 'market_value': 54.0},
            {'symbol': 'QQQ', 'market_value': 3800.0},
        ]
    )

    summary = build_exposure_summary(positions, account_size_usd=100000.0)

    snap_row = summary.loc[summary['underlying_symbol'] == 'SNAP'].iloc[0]
    assert round(float(snap_row['total_market_value']), 2) == 1254.0
    assert int(snap_row['position_count']) == 2


def test_build_paper_trade_queue_applies_exposure_cap() -> None:
    candidates = pd.DataFrame(
        [
            {'ticker': 'META', 'options_setup': 'Buy 673C', 'signal_score': 9, 'projected_return_pct': 40.0, 'allocation_pct': 2.0, 'rank_score': 99.0, 'max_risk_usd': 300.0, 'bias': 'bullish'},
            {'ticker': 'NVDA', 'options_setup': 'Buy 198C', 'signal_score': 8, 'projected_return_pct': 35.0, 'allocation_pct': 2.0, 'rank_score': 98.0, 'max_risk_usd': 300.0, 'bias': 'bullish'},
        ]
    )
    positions = pd.DataFrame(
        [
            {'symbol': 'QQQ', 'market_value': 33000.0},
        ]
    )

    queue = build_paper_trade_queue(
        candidates,
        existing_tickers=set(),
        positions=positions,
        account_size_usd=100000.0,
        max_total_exposure_pct=35.0,
        max_positions=5,
    )

    assert list(queue['ticker']) == ['META']
    assert queue['guardrail_status'].iloc[0] == 'pass'


def test_build_paper_trade_queue_applies_sector_cap() -> None:
    candidates = pd.DataFrame(
        [
            {'ticker': 'AAPL', 'options_setup': 'Buy 220C', 'signal_score': 9, 'projected_return_pct': 32.0, 'allocation_pct': 3.0, 'rank_score': 99.0, 'max_risk_usd': 250.0, 'bias': 'bullish'},
            {'ticker': 'XLF', 'options_setup': 'Buy 55C', 'signal_score': 8, 'projected_return_pct': 28.0, 'allocation_pct': 3.0, 'rank_score': 98.0, 'max_risk_usd': 250.0, 'bias': 'bullish'},
        ]
    )
    positions = pd.DataFrame(
        [
            {'symbol': 'QQQ', 'market_value': 16000.0},
        ]
    )

    queue = build_paper_trade_queue(
        candidates,
        existing_tickers=set(),
        positions=positions,
        account_size_usd=100000.0,
        max_total_exposure_pct=40.0,
        max_single_name_exposure_pct=20.0,
        max_queue_risk_usd=5000.0,
        max_sector_exposure_pct=15.0,
        max_positions=5,
    )

    assert list(queue['ticker']) == ['XLF']
    assert queue['sector'].iloc[0] == 'Financials'


def test_build_risk_overview_and_stress_table() -> None:
    positions = pd.DataFrame(
        [
            {'symbol': 'QQQ', 'market_value': 4000.0},
            {'symbol': 'SNAP260424C00006000', 'market_value': 600.0},
            {'symbol': 'XLF', 'market_value': 1400.0},
        ]
    )

    overview = build_risk_overview(positions, account_size_usd=100000.0)
    stress = build_stress_test_table(positions)

    assert not overview.empty
    assert round(float(overview.iloc[0]['total_exposure_pct']), 2) == 6.0
    assert 'largest_sector' in overview.columns
    assert not stress.empty
    assert {'scenario', 'shock_pct', 'estimated_pnl', 'stressed_market_value'}.issubset(stress.columns)
    assert float(stress.loc[stress['scenario'] == 'Risk-off -5%', 'estimated_pnl'].iloc[0]) < 0


def test_build_crypto_watchlist_flags_breakout_candidates() -> None:
    signals = pd.DataFrame(
        [
            {'ticker': 'BTC', 'bias': 'bullish', 'signal_score': 8, 'annualized_volatility': 0.55, 'advisor_summary': 'Momentum remains strong.'},
            {'ticker': 'ETH', 'bias': 'neutral', 'signal_score': 1, 'annualized_volatility': 0.42, 'advisor_summary': 'Mixed tape.'},
        ]
    )

    watchlist = build_crypto_watchlist(signals)

    assert not watchlist.empty
    assert 'crypto_action' in watchlist.columns
    assert watchlist.iloc[0]['ticker'] == 'BTC'
    assert watchlist.iloc[0]['asset_class'] == 'crypto'


def test_build_strategy_attribution_summarizes_wins_and_losses() -> None:
    journal = pd.DataFrame(
        [
            {'asset_class': 'stock', 'execution_style': 'equity_paper', 'unrealized_pl': 100.0, 'journal_status': 'open_winner', 'market_value': 1200.0},
            {'asset_class': 'option', 'execution_style': 'single_leg_option_paper', 'unrealized_pl': -40.0, 'journal_status': 'open_loser', 'market_value': 300.0},
        ]
    )

    attribution = build_strategy_attribution(journal)

    assert not attribution.empty
    assert {'strategy_bucket', 'positions', 'win_rate', 'total_unrealized_pl'}.issubset(attribution.columns)
    assert 'Equity Paper' in attribution['strategy_bucket'].tolist()


def test_build_execution_quality_report_tracks_fill_rate() -> None:
    execution_log = pd.DataFrame(
        [
            {'ticker': 'AAPL', 'status': 'filled', 'execution_style': 'equity_paper'},
            {'ticker': 'MSFT', 'status': 'accepted', 'execution_style': 'equity_paper'},
            {'ticker': 'BTC', 'status': 'rejected', 'execution_style': 'crypto_paper'},
        ]
    )

    quality = build_execution_quality_report(execution_log)

    assert not quality.empty
    assert {'execution_style', 'submitted_orders', 'fill_rate_pct'}.issubset(quality.columns)
    assert float(quality.loc[quality['execution_style'] == 'Equity Paper', 'fill_rate_pct'].iloc[0]) == 50.0


def test_humanize_display_text_removes_separators() -> None:
    assert humanize_display_text('single_leg_option_paper') == 'Single Leg Option Paper'
    assert humanize_display_text('risk-off') == 'Risk Off'
    assert humanize_display_text('page_operator_now') == 'Page Operator Now'


def test_build_open_trade_timeline_sets_expected_close_dates() -> None:
    journal = pd.DataFrame(
        [
            {'symbol': 'AAPL', 'asset_class': 'stock', 'qty': 5, 'market_value': 950.0, 'unrealized_pl': -15.0},
            {'symbol': 'AMD260424C00277500', 'asset_class': 'option', 'qty': 1, 'market_value': 220.0, 'unrealized_pl': 18.0},
        ]
    )
    exits = pd.DataFrame(
        [
            {'symbol': 'AAPL', 'action': 'reduce_or_close', 'reason': 'The long thesis has weakened.', 'decision_window': 'Review at the next execution cycle.', 'close_or_exercise_plan': 'Close or reduce the stock position on the next execution cycle to protect capital.', 'expiration_date': '', 'days_to_expiration': ''},
            {'symbol': 'AMD260424C00277500', 'action': 'hold', 'reason': 'Signal remains intact.', 'decision_window': 'Review daily into expiry week (3 days left).', 'close_or_exercise_plan': 'Tighten the stop and review daily.', 'expiration_date': '04-24-2026', 'days_to_expiration': 3},
        ]
    )

    timeline = build_open_trade_timeline(journal, exits, reference_date='2026-04-17')

    assert not timeline.empty
    assert 'expected_close_date' in timeline.columns
    assert timeline.loc[timeline['symbol'] == 'AAPL', 'expected_close_date'].iloc[0] == '04-20-2026'
    assert timeline.loc[timeline['symbol'] == 'AMD260424C00277500', 'expected_close_date'].iloc[0] == '04-24-2026'


def test_build_options_candidates_applies_regime_aware_refinement() -> None:
    signal_report = pd.DataFrame(
        [
            {'ticker': 'AAPL', 'bias': 'bullish', 'suggested_structure': 'Long call', 'last_close': 210.0, 'signal_score': 7, 'annualized_volatility': 0.24, 'setup_quality': 6, 'long_rank_score': 92.0, 'short_rank_score': 8.0, 'advisor_summary': 'Strong upside trend.', 'sell_plan': 'Trim into strength.', 'opportunity_source': 'trend_breakout_scan', 'thesis_strength': 'high'},
            {'ticker': 'MSFT', 'bias': 'bullish', 'suggested_structure': 'Bull call spread', 'last_close': 420.0, 'signal_score': 6, 'annualized_volatility': 0.22, 'setup_quality': 5, 'long_rank_score': 88.0, 'short_rank_score': 10.0, 'advisor_summary': 'Momentum remains supportive.', 'sell_plan': 'Take partial profits into extensions.', 'opportunity_source': 'momentum_leaders', 'thesis_strength': 'high'},
            {'ticker': 'QQQ', 'bias': 'bullish', 'suggested_structure': 'Bull call spread', 'last_close': 510.0, 'signal_score': 5, 'annualized_volatility': 0.20, 'setup_quality': 5, 'long_rank_score': 81.0, 'short_rank_score': 12.0, 'advisor_summary': 'Breadth supports the move.', 'sell_plan': 'Respect a break of support.', 'opportunity_source': 'trend_breakout_scan', 'thesis_strength': 'medium'},
            {'ticker': 'TSLA', 'bias': 'bearish', 'suggested_structure': 'Long put', 'last_close': 175.0, 'signal_score': -6, 'annualized_volatility': 0.27, 'setup_quality': 5, 'long_rank_score': 12.0, 'short_rank_score': 78.0, 'advisor_summary': 'Weak relative trend.', 'sell_plan': 'Cover on momentum repair.', 'opportunity_source': 'breakdown_short_scan', 'thesis_strength': 'medium'},
        ]
    )

    candidates = build_options_candidates(signal_report)

    bull = candidates.loc[candidates['ticker'] == 'AAPL'].iloc[0]
    bear = candidates.loc[candidates['ticker'] == 'TSLA'].iloc[0]

    assert 'base_rank_score' in candidates.columns
    assert 'refinement_adjustment' in candidates.columns
    assert 'regime_alignment' in candidates.columns
    assert 'refinement_note' in candidates.columns
    assert bull['regime_alignment'] == 'tailwind'
    assert bear['regime_alignment'] == 'headwind'
    assert float(bull['rank_score']) > float(bull['base_rank_score'])
    assert 'risk on' in str(bull['refinement_note']).lower()


def test_build_options_candidates_uses_recent_learning_feedback() -> None:
    signal_report = pd.DataFrame(
        [
            {'ticker': 'AAPL', 'bias': 'bullish', 'suggested_structure': 'Long call', 'last_close': 210.0, 'signal_score': 7, 'annualized_volatility': 0.24, 'setup_quality': 6, 'long_rank_score': 92.0, 'short_rank_score': 8.0, 'advisor_summary': 'Strong upside trend.', 'sell_plan': 'Trim into strength.', 'opportunity_source': 'trend_breakout_scan', 'thesis_strength': 'high'},
        ]
    )
    learning_feedback = pd.DataFrame(
        [
            {'ticker': 'AAPL', 'learning_adjustment': 4.0, 'learning_note': 'Recent paper trades in this name have been profitable.'},
        ]
    )

    candidates = build_options_candidates(signal_report, learning_feedback=learning_feedback)

    row = candidates.iloc[0]
    assert 'learning_adjustment' in candidates.columns
    assert 'learning_note' in candidates.columns
    assert float(row['learning_adjustment']) == 4.0
    assert 'profitable' in str(row['learning_note']).lower()
    assert float(row['rank_score']) > float(row['base_rank_score'])


def test_build_market_regime_summary_identifies_risk_on_tape() -> None:
    signals = pd.DataFrame(
        [
            {'ticker': 'QQQ', 'bias': 'bullish', 'signal_score': 7, 'annualized_volatility': 0.20},
            {'ticker': 'SPY', 'bias': 'bullish', 'signal_score': 6, 'annualized_volatility': 0.18},
            {'ticker': 'IWM', 'bias': 'bullish', 'signal_score': 5, 'annualized_volatility': 0.22},
        ]
    )

    regime = build_market_regime_summary(signals)

    assert not regime.empty
    assert regime.iloc[0]['market_regime'] == 'risk_on'
    assert float(regime.iloc[0]['bullish_ratio']) == 1.0


# ─── Signal Engine: structure selection and score boundary tests ───────────────

def test_choose_structure_low_vol_bullish_returns_long_call() -> None:
    from src.signal_engine import choose_structure
    assert choose_structure(5, 0.20) == 'Long call'


def test_choose_structure_high_vol_bullish_returns_bull_spread() -> None:
    from src.signal_engine import choose_structure
    assert choose_structure(5, 0.30) == 'Bull call spread'


def test_choose_structure_low_vol_bearish_returns_long_put() -> None:
    from src.signal_engine import choose_structure
    assert choose_structure(-5, 0.20) == 'Long put'


def test_choose_structure_high_vol_bearish_returns_bear_spread() -> None:
    from src.signal_engine import choose_structure
    assert choose_structure(-5, 0.40) == 'Bear put spread'


def test_choose_structure_neutral_score_returns_no_trade() -> None:
    from src.signal_engine import choose_structure
    assert choose_structure(0, 0.30) == 'No trade / wait'
    assert choose_structure(2, 0.30) == 'No trade / wait'
    assert choose_structure(-2, 0.20) == 'No trade / wait'


def test_compute_rsi_overbought_series() -> None:
    from src.signal_engine import compute_rsi
    # RSI on a series with both gains and losses should produce finite values
    # Alternate: up 2 then down 1 — net bullish, so RSI should be above 50
    prices = []
    val = 100.0
    for i in range(60):
        val += 2.0 if i % 3 != 2 else -1.0
        prices.append(val)
    rsi = compute_rsi(pd.Series(prices))
    valid = rsi.dropna()
    assert len(valid) > 0
    assert float(valid.iloc[-1]) > 50


def test_compute_rsi_oversold_series() -> None:
    from src.signal_engine import compute_rsi
    # Alternate: down 2 then up 1 — net bearish, RSI should be below 50
    prices = []
    val = 100.0
    for i in range(60):
        val += -2.0 if i % 3 != 2 else 1.0
        prices.append(val)
    rsi = compute_rsi(pd.Series(prices))
    valid = rsi.dropna()
    assert len(valid) > 0
    assert float(valid.iloc[-1]) < 50


def test_signal_report_bullish_trending_stock() -> None:
    # Strong uptrend: monotonically rising prices
    prices = [float(80 + i * 0.8) for i in range(40)]
    market_data = {'TREND': _make_market_frame(prices)}
    report = build_signal_report(market_data)
    row = report.iloc[0]
    assert row['bias'] == 'bullish'
    assert int(row['signal_score']) > 0
    assert row['suggested_structure'] in {'Long call', 'Bull call spread'}


def test_signal_report_insufficient_data_returns_neutral() -> None:
    market_data = {'SPARSE': _make_market_frame([100.0, 101.0, 99.0])}
    report = build_signal_report(market_data)
    row = report.iloc[0]
    assert row['bias'] == 'insufficient_data'
    assert int(row['signal_score']) == 0


# ─── Exit rules: threshold boundary tests ─────────────────────────────────────

def test_exit_rules_stop_out_at_loss_threshold() -> None:
    from src.exit_manager import build_exit_recommendations
    positions = pd.DataFrame([{
        'symbol': 'SPY',
        'side': 'long',
        'qty': 4,
        'market_value': 2000.0,
        'unrealized_pl': -60.0,   # -3% — breaches -2% threshold
    }])
    signals = pd.DataFrame([{'ticker': 'SPY', 'signal_score': 7, 'bias': 'bullish'}])
    recs = build_exit_recommendations(positions, signals)
    assert recs.iloc[0]['action'] == 'stop_out'


def test_exit_rules_trim_winner_at_gain_threshold() -> None:
    from src.exit_manager import build_exit_recommendations
    positions = pd.DataFrame([{
        'symbol': 'QQQ',
        'side': 'long',
        'qty': 6,
        'market_value': 4000.0,
        'unrealized_pl': 200.0,   # +5% — exceeds +4% take-profit
    }])
    signals = pd.DataFrame([{'ticker': 'QQQ', 'signal_score': 9, 'bias': 'bullish'}])
    recs = build_exit_recommendations(positions, signals)
    assert recs.iloc[0]['action'] == 'trim_winner'


def test_exit_rules_hold_within_thresholds() -> None:
    from src.exit_manager import build_exit_recommendations
    positions = pd.DataFrame([{
        'symbol': 'MSFT',
        'side': 'long',
        'qty': 3,
        'market_value': 1200.0,
        'unrealized_pl': 6.0,    # +0.5% — within both thresholds
    }])
    signals = pd.DataFrame([{'ticker': 'MSFT', 'signal_score': 9, 'bias': 'bullish'}])
    recs = build_exit_recommendations(positions, signals)
    assert recs.iloc[0]['action'] == 'hold'


def test_exit_rules_reduce_on_signal_flip_to_neutral() -> None:
    from src.exit_manager import build_exit_recommendations
    positions = pd.DataFrame([{
        'symbol': 'AMD',
        'side': 'long',
        'qty': 4,
        'market_value': 1100.0,
        'unrealized_pl': -5.0,   # small loss but signal is now 0
    }])
    signals = pd.DataFrame([{'ticker': 'AMD', 'signal_score': 0, 'bias': 'neutral'}])
    recs = build_exit_recommendations(positions, signals)
    assert recs.iloc[0]['action'] in {'reduce_or_close', 'stop_out'}
