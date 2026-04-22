import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

from src.congressional_disclosures import build_public_interest_watchlist, build_recent_large_trades
from src.execution import TradingConfig
from src.multi_asset import build_crypto_watchlist, build_market_regime_summary
from src.performance_journal import add_execution_status_to_candidates, build_open_trade_timeline, humanize_display_text
from src.risk_guardrails import build_exposure_summary, build_risk_overview, build_stress_test_table


st.set_page_config(page_title='Options Trading AI Dashboard', page_icon='📈', layout='wide')

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'
ENV_PATH = PROJECT_DIR / '.env'
BIAS_COLORS = {
    'bullish': '#00c853',
    'bearish': '#ff5252',
    'neutral': '#90a4ae',
    'insufficient_data': '#b0bec5',
}
EXECUTION_COLORS = {
    'Open / Executed': '#00c853',
    'bullish': '#60a5fa',
    'bearish': '#ff5252',
    'neutral': '#90a4ae',
    'insufficient_data': '#b0bec5',
}
CHART_LABELS = {
    'rank_score': 'Overall Rank',
    'signal_score': 'Signal Score',
    'estimated_cost_usd': 'Estimated Cost ($)',
    'projected_profit_usd': 'Projected Profit ($)',
    'projected_return_pct': 'Projected Return (%)',
    'max_risk_usd': 'Maximum Risk ($)',
    'allocation_pct': 'Position Size (%)',
    'annualized_volatility': 'Annualized Volatility',
    'ticker': 'Ticker',
    'bias': 'Bias',
}


def inject_styles() -> None:
    st.markdown(
        """
        <style>
            .stApp,
            [data-testid="stAppViewContainer"] {
                background: linear-gradient(180deg, #0d1b2a 0%, #0f2340 100%);
                color: #e2e8f0;
            }
            .block-container {
                padding-top: 1.25rem;
            }
            .stApp h1,
            .stApp h2,
            .stApp h3,
            .stApp h4,
            .stApp h5,
            .stApp h6,
            .stApp p,
            .stApp label,
            .stApp li,
            .stApp div[data-testid="stMarkdownContainer"],
            .stApp div[data-testid="stCaptionContainer"] {
                color: #e2e8f0 !important;
            }
            [data-testid="stSidebar"] {
                background: #0d1b2a;
                border-right: 1px solid rgba(100, 116, 139, 0.4);
            }
            [data-testid="stSidebar"] * {
                color: #e2e8f0 !important;
            }
            div[data-testid="stMetric"] {
                background: rgba(30, 41, 59, 0.95);
                border: 1px solid rgba(100, 116, 139, 0.45);
                border-radius: 14px;
                padding: 10px 14px;
                box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
            }
            div[data-testid="stMetricValue"],
            div[data-testid="stMetricLabel"],
            div[data-testid="stMetricDelta"] {
                color: #f1f5f9 !important;
            }
            .dashboard-banner {
                padding: 1rem 1.2rem;
                border-radius: 16px;
                border: 1px solid rgba(100, 116, 139, 0.4);
                background: linear-gradient(90deg, #1e293b, #0f2340);
                color: #f1f5f9;
                margin-bottom: 1rem;
            }
            .status-strip {
                display: flex;
                flex-wrap: wrap;
                gap: 0.5rem;
                margin: 0.35rem 0 1rem 0;
            }
            .status-pill {
                background: #1e293b;
                border: 1px solid rgba(100, 116, 139, 0.5);
                border-radius: 999px;
                padding: 0.35rem 0.75rem;
                font-size: 0.92rem;
                color: #cbd5e1;
            }
            .panel-card {
                background: rgba(30, 41, 59, 0.95);
                border: 1px solid rgba(100, 116, 139, 0.45);
                border-radius: 16px;
                padding: 0.95rem 1rem;
                min-height: 145px;
                margin-bottom: 0.75rem;
                box-shadow: 0 6px 18px rgba(0, 0, 0, 0.25);
            }
            .panel-kicker {
                color: #60a5fa;
                font-size: 0.82rem;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin-bottom: 0.35rem;
            }
            .panel-body {
                color: #e2e8f0;
                font-size: 0.95rem;
                line-height: 1.45;
            }
            [data-baseweb="tab-list"] button {
                background: #1e293b;
                color: #cbd5e1 !important;
                border-radius: 10px 10px 0 0;
            }
            [data-baseweb="tab-list"] button[aria-selected="true"] {
                background: #334155;
                color: #f1f5f9 !important;
            }
            [data-baseweb="select"] > div,
            [data-baseweb="input"] > div,
            .stTextInput input,
            .stNumberInput input,
            .stTextArea textarea {
                background: #1e293b !important;
                color: #e2e8f0 !important;
            }
            div[data-testid="stDataFrame"] {
                border: 1px solid rgba(100, 116, 139, 0.4);
                border-radius: 14px;
                overflow: hidden;
                background: #1e293b;
            }
            div[data-testid="stDataFrame"] [role="columnheader"] {
                background: #0f172a;
                color: #94a3b8;
                font-weight: 600;
            }
            div[data-testid="stDataFrame"] [role="gridcell"] {
                color: #e2e8f0;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=10)
def load_latest_csv(prefix: str) -> pd.DataFrame:
    candidates = sorted(OUTPUT_DIR.glob(f'{prefix}*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return pd.DataFrame()

    for candidate in candidates:
        try:
            if candidate.stat().st_size == 0:
                continue
            try:
                return pd.read_csv(candidate, encoding='utf-8')
            except UnicodeDecodeError:
                return pd.read_csv(candidate, encoding='latin-1')
        except (pd.errors.EmptyDataError, pd.errors.ParserError, OSError):
            continue

    return pd.DataFrame()


def ensure_signal_columns(df: pd.DataFrame) -> pd.DataFrame:
    defaults = {
        'last_close': 0.0,
        'signal_score': 0,
        'bias': 'neutral',
        'rsi': 50.0,
        'annualized_volatility': 0.0,
        'suggested_structure': 'No trade',
        'reason': 'No signal commentary available.',
        'opportunity_source': 'price_action_only',
        'advisor_summary': 'No advisor summary available.',
        'sell_plan': 'No exit plan available.',
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default
    return df


def ensure_candidate_columns(df: pd.DataFrame) -> pd.DataFrame:
    defaults = {
        'projected_return_pct': 0.0,
        'estimated_cost_usd': 0.0,
        'projected_profit_usd': 0.0,
        'allocation_pct': 0.0,
        'rank_score': 0.0,
        'base_rank_score': 0.0,
        'refinement_adjustment': 0.0,
        'learning_adjustment': 0.0,
        'long_rank_score': 0.0,
        'short_rank_score': 0.0,
        'setup_quality': 0,
        'thesis_strength': 'developing',
        'regime_alignment': 'neutral',
        'refinement_note': 'No refinement note available.',
        'learning_note': 'No learning note available.',
        'market_regime': 'unknown',
        'days_to_expiration': 0,
        'max_risk_usd': 0.0,
        'options_setup': 'No trade',
        'expiration_target': 'unavailable',
        'bias': 'neutral',
        'signal_score': 0,
        'ticker': 'n/a',
        'trade_action': 'stand aside',
        'opportunity_source': 'price_action_only',
        'advisor_note': 'No advisor note available.',
        'sell_plan': 'No exit plan available.',
        'take_profit_pct': 0.0,
        'stop_loss_pct': 0.0,
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default
    return df


def ensure_positions_columns(df: pd.DataFrame) -> pd.DataFrame:
    defaults = {
        'symbol': 'n/a',
        'side': 'unknown',
        'qty': 0.0,
        'market_value': 0.0,
        'unrealized_pl': 0.0,
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default
    return df


def ensure_exit_columns(df: pd.DataFrame) -> pd.DataFrame:
    defaults = {
        'symbol': 'n/a',
        'asset_class': 'stock',
        'underlying_symbol': '',
        'side': 'unknown',
        'qty': 0.0,
        'unrealized_pl_pct': 0.0,
        'signal_score': 0,
        'action': 'hold',
        'reason': 'No exit recommendation available.',
        'close_or_exercise_plan': 'Review on the next monitoring cycle.',
        'decision_window': 'Next review pending.',
        'expiration_date': '',
        'days_to_expiration': '',
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default
    return df


def ensure_queue_columns(df: pd.DataFrame) -> pd.DataFrame:
    defaults = {
        'ticker': 'n/a',
        'bias': 'neutral',
        'options_setup': 'No trade',
        'signal_score': 0,
        'projected_return_pct': 0.0,
        'max_risk_usd': 0.0,
        'allocation_pct': 0.0,
        'broker_route': 'alpaca-paper',
        'order_status': 'queued_for_review',
        'approval_status': 'pending',
        'approved_for_submit': False,
        'risk_check': 'review',
        'last_submitted_at': '',
        'option_order_status': 'queued_for_review',
        'last_option_submitted_at': '',
        'queue_refreshed_at_utc': '',
        'guardrail_status': 'pass',
        'guardrail_reason': 'Eligible under current exposure and concentration limits.',
        'current_name_exposure_pct': 0.0,
        'portfolio_exposure_after_trade_pct': 0.0,
        'comment': 'Review bid/ask spread, open interest, and event risk before sending.',
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default
    return df


def format_date_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    formatted = df.copy()
    for col in formatted.columns:
        name = str(col).lower()
        if any(token in name for token in ['date', 'time', '_at', '_utc', 'expiration']):
            parsed = pd.to_datetime(formatted[col], errors='coerce', utc=True)
            if parsed.notna().any():
                fallback = formatted[col].astype(str)
                formatted[col] = parsed.dt.strftime('%m-%d-%Y').where(parsed.notna(), fallback)
    return formatted


def make_display_readable(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    display = df.copy()
    for col in display.columns:
        if 'url' in str(col).lower():
            continue
        if pd.api.types.is_object_dtype(display[col]) or pd.api.types.is_string_dtype(display[col]):
            display[col] = display[col].map(humanize_display_text)
    return display


def render_data_table(df: pd.DataFrame, **kwargs) -> None:
    kwargs.setdefault('width', 'stretch')
    kwargs.setdefault('hide_index', True)
    kwargs.setdefault('column_config', dashboard_column_config())
    st.dataframe(make_display_readable(df), **kwargs)


def dashboard_column_config() -> dict:
    return {
        'ticker': st.column_config.TextColumn('Ticker'),
        'symbol': st.column_config.TextColumn('Ticker'),
        'bias': st.column_config.TextColumn('Bias'),
        'signal_score': st.column_config.NumberColumn('Signal Score', format='%d'),
        'rank_score': st.column_config.NumberColumn('Overall Rank', format='%.2f'),
        'base_rank_score': st.column_config.NumberColumn('Base Rank', format='%.2f'),
        'refinement_adjustment': st.column_config.NumberColumn('Refinement Lift', format='%.2f'),
        'learning_adjustment': st.column_config.NumberColumn('Learning Lift', format='%.2f'),
        'long_rank_score': st.column_config.NumberColumn('Bullish Rank', format='%.2f'),
        'short_rank_score': st.column_config.NumberColumn('Bearish Rank', format='%.2f'),
        'setup_quality': st.column_config.NumberColumn('Setup Quality', format='%d'),
        'thesis_strength': st.column_config.TextColumn('Thesis Strength'),
        'regime_alignment': st.column_config.TextColumn('Regime Fit'),
        'refinement_note': st.column_config.TextColumn('Refinement Note'),
        'learning_note': st.column_config.TextColumn('Learning Note'),
        'market_regime': st.column_config.TextColumn('Market Regime'),
        'expiration_target': st.column_config.TextColumn('Target Expiry'),
        'options_setup': st.column_config.TextColumn('Options Plan'),
        'trade_action': st.column_config.TextColumn('Trade Style'),
        'estimated_cost_usd': st.column_config.NumberColumn('Estimated Cost', format='$%.2f'),
        'projected_profit_usd': st.column_config.NumberColumn('Projected Profit', format='$%.2f'),
        'projected_return_pct': st.column_config.ProgressColumn('Projected Return', min_value=-50, max_value=250, format='%.1f%%'),
        'allocation_pct': st.column_config.ProgressColumn('Position Size', min_value=0, max_value=5, format='%.2f%%'),
        'max_risk_usd': st.column_config.NumberColumn('Max Risk', format='$%.2f'),
        'advisor_note': st.column_config.TextColumn('Why This Idea'),
        'advisor_summary': st.column_config.TextColumn('Why This Idea'),
        'sell_plan': st.column_config.TextColumn('Exit Plan'),
        'take_profit_pct': st.column_config.NumberColumn('Take Profit', format='%.1f%%'),
        'stop_loss_pct': st.column_config.NumberColumn('Stop Loss', format='%.1f%%'),
        'opportunity_source': st.column_config.TextColumn('Idea Source'),
        'broker_route': st.column_config.TextColumn('Broker Route'),
        'order_status': st.column_config.TextColumn('Order Status'),
        'execution_state': st.column_config.TextColumn('Execution Status'),
        'execution_badge': st.column_config.TextColumn('Live Status'),
        'guardrail_status': st.column_config.TextColumn('Guardrail'),
        'guardrail_reason': st.column_config.TextColumn('Guardrail Reason'),
        'sector': st.column_config.TextColumn('Sector'),
        'correlation_bucket': st.column_config.TextColumn('Correlation Bucket'),
        'asset_family': st.column_config.TextColumn('Asset Family'),
        'current_name_exposure_pct': st.column_config.NumberColumn('Current Name Exposure', format='%.2f%%'),
        'portfolio_exposure_after_trade_pct': st.column_config.NumberColumn('Projected Portfolio Exposure', format='%.2f%%'),
        'current_sector_exposure_pct': st.column_config.NumberColumn('Current Sector Exposure', format='%.2f%%'),
        'sector_exposure_after_trade_pct': st.column_config.NumberColumn('Projected Sector Exposure', format='%.2f%%'),
        'current_bucket_exposure_pct': st.column_config.NumberColumn('Current Cluster Exposure', format='%.2f%%'),
        'bucket_exposure_after_trade_pct': st.column_config.NumberColumn('Projected Cluster Exposure', format='%.2f%%'),
        'approval_status': st.column_config.SelectboxColumn('Review Decision', options=['pending', 'approved', 'auto_approved', 'hold', 'rejected']),
        'approved_for_submit': st.column_config.CheckboxColumn('Approve to Send', help='Turn this on only when you want the paper trade to be eligible for routing.'),
        'risk_check': st.column_config.TextColumn('Risk Check'),
        'last_submitted_at': st.column_config.TextColumn('Last Stock Send'),
        'option_order_status': st.column_config.TextColumn('Option Order Status'),
        'last_option_submitted_at': st.column_config.TextColumn('Last Option Send'),
        'queue_refreshed_at_utc': st.column_config.TextColumn('Queue Refreshed'),
        'expiration_date': st.column_config.TextColumn('Expiry Date'),
        'strike_price': st.column_config.NumberColumn('Strike', format='%.2f'),
        'option_type': st.column_config.TextColumn('Option Type'),
        'estimated_contract_cost_usd': st.column_config.NumberColumn('Estimated Premium', format='$%.2f'),
        'contract_name': st.column_config.TextColumn('Contract Name'),
        'comment': st.column_config.TextColumn('Review Notes'),
        'market_value': st.column_config.NumberColumn('Market Value', format='$%.2f'),
        'unrealized_pl': st.column_config.NumberColumn('Unrealized P&L', format='$%.2f'),
        'unrealized_pl_pct': st.column_config.NumberColumn('Unrealized P&L', format='%.2f%%'),
        'qty': st.column_config.NumberColumn('Shares', format='%.0f'),
        'action': st.column_config.TextColumn('Recommended Action'),
        'reason': st.column_config.TextColumn('Reasoning'),
        'asset_class': st.column_config.TextColumn('Asset Type'),
        'underlying_symbol': st.column_config.TextColumn('Underlying'),
        'close_or_exercise_plan': st.column_config.TextColumn('Auto Exit / Exercise Plan'),
        'decision_window': st.column_config.TextColumn('Decision Window'),
        'days_to_expiration': st.column_config.NumberColumn('Days to Expiry', format='%d'),
        'recent_headline': st.column_config.TextColumn('Latest Headline'),
        'headline': st.column_config.TextColumn('Headline'),
        'catalyst_type': st.column_config.TextColumn('Catalyst Type'),
        'source_symbol': st.column_config.TextColumn('Source Symbol'),
        'conviction_label': st.column_config.TextColumn('Conviction'),
        'crypto_action': st.column_config.TextColumn('Crypto Action'),
        'market_regime': st.column_config.TextColumn('Market Regime'),
        'regime_note': st.column_config.TextColumn('Regime Note'),
        'strategy_bucket': st.column_config.TextColumn('Strategy Bucket'),
        'positions': st.column_config.NumberColumn('Positions', format='%d'),
        'winning_positions': st.column_config.NumberColumn('Winning Positions', format='%d'),
        'losing_positions': st.column_config.NumberColumn('Losing Positions', format='%d'),
        'win_rate': st.column_config.NumberColumn('Win Rate', format='%.2f'),
        'total_market_value': st.column_config.NumberColumn('Market Value', format='$%.2f'),
        'total_unrealized_pl': st.column_config.NumberColumn('Total P&L', format='$%.2f'),
        'avg_unrealized_pl': st.column_config.NumberColumn('Avg P&L', format='$%.2f'),
        'journal_status': st.column_config.TextColumn('Journal Status'),
        'last_execution_status': st.column_config.TextColumn('Latest Execution Status'),
        'last_execution_time': st.column_config.TextColumn('Latest Execution Time'),
        'execution_style': st.column_config.TextColumn('Execution Style'),
        'submitted_orders': st.column_config.NumberColumn('Submitted', format='%d'),
        'filled_orders': st.column_config.NumberColumn('Filled', format='%d'),
        'working_orders': st.column_config.NumberColumn('Working', format='%d'),
        'rejected_orders': st.column_config.NumberColumn('Rejected', format='%d'),
        'fill_rate_pct': st.column_config.NumberColumn('Fill Rate', format='%.2f%%'),
        'rejection_rate_pct': st.column_config.NumberColumn('Rejection Rate', format='%.2f%%'),
        'expected_close_date': st.column_config.TextColumn('Expected Close Date'),
        'risk_posture': st.column_config.TextColumn('Risk Posture'),
        'escalation_action': st.column_config.TextColumn('Escalation'),
        'portfolio_impact_pct': st.column_config.NumberColumn('Impact', format='%.2f%%'),
        'source_url': st.column_config.LinkColumn('Source Link', display_text='Open source'),
        'report_type': st.column_config.TextColumn('Report Type'),
        'access_note': st.column_config.TextColumn('Access Notes'),
        'member': st.column_config.TextColumn('Member'),
        'transaction_type': st.column_config.TextColumn('Transaction Type'),
        'transaction_date': st.column_config.TextColumn('Trade Date'),
        'disclosed_date': st.column_config.TextColumn('Filed Date'),
        'amount_range': st.column_config.TextColumn('Amount Range'),
    }


def build_paper_trade_queue(df: pd.DataFrame, max_positions: int = 5) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    queue = df.copy()
    queue = queue[
        queue['options_setup'].ne('No trade')
        & queue['projected_return_pct'].gt(0)
        & queue['allocation_pct'].gt(0)
    ].copy()

    if queue.empty:
        return pd.DataFrame()

    queue = queue.sort_values(['rank_score', 'projected_return_pct'], ascending=[False, False]).head(max_positions)
    queue['broker_route'] = 'alpaca-paper'
    queue['order_status'] = 'queued_for_review'
    queue['approval_status'] = 'pending'
    queue['approved_for_submit'] = False
    queue['risk_check'] = queue['max_risk_usd'].apply(lambda value: 'pass' if value <= 1500 else 'review')
    queue['last_submitted_at'] = ''
    queue['option_order_status'] = 'queued_for_review'
    queue['last_option_submitted_at'] = ''
    queue['queue_refreshed_at_utc'] = pd.Timestamp.utcnow().isoformat()
    queue['comment'] = 'Review bid/ask spread, open interest, and event risk before sending.'
    return queue[
        [
            'ticker',
            'bias',
            'options_setup',
            'signal_score',
            'projected_return_pct',
            'max_risk_usd',
            'allocation_pct',
            'broker_route',
            'order_status',
            'approval_status',
            'approved_for_submit',
            'risk_check',
            'last_submitted_at',
            'option_order_status',
            'last_option_submitted_at',
            'queue_refreshed_at_utc',
            'comment',
        ]
    ]


def build_position_decision_table(positions: pd.DataFrame, exits: pd.DataFrame) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame()

    exit_map = exits.copy() if not exits.empty else pd.DataFrame(columns=['symbol'])
    decision = positions.merge(exit_map, on='symbol', how='left', suffixes=('', '_exit'))

    for col, default in {
        'asset_class': 'stock',
        'underlying_symbol': '',
        'action': 'hold',
        'reason': 'No decision available yet.',
        'close_or_exercise_plan': 'Review during the next monitoring cycle.',
        'decision_window': 'Next review pending.',
        'expiration_date': '',
        'days_to_expiration': '',
    }.items():
        if col not in decision.columns:
            decision[col] = default

    display = decision[[
        'symbol', 'asset_class', 'underlying_symbol', 'qty', 'market_value', 'unrealized_pl',
        'action', 'close_or_exercise_plan', 'decision_window', 'expiration_date', 'days_to_expiration'
    ]].copy()
    return display.sort_values(['asset_class', 'symbol'])


# ─────────────────────────────────────────────────────────────
# News helpers
# ─────────────────────────────────────────────────────────────

RSS_FEEDS = [
    ('Yahoo Finance', 'https://finance.yahoo.com/rss/topstories'),
    ('CNBC Markets', 'https://www.cnbc.com/id/100003114/device/rss/rss.html'),
    ('MarketWatch', 'https://feeds.marketwatch.com/marketwatch/topstories/'),
    ('Reuters Business', 'https://feeds.reuters.com/reuters/businessNews'),
    ('Seeking Alpha', 'https://seekingalpha.com/market_currents.xml'),
]


@st.cache_data(ttl=600, show_spinner=False)
def fetch_rss_feed(url: str, source_label: str) -> list:
    """Fetch one RSS feed, return list of article dicts. Cached 10 min."""
    try:
        resp = requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        articles = []
        for item in root.findall('.//item')[:20]:
            title = (item.findtext('title') or '').strip()
            link = (item.findtext('link') or '').strip()
            pub_date_str = (item.findtext('pubDate') or '').strip()
            raw_desc = item.findtext('description') or ''
            description = re.sub(r'<[^>]+>', '', raw_desc).strip()[:240]
            if title and link:
                articles.append({
                    'source': source_label,
                    'title': title,
                    'link': link,
                    'pub_date': pub_date_str,
                    'description': description,
                })
        return articles
    except Exception:
        return []


@st.cache_data(ttl=600, show_spinner=False)
def fetch_ticker_news(tickers: tuple) -> dict:
    """Fetch recent yfinance news for each ticker. Cached 10 min."""
    result = {}
    for ticker in tickers:
        try:
            raw = yf.Ticker(ticker).news or []
            parsed = []
            for item in raw[:8]:
                ts = item.get('providerPublishTime') or item.get('published') or 0
                try:
                    age_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime('%b %d %H:%M UTC') if ts else ''
                except Exception:
                    age_str = ''
                parsed.append({
                    'title': item.get('title', ''),
                    'link': item.get('link', ''),
                    'source': item.get('publisher', ''),
                    'pub_date': age_str,
                })
            result[ticker] = parsed
        except Exception:
            result[ticker] = []
    return result


def _news_card(title: str, source: str, pub_date: str, link: str, description: str = '', accent: str = '#3b82f6') -> None:
    """Render a single news article card with consistent styling."""
    desc_html = f'<div style="font-size:0.82rem;color:#94a3b8;margin-top:3px;">{description}</div>' if description else ''
    st.markdown(
        f'<div style="border-left:3px solid {accent};padding:0.5rem 0.8rem;'
        f'margin-bottom:0.55rem;background:#1e293b;border-radius:5px;">'
        f'<div style="font-size:0.9rem;font-weight:600;">'
        f'<a href="{link}" target="_blank" rel="noopener noreferrer" '
        f'style="color:#60a5fa;text-decoration:none;">{title}</a></div>'
        f'<div style="font-size:0.75rem;color:#64748b;margin-top:2px;">{source} · {pub_date}</div>'
        f'{desc_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


_LIGHT_AXIS_FONT = dict(color='#cbd5e1', size=13)
_LIGHT_TICK_FONT = dict(color='#94a3b8', size=12)


def chart_template(fig):
    _axis = dict(
        gridcolor='rgba(100,116,139,0.25)',
        zerolinecolor='rgba(148,163,184,0.4)',
        tickfont=_LIGHT_TICK_FONT,
        title_font=_LIGHT_AXIS_FONT,
    )
    fig.update_layout(
        template='plotly_dark',
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='#1e293b',
        legend_title_text='',
        font=dict(size=13, color='#e2e8f0'),
        title_font=dict(color='#f1f5f9', size=15),
        hoverlabel=dict(bgcolor='#0f172a', font_color='#f8fafc', bordercolor='#334155'),
        margin=dict(l=20, r=20, t=55, b=20),
        xaxis=_axis,
        yaxis=_axis,
    )
    fig.update_xaxes(tickfont=_LIGHT_TICK_FONT, title_font=_LIGHT_AXIS_FONT)
    fig.update_yaxes(tickfont=_LIGHT_TICK_FONT, title_font=_LIGHT_AXIS_FONT)
    return fig


def _axis_title(label: str) -> dict:
    """Return a Plotly axis title dict with enforced light font for dark theme."""
    return dict(text=label, font=_LIGHT_AXIS_FONT)


def load_runtime_settings() -> dict:
    config = TradingConfig.from_env()
    return {
        'approval_mode': 'automatic' if config.approval_mode == 'automatic' else 'manual',
        'auto_submit': bool(config.auto_submit),
        'use_proxy_equities': bool(config.use_proxy_equities),
        'auto_approve_urgent_exits': str(os.getenv('AUTO_APPROVE_URGENT_EXITS', 'true')).strip().lower() in {'1', 'true', 'yes', 'on'},
    }


def persist_runtime_settings(approval_mode: str, auto_submit: bool) -> None:
    approval_mode = 'automatic' if str(approval_mode).strip().lower() == 'automatic' else 'manual'
    desired = {
        'APPROVAL_MODE': approval_mode,
        'AUTO_SUBMIT': 'true' if auto_submit else 'false',
    }

    existing_lines = ENV_PATH.read_text(encoding='utf-8').splitlines() if ENV_PATH.exists() else []
    updated_lines: list[str] = []
    seen: set[str] = set()

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in line:
            updated_lines.append(line)
            continue

        key, _ = line.split('=', 1)
        normalized_key = key.strip()
        if normalized_key in desired:
            updated_lines.append(f"{normalized_key}={desired[normalized_key]}")
            seen.add(normalized_key)
        else:
            updated_lines.append(line)

    for key, value in desired.items():
        if key not in seen:
            updated_lines.append(f"{key}={value}")

    ENV_PATH.write_text('\n'.join(updated_lines).rstrip() + '\n', encoding='utf-8')


def style_ranked_table(df: pd.DataFrame):
    if df.empty:
        return df

    def highlight_row(row: pd.Series) -> list[str]:
        if str(row.get('execution_state', '')).strip().lower() == 'open / executed':
            return ['background-color: rgba(0, 200, 83, 0.18); color: #ecfdf5'] * len(row)
        return [''] * len(row)

    return df.style.apply(highlight_row, axis=1)


def build_ready_and_executed_tables(queue: pd.DataFrame, execution_log: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    queue = ensure_queue_columns(queue.copy()) if not queue.empty else ensure_queue_columns(pd.DataFrame())
    active_statuses = {'new', 'accepted', 'filled', 'partially_filled', 'submitted', 'pending_new'}

    if queue.empty:
        return pd.DataFrame(), pd.DataFrame()

    stock_sent = queue['order_status'].astype(str).str.lower().isin(active_statuses)
    option_sent = queue['option_order_status'].astype(str).str.lower().isin(active_statuses)
    ready = queue.loc[~(stock_sent | option_sent)].copy()
    executed = queue.loc[stock_sent | option_sent].copy()

    if not execution_log.empty:
        log = execution_log.copy()
        base_ticker = log.get('underlying_ticker', pd.Series('', index=log.index)).fillna('').astype(str).str.upper()
        fallback_ticker = log.get('ticker', pd.Series('', index=log.index)).fillna('').astype(str).str.upper()
        log['display_ticker'] = base_ticker.where(base_ticker.ne(''), fallback_ticker)
        latest = log.dropna(subset=['display_ticker']).drop_duplicates(subset=['display_ticker'], keep='last')
        latest = latest[['display_ticker', 'status', 'detail', 'submitted_at_utc', 'execution_style']].rename(
            columns={
                'status': 'last_execution_status',
                'detail': 'last_execution_detail',
                'submitted_at_utc': 'last_execution_time',
            }
        )
        executed = executed.merge(latest, left_on='ticker', right_on='display_ticker', how='left')
        executed = executed.drop(columns=['display_ticker'], errors='ignore')

    return ready, executed


inject_styles()
signals = format_date_columns(ensure_signal_columns(load_latest_csv('latest_signals')))
candidates = format_date_columns(ensure_candidate_columns(load_latest_csv('options_candidates')))
backtest = format_date_columns(load_latest_csv('backtest_summary'))
execution_preview = format_date_columns(load_latest_csv('execution_preview'))
execution_log = format_date_columns(load_latest_csv('execution_log'))
monitor_status = format_date_columns(load_latest_csv('monitor_status'))
broker_account_status = format_date_columns(load_latest_csv('broker_account_status'))
broker_positions = format_date_columns(ensure_positions_columns(load_latest_csv('broker_positions')))
broker_orders = format_date_columns(load_latest_csv('broker_orders'))
performance_journal = format_date_columns(load_latest_csv('performance_journal'))
performance_summary = format_date_columns(load_latest_csv('performance_summary'))
exposure_summary = format_date_columns(load_latest_csv('exposure_summary'))
risk_overview = format_date_columns(load_latest_csv('risk_overview'))
stress_scenarios = format_date_columns(load_latest_csv('stress_scenarios'))
alerts_feed = format_date_columns(load_latest_csv('alerts_feed'))
strategy_attribution = format_date_columns(load_latest_csv('strategy_attribution'))
execution_quality = format_date_columns(load_latest_csv('execution_quality'))
crypto_watchlist = format_date_columns(load_latest_csv('crypto_watchlist'))
market_regime = format_date_columns(load_latest_csv('market_regime'))
opportunity_discovery = format_date_columns(load_latest_csv('opportunity_discovery'))
catalyst_news = format_date_columns(load_latest_csv('catalyst_news'))
congressional_sources = format_date_columns(load_latest_csv('congressional_sources'))
congressional_disclosures = format_date_columns(load_latest_csv('congressional_disclosures'))
congressional_summary = format_date_columns(load_latest_csv('congressional_summary'))
exit_recommendations = format_date_columns(ensure_exit_columns(load_latest_csv('exit_recommendations')))
trade_queue = format_date_columns(ensure_queue_columns(load_latest_csv('paper_trade_queue')))
position_decisions = build_position_decision_table(broker_positions, exit_recommendations)
open_trade_timeline = build_open_trade_timeline(performance_journal if not performance_journal.empty else broker_positions, exit_recommendations)
account_size_usd = float(os.getenv('PAPER_ACCOUNT_SIZE', '100000') or 100000)
if exposure_summary.empty:
    exposure_summary = build_exposure_summary(broker_positions, account_size_usd=account_size_usd)
if risk_overview.empty:
    risk_overview = build_risk_overview(broker_positions, account_size_usd=account_size_usd)
if stress_scenarios.empty:
    stress_scenarios = build_stress_test_table(broker_positions)
if crypto_watchlist.empty:
    crypto_watchlist = build_crypto_watchlist(pd.DataFrame())
if market_regime.empty:
    market_regime = build_market_regime_summary(signals)

st.title('Options Trading AI | Multi Asset Command Center')
st.markdown(
    """
    <div class="dashboard-banner">
        <strong>Desk view:</strong> conviction, expected return, risk at entry, and paper trade readiness in one place.
    </div>
    """,
    unsafe_allow_html=True,
)
st.caption('Professional research view for signals, options structures, and risk screens. These are model estimates, not guarantees.')

if signals.empty or candidates.empty:
    st.warning('No output files found yet. Run the project scanner first to populate the dashboard.')
    st.stop()

st.sidebar.header('Filters')
st.sidebar.caption('Use the controls below to simplify the research view and focus on the highest-quality ideas.')
st.sidebar.markdown(
    '### Operator workflow\n'
    '1. Review the command center\n'
    '2. Check the risk lab\n'
    '3. Approve only clean ideas\n'
    '4. Route paper trades last'
)
if st.sidebar.button('Refresh dashboard data', use_container_width=True):
    st.cache_data.clear()
    st.rerun()

bias_options = sorted(candidates['bias'].dropna().unique())
default_bias = list(bias_options)
selected_bias = st.sidebar.multiselect('Bias', bias_options, default=default_bias)

score_min = int(candidates['signal_score'].min())
score_max = int(candidates['signal_score'].max())
score_default = min(2, score_max)
min_score = st.sidebar.slider('Minimum signal score', min_value=score_min, max_value=score_max, value=score_default)

return_floor = st.sidebar.slider('Minimum projected return %', min_value=-50.0, max_value=250.0, value=0.0, step=5.0)
risk_cap = st.sidebar.slider('Maximum risk per idea ($)', min_value=100, max_value=5000, value=1500, step=100)
actionable_only = st.sidebar.checkbox('Actionable ideas only', value=True)

filtered = candidates[candidates['bias'].isin(selected_bias)].copy()
filtered = filtered[filtered['signal_score'] >= min_score]
filtered = filtered[filtered['projected_return_pct'] >= return_floor]
filtered = filtered[filtered['max_risk_usd'] <= risk_cap]
if actionable_only:
    filtered = filtered[filtered['options_setup'].ne('No trade')]
filtered = filtered.sort_values(['rank_score', 'projected_return_pct'], ascending=[False, False])
filtered = add_execution_status_to_candidates(filtered, broker_positions, execution_log)

best_backtest = float(backtest['win_rate'].max()) if not backtest.empty and 'win_rate' in backtest.columns else 0.0
avg_risk = float(filtered['max_risk_usd'].mean()) if not filtered.empty else 0.0
avg_return = float(filtered['projected_return_pct'].mean()) if not filtered.empty else 0.0
signal_ceiling = max(10.0, float(candidates['signal_score'].abs().max())) if not candidates.empty else 10.0
conviction = min(max(float(filtered['signal_score'].abs().mean()) / signal_ceiling * 100, 0), 100) if not filtered.empty else 0.0

current_investments = float(broker_positions['market_value'].astype(float).sum()) if not broker_positions.empty and 'market_value' in broker_positions.columns else 0.0
open_positions_count = int(len(broker_positions)) if not broker_positions.empty else 0
option_positions_count = int(broker_positions['symbol'].astype(str).str.contains(r'\d{6}[CP]', regex=True, na=False).sum()) if not broker_positions.empty and 'symbol' in broker_positions.columns else 0
stock_positions_count = max(open_positions_count - option_positions_count, 0)
total_unrealized_pl = float(broker_positions['unrealized_pl'].astype(float).sum()) if not broker_positions.empty and 'unrealized_pl' in broker_positions.columns else 0.0
exit_alerts_count = int((exit_recommendations['action'].astype(str).str.lower() != 'hold').sum()) if not exit_recommendations.empty else 0

runtime_settings = load_runtime_settings()
current_market_state = 'Unknown'
if not broker_account_status.empty:
    current_market_state = str(broker_account_status.iloc[0].get('market_status', 'unknown')).title()
elif not monitor_status.empty:
    current_market_state = str(monitor_status.iloc[0].get('market_status', 'unknown')).title()

metric1, metric2, metric3, metric4, metric5, metric6 = st.columns(6)
metric1.metric('Ready Ideas', int((filtered['options_setup'] != 'No trade').sum()) if not filtered.empty else 0)
metric2.metric('Avg Expected Return', f'{avg_return:.1f}%')
metric3.metric('Avg Risk Per Idea', f'${avg_risk:,.0f}')
metric4.metric('Best Backtest Win Rate', f'{best_backtest * 100:.1f}%')
metric5.metric('Paper Holdings Value', f'${current_investments:,.0f}')
metric6.metric('Exit Signals', exit_alerts_count)

st.markdown(
    f"""
    <div class="status-strip">
        <span class="status-pill"><strong>Mode:</strong> Paper</span>
        <span class="status-pill"><strong>Approval:</strong> {'Automatic' if runtime_settings['approval_mode'] == 'automatic' else 'Manual'}</span>
        <span class="status-pill"><strong>Routing:</strong> {'Auto send on' if runtime_settings['auto_submit'] else 'Preview only'}</span>
        <span class="status-pill"><strong>Exit Guardrails:</strong> {'Auto approve urgent' if runtime_settings['auto_approve_urgent_exits'] else 'Manual'}</span>
        <span class="status-pill"><strong>Market:</strong> {current_market_state}</span>
        <span class="status-pill"><strong>Open Positions:</strong> {open_positions_count}</span>
    </div>
    """,
    unsafe_allow_html=True,
)

risk_snapshot = risk_overview.iloc[0] if not risk_overview.empty else {
    'risk_posture': 'Idle',
    'total_exposure_pct': 0.0,
    'largest_sector': 'None',
    'largest_position_pct': 0.0,
}
regime_snapshot = market_regime.iloc[0] if not market_regime.empty else {
    'market_regime': 'unknown',
    'regime_note': 'No regime note available.',
}
next_action = 'Wait for fresh signals.'
if exit_alerts_count > 0:
    next_action = 'Review urgent exits before opening anything new.'
elif not filtered.empty:
    next_action = f"Top idea ready for review: {filtered.iloc[0].get('ticker', 'n/a')}"

command_left, command_mid, command_right = st.columns(3)
command_left.markdown(
    f"""
    <div class="panel-card">
        <div class="panel-kicker">Next action</div>
        <div class="panel-body">{next_action}</div>
    </div>
    """,
    unsafe_allow_html=True,
)
command_mid.markdown(
    f"""
    <div class="panel-card">
        <div class="panel-kicker">Risk posture</div>
        <div class="panel-body">{risk_snapshot.get('risk_posture', 'Idle')} posture with {float(risk_snapshot.get('total_exposure_pct', 0.0)):.1f}% deployed and {str(risk_snapshot.get('largest_sector', 'None'))} as the biggest sector cluster.</div>
    </div>
    """,
    unsafe_allow_html=True,
)
command_right.markdown(
    f"""
    <div class="panel-card">
        <div class="panel-kicker">Multi asset posture</div>
        <div class="panel-body">Regime: {str(regime_snapshot.get('market_regime', 'unknown')).replace('_', ' ').title()}. {'Automatic paper routing is enabled.' if runtime_settings['auto_submit'] else 'Preview only routing is active.'} Current open book P&amp;L: ${total_unrealized_pl:,.0f}.</div>
    </div>
    """,
    unsafe_allow_html=True,
)

if not monitor_status.empty:
    monitor_row = monitor_status.iloc[0]
    m1, m2, m3 = st.columns(3)
    m1.metric('Monitor Status', str(monitor_row.get('monitor_status', 'unknown')).title())
    _last_run_raw = str(monitor_row.get('last_run_utc', ''))
    try:
        _last_run_dt = pd.to_datetime(_last_run_raw, utc=True)
        _now = pd.Timestamp.now(tz='UTC')
        _delta = _now - _last_run_dt
        _mins = int(_delta.total_seconds() // 60)
        if _mins < 60:
            _last_run_label = f'{_mins}m ago'
        elif _mins < 1440:
            _last_run_label = f'{_mins // 60}h {_mins % 60}m ago'
        else:
            _last_run_label = f'{_mins // 1440}d ago — loop may be stopped'
    except Exception:
        _last_run_label = _last_run_raw or 'n/a'
    m2.metric('Last Market Check', _last_run_label)
    m3.metric('Tracked Positions', int(monitor_row.get('open_positions', 0) or 0))
    st.caption(str(monitor_row.get('note', '')))

with st.expander('How to use this dashboard'):
    st.markdown(
        '1. Review the Overview tab for the strongest ideas.\n'
        '2. Use the Advisor tab to understand why a trade was selected and what the exit plan looks like.\n'
        '3. Treat the Congress and catalyst feeds as secondary research inputs, not standalone buy or sell signals.\n'
        '4. Use the Autonomy tab only after reviewing risk, current positions, and broker status.'
    )
    st.write(f'Current paper portfolio P&L snapshot: ${total_unrealized_pl:,.2f}')

overview_tab, radar_tab, risk_tab, advisor_tab, congress_tab, news_tab, auto_tab, account_tab = st.tabs(['Command Center', 'Opportunity Radar', 'Risk Lab', 'Advisor', 'Congress', '📰 News', 'Execution Desk', '📊 Live Account'])

with overview_tab:
    left, right = st.columns([1.5, 1])

    with left:
        st.subheader('Ranked options ideas')
        display_columns = [
            'execution_badge',
            'ticker',
            'bias',
            'signal_score',
            'rank_score',
            'regime_alignment',
            'expiration_target',
            'options_setup',
            'estimated_cost_usd',
            'projected_profit_usd',
            'projected_return_pct',
            'allocation_pct',
        ]
        ranked_table = filtered[display_columns + ['execution_state']].copy()
        st.caption('Green rows indicate ideas that are already open or executed in the paper account.')
        st.dataframe(
            style_ranked_table(make_display_readable(ranked_table)),
            width='stretch',
            hide_index=True,
            column_config=dashboard_column_config(),
        )

    with right:
        st.subheader('Signal conviction gauge')
        gauge = go.Figure(
            go.Indicator(
                mode='gauge+number',
                value=conviction,
                number={'suffix': '%'},
                title={'text': 'Average conviction'},
                gauge={
                    'axis': {'range': [0, 100]},
                    'bar': {'color': '#60a5fa'},
                    'steps': [
                        {'range': [0, 40], 'color': '#1f2937'},
                        {'range': [40, 70], 'color': '#1d4ed8'},
                        {'range': [70, 100], 'color': '#059669'},
                    ],
                },
            )
        )
        chart_template(gauge).update_layout(height=320)
        st.plotly_chart(gauge, width='stretch')
        st.caption(
            f'This gauge summarizes the average strength of the currently visible trade ideas. It uses the absolute signal score scaled against a {signal_ceiling:.0f} point model range. A very high reading means your current filters are showing mostly strong setups, not that outcomes are guaranteed.'
        )

        st.subheader('Professional visuals quants favor')
        st.markdown(
            '- equity curve and drawdown panels\n'
            '- rolling Sharpe and hit-rate monitoring\n'
            '- return vs risk bubble charts for ranking\n'
            '- exposure and sector concentration heatmaps\n'
            '- slippage, fill quality, and regime breakdowns'
        )

        if not filtered.empty:
            top_idea = filtered.iloc[0]
            st.subheader('Best current setup')
            st.success(
                f"{top_idea['ticker']} | {top_idea['options_setup']} | projected return {float(top_idea['projected_return_pct']):.1f}%"
            )
            if 'advisor_note' in filtered.columns:
                st.caption(str(top_idea.get('advisor_note', 'No advisor note available.')))
            if 'refinement_note' in filtered.columns:
                st.caption(str(top_idea.get('refinement_note', '')))

    if not filtered.empty:
        ranked = px.bar(
            filtered.head(10).sort_values('rank_score', ascending=True),
            x='rank_score',
            y='ticker',
            orientation='h',
            color='display_color_label',
            color_discrete_map=EXECUTION_COLORS,
            text='projected_return_pct',
            title='Top ranked setups',
            custom_data=['options_setup', 'estimated_cost_usd', 'max_risk_usd', 'advisor_note', 'execution_state'],
            labels=CHART_LABELS,
        )
        ranked.update_traces(
            texttemplate='%{text:.1f}%  ·  %{customdata[0]}',
            textposition='outside',
            textfont=dict(color='#e2e8f0', size=11),
            hovertemplate=(
                '<b>%{y}</b><br>'
                'Overall Rank: %{x:.2f}<br>'
                'Projected Return: %{text:.1f}%<br>'
                'Options Plan: %{customdata[0]}<br>'
                'Estimated Cost: $%{customdata[1]:,.2f}<br>'
                'Maximum Risk: $%{customdata[2]:,.2f}<br>'
                'Why selected: %{customdata[3]}<br>'
                'Status: %{customdata[4]}<extra></extra>'
            ),
        )
        chart_template(ranked).update_layout(height=420, xaxis_title='Overall Rank', yaxis_title='Ticker')
        st.plotly_chart(ranked, width='stretch')

        selected_ticker = st.selectbox('Open a detailed pick explanation', filtered['ticker'].tolist(), key='overview_pick_selector')
        selected_pick = filtered[filtered['ticker'] == selected_ticker].iloc[0]
        with st.expander(f'Detailed explanation for {selected_ticker}', expanded=False):
            st.markdown(
                f"**Bias:** {selected_pick.get('bias', 'neutral').title()}  \n"
                f"**Options plan:** {selected_pick.get('options_setup', 'No trade')}  \n"
                f"**Regime fit:** {selected_pick.get('regime_alignment', 'neutral').title()}  \n"
                f"**Why this idea stands out:** {selected_pick.get('advisor_note', 'No explanation available.')}  \n"
                f"**Refinement note:** {selected_pick.get('refinement_note', 'No refinement note available.')}  \n"
                f"**Learning note:** {selected_pick.get('learning_note', 'No learning note available.')}  \n"
                f"**Exit plan:** {selected_pick.get('sell_plan', 'No exit plan available.')}"
            )

with radar_tab:
    regime_row = market_regime.iloc[0] if not market_regime.empty else None
    if regime_row is not None:
        st.subheader('Market regime')
        g1, g2, g3 = st.columns(3)
        g1.metric('Regime', str(regime_row.get('market_regime', 'unknown')).replace('_', ' ').title())
        g2.metric('Bullish Breadth', f"{float(regime_row.get('bullish_ratio', 0.0)) * 100:.0f}%")
        g3.metric('Avg Volatility', f"{float(regime_row.get('avg_volatility', 0.0)):.2f}")
        st.caption(str(regime_row.get('regime_note', '')))

    if filtered.empty:
        st.info('No ideas match the current filter set.')
    else:
        top_left, top_right = st.columns(2)

        with top_left:
            scatter = px.scatter(
                filtered,
                x='estimated_cost_usd',
                y='projected_return_pct',
                size='allocation_pct',
                color='bias',
                color_discrete_map=BIAS_COLORS,
                text='ticker',
                custom_data=['signal_score', 'options_setup', 'max_risk_usd', 'advisor_note'],
                title='Opportunity map with clear risk reward zones',
                labels=CHART_LABELS,
            )
            scatter.update_traces(
                textposition='top center',
                textfont=dict(color='#e2e8f0', size=11),
                marker=dict(line=dict(width=1, color='#334155')),
                hovertemplate='<b>%{text}</b><br>Estimated Cost: $%{x:,.2f}<br>Projected Return: %{y:.1f}%<br>Signal Score: %{customdata[0]}<br>Options Plan: %{customdata[1]}<br>Maximum Risk: $%{customdata[2]:,.2f}<br>Why it is selected: %{customdata[3]}<extra></extra>',
            )
            scatter.add_hline(y=0, line_dash='dash', line_color='#94a3b8')
            scatter.add_hline(y=float(filtered['projected_return_pct'].median()), line_dash='dot', line_color='#fbbf24', annotation_text='Median expected return')
            scatter.add_vline(x=float(filtered['estimated_cost_usd'].median()), line_dash='dot', line_color='#38bdf8', annotation_text='Median entry cost')
            chart_template(scatter).update_layout(height=460, xaxis_title='Estimated entry cost', yaxis_title='Projected return percent')
            st.plotly_chart(scatter, width='stretch')
            st.caption('Upper left ideas are generally easier to like because they combine lower entry cost with higher projected return.')

        with top_right:
            score_ladder = px.bar(
                filtered.sort_values(['signal_score', 'rank_score'], ascending=[True, True]).tail(12),
                x='signal_score',
                y='ticker',
                orientation='h',
                color='bias',
                color_discrete_map=BIAS_COLORS,
                text='signal_score',
                title='Signal score ladder',
                labels=CHART_LABELS,
            )
            score_ladder.update_traces(
                textposition='outside',
                textfont=dict(color='#e2e8f0', size=11),
            )
            chart_template(score_ladder).update_layout(height=460, xaxis_title='Signal score', yaxis_title='Ticker')
            st.plotly_chart(score_ladder, width='stretch')
            st.caption('Higher bars indicate stronger model conviction under the current filter set.')

        heatmap_df = filtered[['ticker', 'signal_score', 'projected_return_pct', 'annualized_volatility', 'allocation_pct']].copy() if 'annualized_volatility' in filtered.columns else filtered[['ticker', 'signal_score', 'projected_return_pct', 'allocation_pct']].copy()
        numeric = heatmap_df.set_index('ticker').astype(float).round(2)
        heatmap = px.imshow(
            numeric,
            text_auto='.2f',
            aspect='auto',
            color_continuous_scale='Blues',
            title='Cross sectional ranking matrix',
            labels={'x': 'Metric', 'y': 'Ticker', 'color': 'Value'},
        )
        heatmap.update_traces(textfont=dict(color='#e2e8f0', size=11))
        chart_template(heatmap).update_layout(
            height=420,
            xaxis=dict(tickfont=dict(color='#111827', size=12)),
            yaxis=dict(tickfont=dict(color='#111827', size=12)),
        )
        st.plotly_chart(heatmap, width='stretch')

    st.subheader('Crypto watchlist')
    if crypto_watchlist.empty:
        st.info('Crypto watchlist has not populated yet. Run the multi asset refresh to pull BTC, ETH, SOL, and other liquid names.')
    else:
        crypto_left, crypto_right = st.columns([1.1, 1])
        with crypto_left:
            _crypto_cols = {
                'ticker': st.column_config.TextColumn('Ticker', width='small'),
                'bias': st.column_config.TextColumn('Bias', width='small'),
                'signal_score': st.column_config.NumberColumn('Signal Score', format='%d'),
                'projected_return_pct': st.column_config.NumberColumn('Proj. Return', format='%.1f%%'),
                'annualized_volatility': st.column_config.NumberColumn('Ann. Vol', format='%.2f'),
                'allocation_pct': st.column_config.ProgressColumn('Alloc %', format='%.1f%%', min_value=0, max_value=100),
                'options_setup': st.column_config.TextColumn('Options Plan'),
            }
            st.dataframe(
                make_display_readable(crypto_watchlist),
                hide_index=True,
                use_container_width=True,
                column_config=_crypto_cols,
            )
        with crypto_right:
            _crypto_plot = crypto_watchlist.copy()
            _crypto_plot['bubble_size'] = _crypto_plot['signal_score'].abs().clip(lower=1)
            crypto_chart = px.scatter(
                _crypto_plot,
                x='annualized_volatility',
                y='signal_score',
                color='bias',
                size='bubble_size',
                text='ticker',
                color_discrete_map=BIAS_COLORS,
                title='Crypto conviction map',
            )
            crypto_chart.update_traces(
                textposition='top center',
                textfont=dict(color='#e2e8f0', size=11),
                marker=dict(line=dict(width=1, color='#334155')),
            )
            chart_template(crypto_chart).update_layout(height=360)
            st.plotly_chart(crypto_chart, width='stretch')

with risk_tab:
    risk_snapshot = risk_overview.iloc[0] if not risk_overview.empty else {
        'total_exposure_pct': 0.0,
        'stock_exposure_pct': 0.0,
        'option_exposure_pct': 0.0,
        'largest_position_pct': 0.0,
        'largest_sector': 'None',
        'largest_correlation_bucket': 'None',
        'risk_posture': 'Idle',
    }

    r1, r2, r3, r4 = st.columns(4)
    r1.metric('Risk Posture', str(risk_snapshot.get('risk_posture', 'Idle')))
    r2.metric('Total Exposure', f"{float(risk_snapshot.get('total_exposure_pct', 0.0)):.1f}%")
    r3.metric('Option Exposure', f"{float(risk_snapshot.get('option_exposure_pct', 0.0)):.1f}%")
    r4.metric('Largest Name', f"{float(risk_snapshot.get('largest_position_pct', 0.0)):.1f}%")

    upper_left, upper_right = st.columns([1.2, 1])

    with upper_left:
        if exposure_summary.empty:
            st.info('Portfolio concentration data will populate when broker positions are available.')
        else:
            concentration = px.bar(
                exposure_summary.head(12).sort_values('exposure_pct', ascending=True),
                x='exposure_pct',
                y='underlying_symbol',
                orientation='h',
                color='sector',
                text='exposure_pct',
                title='Portfolio concentration by name and sector',
                custom_data=['correlation_bucket', 'total_market_value'],
            )
            concentration.update_traces(
                texttemplate='%{text:.1f}%',
                textposition='outside',
                hovertemplate='<b>%{y}</b><br>Exposure: %{x:.2f}%<br>Sector: %{marker.color}<br>Correlation Bucket: %{customdata[0]}<br>Market Value: $%{customdata[1]:,.2f}<extra></extra>'
            )
            chart_template(concentration).update_layout(height=430, xaxis_title='Exposure percent of account', yaxis_title='Underlying')
            st.plotly_chart(concentration, width='stretch')
            st.caption('Longer bars signal larger concentration risk and deserve extra review.')

    with upper_right:
        if not stress_scenarios.empty:
            stress_plot = stress_scenarios.copy()
            stress_plot['outcome'] = stress_plot['estimated_pnl'].apply(lambda value: 'Gain' if float(value) >= 0 else 'Loss')
            stress_chart = px.bar(
                stress_plot,
                x='scenario',
                y='estimated_pnl',
                color='outcome',
                text='estimated_pnl',
                color_discrete_map={'Gain': '#00c853', 'Loss': '#ff5252'},
                title='Stress test with estimated profit and loss',
            )
            stress_chart.update_traces(texttemplate='$%{text:,.0f}', textposition='outside')
            stress_chart.add_hline(y=0, line_dash='dash', line_color='#94a3b8')
            chart_template(stress_chart).update_layout(height=430, showlegend=False, xaxis_title='', yaxis_title='Estimated P&L')
            st.plotly_chart(stress_chart, width='stretch')
            st.caption('Red bars show downside shock impact. Green bars show how the book responds in supportive conditions.')
        else:
            st.info('Stress scenarios will populate when positions are loaded.')

    lower_left, lower_right = st.columns([1.05, 1])

    with lower_left:
        if not exposure_summary.empty:
            st.subheader('Risk concentration table')
            st.dataframe(
                make_display_readable(exposure_summary),
                hide_index=True,
                use_container_width=True,
                column_config={
                    'underlying_symbol': st.column_config.TextColumn('Symbol', width='small'),
                    'sector': st.column_config.TextColumn('Sector'),
                    'correlation_bucket': st.column_config.TextColumn('Corr. Bucket'),
                    'asset_family': st.column_config.TextColumn('Asset Class', width='small'),
                    'total_market_value': st.column_config.NumberColumn('Market Value', format='$%.2f'),
                    'position_count': st.column_config.NumberColumn('Positions', format='%d', width='small'),
                    'stock_positions': st.column_config.NumberColumn('Stocks', format='%d', width='small'),
                    'option_positions': st.column_config.NumberColumn('Options', format='%d', width='small'),
                    'exposure_pct': st.column_config.ProgressColumn('Exposure %', format='%.1f%%', min_value=0, max_value=100),
                },
            )
        else:
            st.info('Concentration data will appear once positions are loaded.')

    with lower_right:
        st.subheader('Escalation queue')
        if alerts_feed.empty:
            st.info('No active escalation items right now.')
        else:
            escalation_cols = [col for col in ['symbol', 'alert_type', 'severity', 'escalation_action', 'portfolio_impact_pct', 'reason'] if col in alerts_feed.columns]
            st.dataframe(
                make_display_readable(alerts_feed[escalation_cols]),
                hide_index=True,
                use_container_width=True,
                column_config={
                    'symbol': st.column_config.TextColumn('Ticker', width='small'),
                    'alert_type': st.column_config.TextColumn('Alert Type'),
                    'severity': st.column_config.TextColumn('Severity', width='small'),
                    'escalation_action': st.column_config.TextColumn('Action'),
                    'portfolio_impact_pct': st.column_config.NumberColumn('Impact %', format='%.1f%%', width='small'),
                    'reason': st.column_config.TextColumn('Reason', width='large'),
                },
            )

    if not filtered.empty:
        st.subheader('Entry risk vs projected reward')
        risk_reward = px.scatter(
            filtered,
            x='max_risk_usd',
            y='projected_profit_usd',
            color='bias',
            color_discrete_map=BIAS_COLORS,
            size='allocation_pct',
            text='ticker',
            title='Projected reward plotted against max entry risk',
            custom_data=['options_setup', 'projected_return_pct', 'advisor_note'],
            labels=CHART_LABELS,
        )
        risk_reward.add_hline(y=0, line_dash='dash', line_color='#94a3b8')
        risk_reward.update_traces(
            textposition='top center',
            hovertemplate='<b>%{text}</b><br>Maximum Risk: $%{x:,.2f}<br>Projected Profit: $%{y:,.2f}<br>Projected Return: %{customdata[1]:.1f}%<br>Options Plan: %{customdata[0]}<br>Why it is selected: %{customdata[2]}<extra></extra>',
        )
        chart_template(risk_reward).update_layout(height=420)
        st.plotly_chart(risk_reward, width='stretch')

    with st.expander('How to read the estimator and the new risk desk'):
        st.write(
            'Phase 1 upgrades simplified the workspace into a command center with clearer operator actions. '
            'Stage 2 adds institutional style controls: sector concentration, correlation clustering, stress scenarios, and alert escalation. '
            'Estimated cost is the projected dollar amount required for one contract or spread, while the stress panel estimates how the current book might react to a sudden market shock.'
        )

with advisor_tab:
    analytics_left, analytics_right = st.columns(2)
    with analytics_left:
        st.subheader('Strategy attribution')
        if strategy_attribution.empty:
            st.info('Strategy attribution will populate as the journal refreshes.')
        else:
            render_data_table(strategy_attribution)
    with analytics_right:
        st.subheader('Execution quality')
        if execution_quality.empty:
            st.info('Execution quality will populate as orders are routed and tracked.')
        else:
            render_data_table(execution_quality)

    st.subheader('Trade reasoning')
    if filtered.empty:
        st.info('No current ideas match the filter set.')
    else:
        advisor_cols = [
            'ticker',
            'bias',
            'trade_action',
            'opportunity_source',
            'advisor_note',
            'sell_plan',
            'take_profit_pct',
            'stop_loss_pct',
        ]
        available_cols = [col for col in advisor_cols if col in filtered.columns]
        render_data_table(filtered[available_cols])

    st.subheader('Opportunity discovery feed')
    if opportunity_discovery.empty:
        st.info('Run the discovery feed generator to populate broader sourcing ideas.')
    else:
        render_data_table(opportunity_discovery)

    st.subheader('Recent news and catalyst feed')
    if catalyst_news.empty:
        st.info('No catalyst headlines have been pulled yet.')
    else:
        render_data_table(catalyst_news)

    st.subheader('Short side watchlist')
    short_candidates = filtered[filtered['bias'] == 'bearish'].copy()
    if short_candidates.empty:
        st.info('No strong bearish candidates are active in the current market snapshot.')
    else:
        short_candidates = short_candidates.sort_values(['short_rank_score', 'signal_score'], ascending=[False, True]) if 'short_rank_score' in short_candidates.columns else short_candidates
        short_cols = [col for col in ['ticker', 'short_rank_score', 'signal_score', 'setup_quality', 'options_setup', 'advisor_note', 'sell_plan'] if col in short_candidates.columns]
        render_data_table(short_candidates[short_cols])

    st.subheader('Where to expand opportunity sourcing')
    st.markdown(
        '- earnings calendars and post-earnings drift screens\n'
        '- unusual volume and relative weakness/strength scans\n'
        '- sector rotation and index breadth data\n'
        '- reputable news flow, filings, and analyst revision tracking\n'
        '- curated community idea flow for hypothesis generation only, not blind following'
    )

    st.info(
        'Current trade mapping: bullish signals become call-based structures, bearish signals become put-based structures. '
        'The suggested sell plan is now shown alongside each idea.'
    )

with congress_tab:
    st.subheader('Congressional disclosures intelligence')
    st.caption('Use public disclosure filings as delayed research context only. They are not proof of misconduct and should not be treated as a standalone trading signal.')

    lookback_days = st.select_slider('Recent-trade window', options=[7, 14, 30, 60], value=14)
    min_amount_usd = st.select_slider('Large-trade threshold', options=[15000, 50000, 100000, 250000], value=50000)
    chamber_focus = st.selectbox('Chamber focus', ['All', 'House', 'Senate'], index=0)

    watchlist_view = build_public_interest_watchlist(congressional_disclosures, lookback_days=max(30, lookback_days))
    recent_large_trades = build_recent_large_trades(
        congressional_disclosures,
        lookback_days=lookback_days,
        min_amount_usd=min_amount_usd,
    )

    if chamber_focus != 'All':
        watchlist_view = watchlist_view[watchlist_view['chamber'].fillna('').eq(chamber_focus)]
        recent_large_trades = recent_large_trades[recent_large_trades['chamber'].fillna('').eq(chamber_focus)]

    directory_view = pd.DataFrame(columns=['member', 'chamber', 'recent_big_trades', 'tickers', 'latest_trade_date'])
    if not recent_large_trades.empty:
        directory_view = (
            recent_large_trades.groupby(['member', 'chamber'], dropna=False)
            .agg(
                recent_big_trades=('ticker', 'count'),
                tickers=('ticker', lambda s: ', '.join(sorted({str(value) for value in s if str(value).strip()}))),
                latest_trade_date=('effective_date', 'max'),
            )
            .reset_index()
            .sort_values(['recent_big_trades', 'latest_trade_date', 'member'], ascending=[False, False, True])
        )

    biggest_range = recent_large_trades['amount_range'].iloc[0] if not recent_large_trades.empty else 'n/a'
    metric_left, metric_mid, metric_right, metric_far = st.columns(4)
    metric_left.metric('Recent big trades', int(len(recent_large_trades)))
    metric_mid.metric('Unique members', int(directory_view['member'].nunique()) if not directory_view.empty else 0)
    metric_right.metric('Watchlist hits', int(watchlist_view['recent_trade_count'].sum()) if not watchlist_view.empty else 0)
    metric_far.metric('Largest band seen', str(biggest_range))

    st.subheader('High visibility watchlist')
    st.caption('This list highlights officials that many market observers choose to monitor closely, including Nancy Pelosi. It is for public interest tracking only.')
    if watchlist_view.empty:
        st.info('No watchlist rows match the selected chamber filter yet.')
    else:
        render_data_table(watchlist_view)

    st.subheader(f'Member directory for ${min_amount_usd:,.0f}+ trades in the last {lookback_days} days')
    if directory_view.empty:
        st.info('No locally imported congressional rows match the current recent-window and size filters yet. Import official exports to populate this directory.')
    else:
        render_data_table(directory_view)

    st.subheader('Recent large disclosed trades')
    if recent_large_trades.empty:
        st.info('No recent large disclosed trade rows are loaded yet. Drop CSV or JSON exports into the data/congress_raw folder, then refresh the congressional report feed.')
    else:
        render_data_table(recent_large_trades)

    with st.expander('See normalized ticker summary and raw rows'):
        if congressional_summary.empty:
            st.info('No local congressional summary is available yet.')
        else:
            render_data_table(congressional_summary)

        if not congressional_disclosures.empty:
            render_data_table(congressional_disclosures)

    st.subheader('Official source links')
    if congressional_sources.empty:
        st.info('Congressional disclosure sources have not been refreshed yet.')
    else:
        render_data_table(congressional_sources)

    st.markdown(
        '- House disclosure archives are publicly listed by year.\n'
        '- Senate disclosures are available through the official eFD portal, subject to its access terms.\n'
        '- Best practice: use this page to generate research ideas, then confirm with earnings, valuation, liquidity, and price-action evidence before acting.'
    )

with news_tab:
    # ── Header ──────────────────────────────────────────────────────
    st.subheader('📰 Finance News Center')
    st.caption('Live market news, stock-specific headlines, and catalyst alerts — all in one place. Refreshes every 10 minutes.')

    # ── Manual alerts ───────────────────────────────────────────────
    MANUAL_ALERTS = [
        {
            'date': '2026-04-20',
            'ticker': 'AAPL',
            'headline': 'Tim Cook stepping down as Apple CEO — leadership transition announced.',
            'sentiment': 'bearish',
            'impact': 'high',
            'note': 'CEO transitions historically introduce short-term volatility and repricing of growth premium. Review open AAPL stock position.',
            'source': 'manual',
        },
    ]
    manual_df = pd.DataFrame(MANUAL_ALERTS)

    # Merge with catalyst CSV
    feed_df = catalyst_news.copy() if not catalyst_news.empty else pd.DataFrame()
    if not feed_df.empty:
        for col in ['sentiment', 'impact', 'note', 'source']:
            if col not in feed_df.columns:
                feed_df[col] = ''
        feed_df['date'] = feed_df.get('date', feed_df.get('generated_at_utc', ''))
    combined_news = pd.concat([manual_df, feed_df], ignore_index=True) if not feed_df.empty else manual_df

    # ── Position exposure callout ───────────────────────────────────
    if not broker_positions.empty and 'symbol' in broker_positions.columns:
        flagged_tickers = combined_news['ticker'].dropna().str.upper().unique().tolist() if 'ticker' in combined_news.columns else []
        for fticker in flagged_tickers:
            match = broker_positions[broker_positions['symbol'].astype(str).str.upper() == fticker]
            if not match.empty:
                prow = match.iloc[0]
                st.warning(
                    f'**Open {fticker} position flagged by alert:** {int(float(prow.get("qty", 0)))} shares · '
                    f'market value ${float(prow.get("market_value", 0)):,.2f} · '
                    f'unrealized P&L ${float(prow.get("unrealized_pl", 0)):,.2f}.'
                )

    # ── High-impact alert banners ───────────────────────────────────
    if 'impact' in combined_news.columns:
        high_impact = combined_news[combined_news['impact'].astype(str).str.lower() == 'high']
        if not high_impact.empty:
            st.markdown('#### ⚠️ High-impact alerts')
            for _, row in high_impact.iterrows():
                accent = '#ff5252' if str(row.get('sentiment', '')).lower() == 'bearish' else '#00c853'
                st.markdown(
                    f'<div style="border-left:4px solid {accent};padding:0.6rem 1rem;'
                    f'background:#fff8f0;border-radius:4px;margin-bottom:0.5rem;">'
                    f'<b>{row.get("ticker", "")} — {row.get("headline", row.get("title", ""))}</b><br>'
                    f'<span style="color:#555;font-size:0.85rem">{row.get("note", "")}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    st.divider()

    # ── Inner tabs ──────────────────────────────────────────────────
    live_feed_tab, portfolio_news_tab, alerts_table_tab = st.tabs(['📡 Live Market Feed', '📈 Your Holdings', '🗂 Alerts & Catalysts'])

    with live_feed_tab:
        # Sidebar controls
        ctrl_left, ctrl_right = st.columns([3, 1])
        with ctrl_right:
            refresh_news = st.button('🔄 Refresh feed', use_container_width=True)
            if refresh_news:
                fetch_rss_feed.clear()
        with ctrl_left:
            st.caption(f'Pulling headlines from {len(RSS_FEEDS)} sources — Yahoo Finance, CNBC, MarketWatch, Reuters, Seeking Alpha.')

        # Collect all articles
        all_articles: list = []
        with st.spinner('Loading live news…'):
            for label, url in RSS_FEEDS:
                articles = fetch_rss_feed(url, label)
                all_articles.extend(articles)

        if not all_articles:
            st.info('Could not reach news sources right now. Check your network connection or click Refresh feed.')
        else:
            # Filter controls
            search_left, search_right = st.columns([2, 1])
            with search_left:
                query = st.text_input('Filter headlines', placeholder='Search by keyword…', label_visibility='collapsed')
            with search_right:
                selected_source = st.selectbox(
                    'Source',
                    options=['All sources'] + [label for label, _ in RSS_FEEDS],
                    label_visibility='collapsed',
                )

            filtered_articles = all_articles
            if query:
                filtered_articles = [a for a in filtered_articles if query.lower() in a['title'].lower() or query.lower() in a['description'].lower()]
            if selected_source != 'All sources':
                filtered_articles = [a for a in filtered_articles if a['source'] == selected_source]

            st.caption(f'Showing {len(filtered_articles)} headlines')
            for article in filtered_articles:
                _news_card(
                    title=article['title'],
                    source=article['source'],
                    pub_date=article['pub_date'],
                    link=article['link'],
                    description=article['description'],
                )

    with portfolio_news_tab:
        # Build ticker list: open positions + top-ranked ideas
        position_tickers: list = []
        if not broker_positions.empty and 'symbol' in broker_positions.columns:
            position_tickers = broker_positions['symbol'].astype(str).str.upper().tolist()
        idea_tickers: list = []
        if not filtered.empty and 'ticker' in filtered.columns:
            idea_tickers = filtered['ticker'].head(8).astype(str).str.upper().tolist()
        watch_tickers = list(dict.fromkeys(position_tickers + idea_tickers))[:15]

        if not watch_tickers:
            st.info('No tickers to watch yet. Open positions or ranked trade ideas will appear here once data loads.')
        else:
            selected_ticker = st.selectbox(
                'Pick a ticker to view news',
                options=watch_tickers,
                help='Lists open positions first, then top-ranked ideas.',
            )
            st.caption(f'Fetching recent news for **{selected_ticker}** via Yahoo Finance…')
            with st.spinner(f'Loading {selected_ticker} news…'):
                ticker_news_map = fetch_ticker_news(tuple(watch_tickers))

            ticker_articles = ticker_news_map.get(selected_ticker, [])
            if not ticker_articles:
                st.info(f'No recent news found for {selected_ticker} right now.')
            else:
                for article in ticker_articles:
                    _news_card(
                        title=article['title'],
                        source=article['source'],
                        pub_date=article['pub_date'],
                        link=article['link'],
                        accent='#6366f1',
                    )

            st.divider()
            st.caption('All tickers on your radar:')
            st.markdown('  '.join([f'`{t}`' for t in watch_tickers]))

    with alerts_table_tab:
        st.subheader('Catalyst alert log')
        if combined_news.empty:
            st.info('No catalyst entries yet.')
        else:
            def _highlight_sentiment(row: pd.Series):
                color = 'background-color: #fff0f0' if str(row.get('sentiment', '')).lower() == 'bearish' else (
                    'background-color: #f0fff4' if str(row.get('sentiment', '')).lower() == 'bullish' else ''
                )
                return [color] * len(row)

            st.dataframe(
                combined_news.style.apply(_highlight_sentiment, axis=1),
                hide_index=True,
                use_container_width=True,
                column_config={
                    'ticker': st.column_config.TextColumn('Ticker', width='small'),
                    'date': st.column_config.TextColumn('Date', width='medium'),
                    'headline': st.column_config.TextColumn('Headline', width='large'),
                    'sentiment': st.column_config.TextColumn('Sentiment', width='small'),
                    'impact': st.column_config.TextColumn('Impact', width='small'),
                    'note': st.column_config.TextColumn('Note', width='large'),
                    'source': st.column_config.TextColumn('Source', width='small'),
                },
            )

with auto_tab:
    runtime_settings = load_runtime_settings()

    st.subheader('Autonomy Controls')
    control_left, control_right = st.columns([1.2, 1])
    with control_left:
        selected_approval_mode = st.radio(
            'Approval workflow',
            options=['manual', 'automatic'],
            index=0 if runtime_settings['approval_mode'] == 'manual' else 1,
            format_func=lambda value: 'Manual review before send' if value == 'manual' else 'Automatic approval for ready ideas',
            horizontal=True,
        )
        selected_auto_submit = st.checkbox(
            'Auto send approved paper orders to Alpaca',
            value=runtime_settings['auto_submit'],
            help='Turn this off if you want previews only. Leave it on if you want approved paper orders routed automatically.',
        )
        if st.button('Save automation settings', use_container_width=True):
            persist_runtime_settings(selected_approval_mode, selected_auto_submit)
            st.success('Automation settings saved. Refreshing the dashboard now.')
            st.cache_data.clear()
            st.rerun()

    with control_right:
        st.metric('Approval mode', 'Automatic' if runtime_settings['approval_mode'] == 'automatic' else 'Manual')
        st.metric('Broker send', 'On' if runtime_settings['auto_submit'] else 'Preview only')
        st.caption(
            'Blocked means the idea is still waiting on approval or another readiness gate. '
            'Preview only means automatic sending is off. Skipped existing means duplicate protection prevented a repeat order.'
        )

    st.subheader('Portfolio Concentration')
    if exposure_summary.empty:
        st.info('Exposure summary will appear after the next queue refresh.')
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric('Tracked underlyings', int(len(exposure_summary)))
        c2.metric('Top name exposure', f"{float(exposure_summary['exposure_pct'].max() if not exposure_summary.empty else 0.0):.2f}%")
        c3.metric('Largest name', str(exposure_summary.iloc[0].get('underlying_symbol', 'n/a')) if not exposure_summary.empty else 'n/a')
        render_data_table(exposure_summary)

    st.subheader('Paper Trading Queue')
    queue = trade_queue.copy() if not trade_queue.empty else build_paper_trade_queue(filtered)
    queue = ensure_queue_columns(queue)

    if queue.empty:
        st.info('No current ideas meet the paper trading gate. Lower the filters or regenerate signals.')
        editable_queue = queue
    else:
        st.caption('Use manual mode when you want to review each idea yourself. Use automatic mode when you want eligible non-duplicate ideas approved for paper routing automatically.')
        editable_queue = st.data_editor(
            queue,
            width='stretch',
            hide_index=True,
            column_config=dashboard_column_config(),
            disabled=['ticker', 'bias', 'options_setup', 'signal_score', 'projected_return_pct', 'max_risk_usd', 'allocation_pct', 'broker_route', 'order_status', 'risk_check', 'last_submitted_at', 'option_order_status', 'last_option_submitted_at', 'queue_refreshed_at_utc', 'guardrail_status', 'guardrail_reason', 'current_name_exposure_pct', 'portfolio_exposure_after_trade_pct'],
            key='paper_trade_queue_editor',
        )

        if st.button('Save approval changes', use_container_width=True):
            editable_queue = ensure_queue_columns(editable_queue)
            editable_queue['approval_status'] = editable_queue['approval_status'].astype(str).str.lower().replace({'nan': 'pending'})
            editable_queue['approved_for_submit'] = editable_queue['approved_for_submit'].fillna(False).astype(bool)
            editable_queue.loc[editable_queue['approval_status'].isin(['approved', 'auto_approved']), 'approved_for_submit'] = True
            editable_queue.loc[editable_queue['approval_status'].isin(['rejected', 'hold']), 'approved_for_submit'] = False
            if selected_approval_mode == 'automatic':
                auto_mask = editable_queue['approval_status'].isin(['pending', ''])
                editable_queue.loc[auto_mask, 'approval_status'] = 'auto_approved'
                editable_queue.loc[auto_mask, 'approved_for_submit'] = True
            editable_queue.to_csv(OUTPUT_DIR / 'paper_trade_queue.csv', index=False)
            st.success('Approval settings saved. Eligible names can now move into the next paper execution step.')

        pending_names = editable_queue.loc[editable_queue['approved_for_submit'].eq(False), 'ticker'].astype(str).tolist()
        if pending_names:
            st.info(f"Waiting for review: {', '.join(pending_names[:5])}")

    ready_ideas, executed_ideas = build_ready_and_executed_tables(editable_queue if not editable_queue.empty else queue, execution_log)

    default_end = pd.Timestamp.utcnow().date()
    default_start = (pd.Timestamp.utcnow() - pd.Timedelta(days=7)).date()
    filter_left, filter_right = st.columns(2)
    with filter_left:
        ready_start = st.date_input('Ready ideas from', value=default_start, key='ready_start_date')
    with filter_right:
        ready_end = st.date_input('Through', value=default_end, key='ready_end_date')

    if not ready_ideas.empty and 'queue_refreshed_at_utc' in ready_ideas.columns:
        ready_dates = pd.to_datetime(ready_ideas['queue_refreshed_at_utc'], errors='coerce', utc=True)
        ready_mask = ready_dates.isna() | ((ready_dates.dt.date >= ready_start) & (ready_dates.dt.date <= ready_end))
        ready_ideas = ready_ideas.loc[ready_mask].copy()

    if not executed_ideas.empty and 'last_execution_time' in executed_ideas.columns:
        executed_dates = pd.to_datetime(executed_ideas['last_execution_time'], errors='coerce', utc=True)
        executed_mask = executed_dates.isna() | ((executed_dates.dt.date >= ready_start) & (executed_dates.dt.date <= ready_end))
        executed_ideas = executed_ideas.loc[executed_mask].copy()

    blocked_count = int(execution_log['status'].astype(str).str.lower().eq('blocked').sum()) if not execution_log.empty and 'status' in execution_log.columns else 0
    ready_count = int(len(ready_ideas)) if not ready_ideas.empty else 0
    approved_count = int(ready_ideas['approved_for_submit'].fillna(False).astype(bool).sum()) if not ready_ideas.empty else 0
    executed_count = int(len(executed_ideas)) if not executed_ideas.empty else 0

    stat1, stat2, stat3, stat4 = st.columns(4)
    stat1.metric('Ready Ideas', ready_count)
    stat2.metric('Ready and Approved', approved_count)
    stat3.metric('Ready Ideas Executed', executed_count)
    stat4.metric('Blocked Log Rows', blocked_count)

    ideas_left, ideas_right = st.columns(2)
    with ideas_left:
        st.subheader('Ready Ideas')
        if ready_ideas.empty:
            st.info('No fresh, non duplicate ideas are waiting in the selected date window.')
        else:
            ready_cols = [col for col in ['ticker', 'bias', 'options_setup', 'signal_score', 'projected_return_pct', 'approval_status', 'approved_for_submit', 'queue_refreshed_at_utc', 'comment'] if col in ready_ideas.columns]
            render_data_table(ready_ideas[ready_cols])

    with ideas_right:
        st.subheader('Ready Ideas Executed')
        if executed_ideas.empty:
            st.info('No routed ideas fall inside the selected date window yet.')
        else:
            executed_cols = [col for col in ['ticker', 'bias', 'options_setup', 'order_status', 'option_order_status', 'last_execution_status', 'last_execution_time', 'last_execution_detail'] if col in executed_ideas.columns]
            render_data_table(executed_ideas[executed_cols])

    st.subheader('Broker Status')
    if broker_account_status.empty:
        st.info('Run the broker sync or execution script to populate account status.')
    else:
        broker_row = broker_account_status.iloc[0]
        h1, h2, h3, h4, h5, h6 = st.columns(6)
        h1.metric('Connection', str(broker_row.get('connection_status', 'unknown')).replace('_', ' ').title())
        h2.metric('Market', str(broker_row.get('market_status', 'unknown')).title())
        h3.metric('Equity', f"${float(broker_row.get('equity', 0.0) or 0.0):,.0f}")
        h4.metric('Buying Power', f"${float(broker_row.get('buying_power', 0.0) or 0.0):,.0f}")
        h5.metric('Options Approval', f"Level {int(broker_row.get('options_approved_level', 0) or 0)}")
        h6.metric('Options Trading', f"Level {int(broker_row.get('options_trading_level', 0) or 0)}")
        st.caption(
            f"{str(broker_row.get('detail', ''))} • Options buying power: ${float(broker_row.get('options_buying_power', 0.0) or 0.0):,.0f}"
        )

    preview_col, results_col = st.columns(2)
    with preview_col:
        st.subheader('Execution preview')
        if execution_preview.empty:
            st.info('Run the paper execution script to create broker ready preview orders.')
        else:
            render_data_table(execution_preview)

    with results_col:
        st.subheader('Execution log')
        if execution_log.empty:
            st.info('No broker execution results have been logged yet.')
        else:
            render_data_table(execution_log.tail(10))

    alert_left, alert_right = st.columns([1.2, 1])
    with alert_left:
        st.subheader('Performance Journal')
        if performance_summary.empty:
            st.info('Run the performance journal refresh to populate position by position performance notes.')
        else:
            summary_row = performance_summary.iloc[0]
            p1, p2, p3, p4 = st.columns(4)
            p1.metric('Winning positions', int(summary_row.get('winning_positions', 0) or 0))
            p2.metric('Losing positions', int(summary_row.get('losing_positions', 0) or 0))
            p3.metric('Tracked options', int(summary_row.get('option_positions', 0) or 0))
            p4.metric('Journal P&L', f"${float(summary_row.get('total_unrealized_pl', 0.0) or 0.0):,.2f}")
        if performance_journal.empty:
            st.caption('No journal rows are available yet.')
        else:
            render_data_table(performance_journal)
    with alert_right:
        st.subheader('Priority Alerts')
        if alerts_feed.empty:
            st.info('No urgent alerts are active right now.')
        else:
            render_data_table(alerts_feed)
            st.caption('These alerts reduce daily scanning work by surfacing urgent risk, expiry, and profit watch items automatically.')

    st.subheader('Current Paper Holdings')
    status1, status2, status3, status4, status5 = st.columns(5)
    status1.metric('Open positions', open_positions_count)
    status2.metric('Stock positions', stock_positions_count)
    status3.metric('Options positions', option_positions_count)
    status4.metric('Portfolio market value', f'${current_investments:,.0f}')
    status5.metric('Unrealized P&L', f'${total_unrealized_pl:,.2f}')

    if broker_positions.empty:
        st.info('No open positions are currently synced.')
    else:
        hold_left, hold_right = st.columns([1.2, 1])
        with hold_left:
            render_data_table(broker_positions)
        with hold_right:
            holdings_chart = px.bar(
                broker_positions,
                x='symbol',
                y='market_value',
                color='side',
                title='Current Alpaca holdings by market value',
                text='qty',
            )
            holdings_chart.update_traces(textposition='outside')
            chart_template(holdings_chart).update_layout(height=360)
            st.plotly_chart(holdings_chart, width='stretch')
        st.caption(f'Open synced positions: {open_positions_count}')

    st.subheader('Expected Close Window')
    if open_trade_timeline.empty:
        st.info('Open trade timing guidance will appear here after the next monitoring cycle.')
    else:
        render_data_table(open_trade_timeline)
        st.caption('This box gives the current autonomous estimate for when each open stock or option position should likely be closed or reviewed again.')

    st.subheader('Position Decision Center')
    if position_decisions.empty:
        st.info('Open position close and exercise guidance will appear here after the next monitoring cycle.')
    else:
        render_data_table(position_decisions)
        st.caption('This table shows the current automated view of how each open stock or option position should be managed, closed, or reviewed for possible exercise.')

    st.subheader('Recent broker orders')
    if broker_orders.empty:
        st.info('No broker orders have been synced yet.')
    else:
        render_data_table(broker_orders.head(10))

    st.subheader('Exit recommendations')
    if exit_recommendations.empty:
        st.info('Run the exit evaluation script to populate action suggestions for current holdings.')
    else:
        render_data_table(exit_recommendations)

    st.subheader('Safe path to autonomous trading')
    st.markdown(
        '1. Generate signals and candidate structures.\n'
        '2. Gate every idea through liquidity, max risk, position size rules, and manual approval.\n'
        '3. Build proxy equity orders for paper routing while the options contract layer matures.\n'
        '4. Sync broker account state, fills, positions, and rule violations into the dashboard.\n'
        '5. Enable automatic submission in paper mode first, then move to small live size only after stable forward results.'
    )

    st.subheader('Broker view')
    st.markdown(
        '**Alpaca:** strong for API-first paper trading, fast setup, and a clean developer experience. '
        'Main caution: paper fills are still simulated, so options execution realism can differ from live conditions.\n\n'
        '**Current implementation:** the system uses safe proxy equity orders in paper mode so the execution layer can be validated now and later swapped toward real options routing.\n\n'
        '**Also worth considering:** QuantConnect Lean for research-to-live workflow, Interactive Brokers for deeper production brokerage depth, and Tradier if you want a more options-centric retail API path.'
    )

with account_tab:
    st.subheader('Live Account — Alpaca Paper')

    # ── Top KPI row ──────────────────────────────────────────────────────────
    if not broker_account_status.empty:
        acct = broker_account_status.iloc[0]
        equity_val      = float(acct.get('equity', 0.0) or 0.0)
        cash_val        = float(acct.get('cash', 0.0) or 0.0)
        port_val        = float(acct.get('portfolio_value', 0.0) or 0.0)
        opt_bp          = float(acct.get('options_buying_power', 0.0) or 0.0)
        mkt_status      = str(acct.get('market_status', 'unknown')).title()
        conn_status     = str(acct.get('connection_status', 'unknown')).replace('_', ' ').title()
    else:
        equity_val = cash_val = port_val = opt_bp = 0.0
        mkt_status = conn_status = 'N/A'

    total_pl   = float(broker_positions['unrealized_pl'].apply(pd.to_numeric, errors='coerce').sum()) if not broker_positions.empty else 0.0
    winners    = int((broker_positions['unrealized_pl'].apply(pd.to_numeric, errors='coerce') > 0).sum()) if not broker_positions.empty else 0
    losers     = int((broker_positions['unrealized_pl'].apply(pd.to_numeric, errors='coerce') < 0).sum()) if not broker_positions.empty else 0
    total_pos  = winners + losers
    win_rate   = f"{100 * winners / total_pos:.0f}%" if total_pos > 0 else 'N/A'
    pl_delta   = f"{'▲' if total_pl >= 0 else '▼'} ${abs(total_pl):,.2f}"

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric('Portfolio Value',    f'${port_val:,.0f}')
    k2.metric('Equity',             f'${equity_val:,.0f}')
    k3.metric('Cash',               f'${cash_val:,.0f}')
    k4.metric('Unrealized P&L',     pl_delta)
    k5.metric('Win Rate',           win_rate, help=f'{winners} winners / {losers} losers')
    k6.metric('Market',             mkt_status)

    st.caption(f'Broker: Alpaca paper  |  Connection: {conn_status}  |  Options buying power: ${opt_bp:,.0f}  |  Options level: {int(broker_account_status.iloc[0].get("options_approved_level", 0) or 0) if not broker_account_status.empty else 0}')
    st.divider()

    # ── Split positions: options vs stocks ───────────────────────────────────
    if not broker_positions.empty:
        pos = broker_positions.copy()
        pos['unrealized_pl'] = pd.to_numeric(pos['unrealized_pl'], errors='coerce').fillna(0.0)
        pos['market_value']  = pd.to_numeric(pos['market_value'],  errors='coerce').fillna(0.0)
        pos['qty']           = pd.to_numeric(pos['qty'],           errors='coerce').fillna(0.0)
        pos['pl_pct']        = (pos['unrealized_pl'] / (pos['market_value'] - pos['unrealized_pl'])).replace([float('inf'), float('-inf')], 0.0).fillna(0.0) * 100
        # detect options by OCC symbol pattern (has digits + C/P + digits)
        import re as _re
        pos['is_option'] = pos['symbol'].apply(lambda s: bool(_re.search(r'\d[CP]\d', str(s))))
        options_pos = pos[pos['is_option']].copy()
        stocks_pos  = pos[~pos['is_option']].copy()

        opt_col, stock_col = st.columns(2)

        with opt_col:
            st.subheader(f'Options Positions ({len(options_pos)})')
            if options_pos.empty:
                st.info('No open options positions.')
            else:
                display_opts = options_pos[['symbol', 'qty', 'market_value', 'unrealized_pl', 'pl_pct']].rename(columns={
                    'symbol': 'Contract', 'qty': 'Qty', 'market_value': 'Mkt Value ($)',
                    'unrealized_pl': 'Unrealized P&L ($)', 'pl_pct': 'P&L %',
                })
                st.dataframe(
                    display_opts.style.map(
                        lambda v: 'color: #4ade80' if isinstance(v, (int, float)) and v > 0 else ('color: #f87171' if isinstance(v, (int, float)) and v < 0 else ''),
                        subset=['Unrealized P&L ($)', 'P&L %'],
                    ).format({'Mkt Value ($)': '${:,.2f}', 'Unrealized P&L ($)': '${:,.2f}', 'P&L %': '{:.1f}%'}),
                    use_container_width=True, hide_index=True,
                )
                opts_pl = float(options_pos['unrealized_pl'].sum())
                st.caption(f'Options P&L: {"▲" if opts_pl >= 0 else "▼"} ${abs(opts_pl):,.2f}')

        with stock_col:
            st.subheader(f'Stock Positions ({len(stocks_pos)})')
            if stocks_pos.empty:
                st.info('No open stock positions.')
            else:
                display_stk = stocks_pos[['symbol', 'qty', 'market_value', 'unrealized_pl', 'pl_pct']].rename(columns={
                    'symbol': 'Ticker', 'qty': 'Shares', 'market_value': 'Mkt Value ($)',
                    'unrealized_pl': 'Unrealized P&L ($)', 'pl_pct': 'P&L %',
                })
                st.dataframe(
                    display_stk.style.map(
                        lambda v: 'color: #4ade80' if isinstance(v, (int, float)) and v > 0 else ('color: #f87171' if isinstance(v, (int, float)) and v < 0 else ''),
                        subset=['Unrealized P&L ($)', 'P&L %'],
                    ).format({'Mkt Value ($)': '${:,.2f}', 'Unrealized P&L ($)': '${:,.2f}', 'P&L %': '{:.1f}%'}),
                    use_container_width=True, hide_index=True,
                )
                stk_pl = float(stocks_pos['unrealized_pl'].sum())
                st.caption(f'Stocks P&L: {"▲" if stk_pl >= 0 else "▼"} ${abs(stk_pl):,.2f}')

        st.divider()

        # ── P&L bar chart across all positions ───────────────────────────────
        st.subheader('P&L by Position')
        pl_chart = px.bar(
            pos.sort_values('unrealized_pl'),
            x='symbol', y='unrealized_pl',
            color='unrealized_pl',
            color_continuous_scale=['#f87171', '#94a3b8', '#4ade80'],
            color_continuous_midpoint=0,
            labels={'symbol': 'Symbol', 'unrealized_pl': 'Unrealized P&L ($)'},
            text='unrealized_pl',
        )
        pl_chart.update_traces(texttemplate='$%{text:.0f}', textposition='outside', textfont=dict(color='#e2e8f0'))
        pl_chart.update_coloraxes(showscale=False)
        chart_template(pl_chart).update_layout(height=380, showlegend=False)
        st.plotly_chart(pl_chart, use_container_width=True)

    else:
        st.info('No positions synced yet. The monitor loop will populate this after the next cycle.')

    st.divider()

    # ── Recent orders ────────────────────────────────────────────────────────
    st.subheader('Recent Broker Orders')
    if broker_orders.empty:
        st.info('No broker orders synced yet.')
    else:
        orders_display = broker_orders.copy()
        orders_display['submitted_at'] = pd.to_datetime(orders_display.get('submitted_at', ''), errors='coerce').dt.strftime('%m/%d %H:%M')
        orders_display = orders_display[['symbol', 'side', 'qty', 'status', 'submitted_at']].rename(columns={
            'symbol': 'Symbol', 'side': 'Side', 'qty': 'Qty', 'status': 'Status', 'submitted_at': 'Submitted (UTC)',
        })
        filled_mask  = orders_display['Status'].str.lower() == 'filled'
        accepted_mask = orders_display['Status'].str.lower() == 'accepted'
        st.dataframe(
            orders_display.style.map(
                lambda v: 'color: #4ade80' if str(v).lower() in {'filled'} else ('color: #60a5fa' if str(v).lower() == 'accepted' else ''),
                subset=['Status'],
            ),
            use_container_width=True, hide_index=True,
        )
        st.caption(f"{int(filled_mask.sum())} filled  |  {int(accepted_mask.sum())} accepted  |  {len(orders_display)} total shown")
