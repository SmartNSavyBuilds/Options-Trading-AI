from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import requests


HOUSE_DISCLOSURE_URL = 'https://disclosures-clerk.house.gov/PublicDisclosure/FinancialDisclosure'
SENATE_DISCLOSURE_URL = 'https://efdsearch.senate.gov/search/'
EXPECTED_COLUMNS = [
    'chamber',
    'member',
    'ticker',
    'transaction_type',
    'transaction_date',
    'disclosed_date',
    'amount_range',
    'source_note',
]
PUBLIC_INTEREST_WATCHLIST = [
    {'member': 'Nancy Pelosi', 'chamber': 'House'},
    {'member': 'Ro Khanna', 'chamber': 'House'},
    {'member': 'Dan Crenshaw', 'chamber': 'House'},
    {'member': 'Josh Gottheimer', 'chamber': 'House'},
    {'member': 'Tommy Tuberville', 'chamber': 'Senate'},
    {'member': 'Debbie Wasserman Schultz', 'chamber': 'House'},
]
COLUMN_ALIASES = {
    'chamber': ['chamber', 'branch', 'office'],
    'member': ['member', 'representative', 'rep', 'senator', 'politician', 'filer'],
    'ticker': ['ticker', 'symbol', 'stock', 'asset_ticker'],
    'transaction_type': ['transaction_type', 'transaction', 'type', 'tx_type'],
    'transaction_date': ['transaction_date', 'transaction date', 'tx_date', 'trade_date'],
    'disclosed_date': ['disclosed_date', 'notification date', 'filed_date', 'filing_date', 'disclosure_date'],
    'amount_range': ['amount_range', 'amount', 'range', 'value_range'],
    'source_note': ['source_note', 'source', 'note'],
}


def fetch_official_disclosure_sources(limit_years: int = 8) -> pd.DataFrame:
    rows: list[dict[str, object]] = []

    try:
        response = requests.get(HOUSE_DISCLOSURE_URL, timeout=20)
        response.raise_for_status()
        years = sorted(set(re.findall(r'financial-pdfs/(\d{4})FD\.zip', response.text)), reverse=True)[:limit_years]
        if years:
            for year in years:
                rows.append({
                    'chamber': 'House',
                    'report_type': 'Financial Disclosure Archive',
                    'year': int(year),
                    'source_url': f'https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip',
                    'access_note': 'Official House public archive',
                })
        else:
            rows.append({
                'chamber': 'House',
                'report_type': 'Financial Disclosure Portal',
                'year': None,
                'source_url': HOUSE_DISCLOSURE_URL,
                'access_note': 'Official House disclosure portal; archive links can be loaded from the site manually if needed.',
            })
    except Exception as exc:
        rows.append({
            'chamber': 'House',
            'report_type': 'Financial Disclosure Portal',
            'year': None,
            'source_url': HOUSE_DISCLOSURE_URL,
            'access_note': f'House source fetch error: {exc}',
        })

    rows.append({
        'chamber': 'Senate',
        'report_type': 'eFD Search Portal',
        'year': None,
        'source_url': SENATE_DISCLOSURE_URL,
        'access_note': 'Official Senate portal; user must comply with access terms before using reports.',
    })

    return pd.DataFrame(rows)


def create_local_trade_template(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = data_dir / 'congress_raw'
    raw_dir.mkdir(parents=True, exist_ok=True)

    sample_template = raw_dir / 'sample_congress_import_template.csv'
    if not sample_template.exists():
        pd.DataFrame(
            columns=['Representative', 'Ticker', 'Transaction', 'Transaction Date', 'Notification Date', 'Amount']
        ).to_csv(sample_template, index=False)

    template_path = data_dir / 'congress_disclosures.csv'
    if not template_path.exists():
        pd.DataFrame(columns=EXPECTED_COLUMNS).to_csv(template_path, index=False)
    return template_path


def _normalize_col_name(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(name).strip().lower())


def _match_column(columns: list[str], aliases: list[str]) -> str | None:
    normalized = {_normalize_col_name(col): col for col in columns}
    for alias in aliases:
        hit = normalized.get(_normalize_col_name(alias))
        if hit:
            return hit
    return None


def _infer_chamber(source_name: str) -> str:
    label = str(source_name).lower()
    if 'senate' in label:
        return 'Senate'
    if 'house' in label:
        return 'House'
    return ''


def _extract_ticker(value: object) -> str:
    text = str(value or '').upper()
    if not text:
        return ''
    paren_match = re.search(r'\(([A-Z]{1,5})\)', text)
    if paren_match:
        return paren_match.group(1)
    tokens = re.findall(r'\b[A-Z]{1,5}\b', text)
    for token in tokens:
        if token not in {'USD', 'ETF', 'INC', 'CORP', 'LLC'}:
            return token
    return ''


def _parse_amount_range(value: object) -> tuple[int, int]:
    text = str(value or '')
    numbers = [int(match.replace(',', '')) for match in re.findall(r'\$?([\d,]+)', text)]
    if not numbers:
        return 0, 0
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return min(numbers), max(numbers)


def enrich_disclosures(disclosures: pd.DataFrame) -> pd.DataFrame:
    if disclosures.empty:
        return pd.DataFrame(columns=EXPECTED_COLUMNS + ['effective_date', 'amount_low_usd', 'amount_high_usd', 'amount_mid_usd'])

    clean = disclosures.copy()
    for col in EXPECTED_COLUMNS:
        if col not in clean.columns:
            clean[col] = ''

    clean['member'] = clean['member'].astype(str).str.strip()
    clean['ticker'] = clean['ticker'].astype(str).str.upper().str.strip()
    clean['transaction_type'] = clean['transaction_type'].astype(str).str.strip()
    clean['chamber'] = clean['chamber'].astype(str).str.strip().replace('', 'Unknown')
    clean['transaction_date'] = pd.to_datetime(clean['transaction_date'], errors='coerce')
    clean['disclosed_date'] = pd.to_datetime(clean['disclosed_date'], errors='coerce')
    clean['effective_date'] = clean['transaction_date'].combine_first(clean['disclosed_date'])
    bounds = clean['amount_range'].map(_parse_amount_range)
    clean['amount_low_usd'] = bounds.map(lambda pair: pair[0])
    clean['amount_high_usd'] = bounds.map(lambda pair: pair[1])
    clean['amount_mid_usd'] = (clean['amount_low_usd'] + clean['amount_high_usd']) / 2
    return clean


def build_recent_large_trades(
    disclosures: pd.DataFrame,
    lookback_days: int = 14,
    min_amount_usd: int = 50000,
    reference_date: str | None = None,
) -> pd.DataFrame:
    clean = enrich_disclosures(disclosures)
    if clean.empty:
        return pd.DataFrame(columns=['member', 'chamber', 'ticker', 'transaction_type', 'effective_date', 'amount_range', 'days_ago', 'source_note'])

    ref = pd.Timestamp(reference_date).normalize() if reference_date else pd.Timestamp.now().normalize()
    cutoff = ref - pd.Timedelta(days=max(1, lookback_days))
    filtered = clean[(clean['effective_date'] >= cutoff) & (clean['amount_high_usd'] >= min_amount_usd)].copy()
    if filtered.empty:
        return pd.DataFrame(columns=['member', 'chamber', 'ticker', 'transaction_type', 'effective_date', 'amount_range', 'days_ago', 'source_note'])

    filtered['days_ago'] = (ref - filtered['effective_date']).dt.days.clip(lower=0)
    filtered['effective_date'] = filtered['effective_date'].dt.strftime('%Y-%m-%d')
    filtered['amount_range'] = filtered['amount_range'].replace('', 'Not reported')
    columns = ['member', 'chamber', 'ticker', 'transaction_type', 'effective_date', 'amount_range', 'days_ago', 'source_note']
    return filtered[columns].sort_values(['days_ago', 'member', 'ticker'], ascending=[True, True, True]).reset_index(drop=True)


def build_public_interest_watchlist(
    disclosures: pd.DataFrame,
    watchlist: list[dict[str, str]] | None = None,
    lookback_days: int = 60,
    reference_date: str | None = None,
) -> pd.DataFrame:
    watch_items = watchlist or PUBLIC_INTEREST_WATCHLIST
    clean = enrich_disclosures(disclosures)
    ref = pd.Timestamp(reference_date).normalize() if reference_date else pd.Timestamp.now().normalize()
    cutoff = ref - pd.Timedelta(days=max(1, lookback_days))

    rows: list[dict[str, object]] = []
    for item in watch_items:
        member_name = item.get('member', '').strip()
        chamber = item.get('chamber', '').strip()
        if clean.empty:
            member_rows = pd.DataFrame()
        else:
            member_rows = clean[clean['member'].astype(str).str.contains(re.escape(member_name), case=False, na=False)].copy()
        recent_rows = member_rows[member_rows['effective_date'] >= cutoff].copy() if not member_rows.empty else pd.DataFrame()
        big_rows = recent_rows[recent_rows['amount_high_usd'] >= 50000].copy() if not recent_rows.empty else pd.DataFrame()
        latest = member_rows.sort_values('effective_date', ascending=False).head(1) if not member_rows.empty else pd.DataFrame()

        latest_ticker = latest['ticker'].iloc[0] if not latest.empty else ''
        latest_trade_type = latest['transaction_type'].iloc[0] if not latest.empty else ''
        latest_trade_date = latest['effective_date'].dt.strftime('%Y-%m-%d').iloc[0] if not latest.empty else ''
        latest_amount = latest['amount_range'].iloc[0] if not latest.empty else ''

        watch_status = 'Monitoring'
        if len(big_rows) > 0:
            watch_status = 'Recent large disclosure'
        elif len(recent_rows) > 0:
            watch_status = 'Recent disclosure'

        rows.append(
            {
                'member': member_name,
                'chamber': chamber or (latest['chamber'].iloc[0] if not latest.empty else ''),
                'recent_trade_count': int(len(recent_rows)),
                'recent_big_trade_count': int(len(big_rows)),
                'latest_ticker': latest_ticker,
                'latest_trade_type': latest_trade_type,
                'latest_trade_date': latest_trade_date,
                'latest_amount_range': latest_amount,
                'watch_status': watch_status,
            }
        )

    return pd.DataFrame(rows).sort_values(['recent_big_trade_count', 'recent_trade_count', 'member'], ascending=[False, False, True]).reset_index(drop=True)


def normalize_trade_frame(df: pd.DataFrame, source_note: str = '') -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=EXPECTED_COLUMNS)

    clean = df.copy()
    result = pd.DataFrame(index=clean.index)
    for target, aliases in COLUMN_ALIASES.items():
        match = _match_column(list(clean.columns), aliases)
        result[target] = clean[match] if match else ''

    if 'ticker' not in result.columns or not result['ticker'].astype(str).str.strip().any():
        asset_match = _match_column(list(clean.columns), ['asset', 'asset_description', 'description', 'asset_name'])
        if asset_match:
            result['ticker'] = clean[asset_match].map(_extract_ticker)
    else:
        result['ticker'] = result['ticker'].map(_extract_ticker)

    result['chamber'] = result['chamber'].replace('', _infer_chamber(source_note))
    result['source_note'] = result['source_note'].replace('', f'imported_from:{source_note}' if source_note else 'manual_entry')
    result = result.fillna('')
    result['ticker'] = result['ticker'].astype(str).str.upper().str.replace(r'[^A-Z.]', '', regex=True)
    result['member'] = result['member'].astype(str).str.strip()
    result = result[EXPECTED_COLUMNS]
    result = result[(result['ticker'] != '') | (result['member'] != '')]
    return result.drop_duplicates().reset_index(drop=True)


def _read_raw_trade_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == '.csv':
        return pd.read_csv(path)
    if suffix == '.json':
        payload = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(payload, list):
            return pd.DataFrame(payload)
        if isinstance(payload, dict):
            for key in ['results', 'data', 'items', 'rows']:
                if isinstance(payload.get(key), list):
                    return pd.DataFrame(payload[key])
            return pd.DataFrame([payload])
    if suffix in {'.xlsx', '.xls'}:
        return pd.read_excel(path)
    return pd.DataFrame()


def import_raw_trade_data(data_dir: Path) -> pd.DataFrame:
    create_local_trade_template(data_dir)
    raw_dir = data_dir / 'congress_raw'
    frames: list[pd.DataFrame] = []

    for path in sorted(raw_dir.iterdir()):
        if not path.is_file() or path.name.startswith('sample_congress_import_template'):
            continue
        try:
            raw = _read_raw_trade_file(path)
            normalized = normalize_trade_frame(raw, source_note=path.name)
            if not normalized.empty:
                frames.append(normalized)
        except Exception:
            continue

    if not frames:
        return pd.DataFrame(columns=EXPECTED_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates().reset_index(drop=True)
    return combined


def load_local_trade_data(data_dir: Path) -> pd.DataFrame:
    template_path = create_local_trade_template(data_dir)
    try:
        df = pd.read_csv(template_path)
    except Exception:
        return pd.DataFrame(columns=EXPECTED_COLUMNS)
    return normalize_trade_frame(df, source_note='manual_template')


def build_trade_summary(disclosures: pd.DataFrame) -> pd.DataFrame:
    if disclosures.empty:
        return pd.DataFrame(columns=['ticker', 'buy_count', 'sell_count', 'net_signal'])

    clean = disclosures.copy()
    clean['ticker'] = clean['ticker'].astype(str).str.upper()
    buy_mask = clean['transaction_type'].astype(str).str.contains('purchase|buy', case=False, na=False)
    sell_mask = clean['transaction_type'].astype(str).str.contains('sale|sell', case=False, na=False)

    summary = clean.groupby('ticker').agg(
        buy_count=('ticker', lambda s: int(buy_mask.loc[s.index].sum())),
        sell_count=('ticker', lambda s: int(sell_mask.loc[s.index].sum())),
    ).reset_index()
    summary['net_signal'] = summary['buy_count'] - summary['sell_count']
    return summary.sort_values(['net_signal', 'buy_count'], ascending=[False, False])


def refresh_congressional_outputs(project_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    outputs = project_dir / 'outputs'
    data_dir = project_dir / 'data'
    outputs.mkdir(exist_ok=True)

    sources = fetch_official_disclosure_sources()
    manual = load_local_trade_data(data_dir)
    imported = import_raw_trade_data(data_dir)
    disclosures = pd.concat([manual, imported], ignore_index=True) if not imported.empty else manual
    disclosures = disclosures.drop_duplicates().reset_index(drop=True)
    summary = build_trade_summary(disclosures)
    recent_large = build_recent_large_trades(disclosures)
    watchlist = build_public_interest_watchlist(disclosures)

    disclosures.to_csv(data_dir / 'congress_disclosures.csv', index=False)
    sources.to_csv(outputs / 'congressional_sources.csv', index=False)
    disclosures.to_csv(outputs / 'congressional_disclosures.csv', index=False)
    summary.to_csv(outputs / 'congressional_summary.csv', index=False)
    recent_large.to_csv(outputs / 'congressional_recent_large.csv', index=False)
    watchlist.to_csv(outputs / 'congressional_watchlist.csv', index=False)
    return sources, disclosures, summary
