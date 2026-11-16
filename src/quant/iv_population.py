from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import utc_now_ms
from .db import connect, init_schema


IV_METHOD = 'atm_call_spot_total_variance_interp_v1'
TENOR_DAYS = 30


def historical_percentile(
    prior_values: Iterable[float],
    current_iv: float,
    *,
    window_size: int = 252,
    min_observations: int = 120,
) -> float | None:
    current = float(current_iv)
    if not math.isfinite(current) or current <= 0:
        raise ValueError('Current implied volatility must be positive and finite')
    if window_size <= 0 or min_observations <= 0 or min_observations > window_size:
        raise ValueError('Percentile window and minimum observation counts are invalid')
    valid = [float(value) for value in prior_values if math.isfinite(float(value)) and float(value) > 0]
    history = valid[-window_size:]
    if len(history) < min_observations:
        return None
    return 100.0 * sum(value <= current for value in history) / len(history)


def calculate_iv_percentile(
    database: Path,
    source: str,
    symbol: str,
    current_iv: float,
    as_of_ts_ms: int,
    *,
    tenor_days: int = TENOR_DAYS,
    method: str = IV_METHOD,
    window_size: int = 252,
    min_observations: int = 120,
) -> tuple[float | None, int]:
    if not database.is_file():
        return None, 0
    init_schema(database)
    connection = connect(database)
    try:
        rows = connection.execute(
            '''SELECT iv FROM iv_observations
            WHERE source=? AND symbol=? AND tenor_days=? AND method=? AND obs_ts_ms<?
            ORDER BY obs_ts_ms DESC LIMIT ?''',
            (source, symbol.strip().upper(), tenor_days, method, as_of_ts_ms, window_size),
        ).fetchall()
    except Exception:
        return None, 0
    finally:
        connection.close()
    prior = [float(row['iv']) for row in reversed(rows)]
    valid_count = len([value for value in prior if math.isfinite(value) and value > 0])
    return historical_percentile(prior, current_iv, window_size=window_size,
                                  min_observations=min_observations), valid_count


def derive_atm_30d_observations(
    database: Path,
    *,
    source: str | None = None,
    symbols: Iterable[str] | None = None,
    max_spread_pct: float = 0.06,
    max_moneyness_pct: float = 0.10,
    anomaly_z: float = 5.0,
) -> int:
    if not math.isfinite(max_spread_pct) or max_spread_pct < 0:
        raise ValueError('max_spread_pct must be finite and nonnegative')
    if not math.isfinite(max_moneyness_pct) or max_moneyness_pct <= 0:
        raise ValueError('max_moneyness_pct must be finite and positive')
    if not math.isfinite(anomaly_z) or anomaly_z <= 0:
        raise ValueError('anomaly_z must be finite and positive')
    if not database.is_file():
        return 0
    init_schema(database)

    symbol_set = {str(symbol).strip().upper() for symbol in symbols or () if str(symbol).strip()}
    connection = connect(database)
    try:
        query = "SELECT * FROM option_chain_snapshots WHERE interval='eod'"
        params: list[Any] = []
        if source:
            query += ' AND source=?'
            params.append(source)
        if symbol_set:
            placeholders = ','.join('?' for _ in symbol_set)
            query += f' AND underlying IN ({placeholders})'
            params.extend(sorted(symbol_set))
        query += ' ORDER BY source, underlying, ts_ms'
        rows = connection.execute(query, params).fetchall()
        grouped: dict[tuple[str, str, date], list[Any]] = defaultdict(list)
        for row in rows:
            if not row['underlying']:
                continue
            observed_date = datetime.fromtimestamp(row['ts_ms'] / 1000, tz=timezone.utc).date()
            grouped[(row['source'], str(row['underlying']).upper(), observed_date)].append(row)

        observations = []
        for (row_source, symbol, observed_date), day_rows in grouped.items():
            result = _derive_one_day(
                day_rows, observed_date, max_spread_pct, max_moneyness_pct, anomaly_z,
            )
            if result is None:
                continue
            obs_ts_ms, implied_volatility, provenance = result
            observations.append((
                row_source, symbol, obs_ts_ms, TENOR_DAYS, IV_METHOD, implied_volatility,
                '[]', json.dumps(provenance, sort_keys=True, separators=(',', ':')), utc_now_ms(),
            ))
        if not observations:
            return 0
        with connection:
            connection.executemany(
                '''INSERT INTO iv_observations (
                    source, symbol, obs_ts_ms, tenor_days, method, iv, quality_flags, extra_json, ingested_ts_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, symbol, obs_ts_ms, tenor_days, method) DO UPDATE SET
                    iv=excluded.iv,
                    quality_flags=excluded.quality_flags,
                    extra_json=excluded.extra_json,
                    ingested_ts_ms=excluded.ingested_ts_ms''',
                observations,
            )
        return len(observations)
    finally:
        connection.close()


def populate_iv_percentiles(
    database: Path,
    *,
    source: str | None = None,
    symbols: Iterable[str] | None = None,
    tenor_days: int = TENOR_DAYS,
    method: str = IV_METHOD,
    window_size: int = 252,
    min_observations: int = 120,
) -> int:
    if window_size <= 0 or min_observations <= 0 or min_observations > window_size:
        raise ValueError('Percentile window and minimum observation counts are invalid')
    if not database.is_file():
        return 0
    init_schema(database)
    symbol_set = {str(symbol).strip().upper() for symbol in symbols or () if str(symbol).strip()}
    connection = connect(database)
    try:
        query = '''SELECT source, symbol, obs_ts_ms, tenor_days, method, iv FROM iv_observations
            WHERE tenor_days=? AND method=?'''
        params: list[Any] = [tenor_days, method]
        if source:
            query += ' AND source=?'
            params.append(source)
        if symbol_set:
            placeholders = ','.join('?' for _ in symbol_set)
            query += f' AND symbol IN ({placeholders})'
            params.extend(sorted(symbol_set))
        query += ' ORDER BY source, symbol, obs_ts_ms'
        rows = connection.execute(query, params).fetchall()
        groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for row in rows:
            groups[(row['source'], row['symbol'])].append(row)

        computed_ts_ms = utc_now_ms()
        ranks = []
        for (row_source, symbol), series in groups.items():
            values = [float(row['iv']) for row in series]
            for index, row in enumerate(series):
                prior = values[max(0, index - window_size):index]
                percentile = historical_percentile(
                    prior, values[index], window_size=window_size,
                    min_observations=min_observations,
                )
                if percentile is None:
                    continue
                ranks.append((
                    row_source, symbol, row['obs_ts_ms'], tenor_days, method, window_size,
                    min(len(prior), window_size), percentile, computed_ts_ms,
                ))
        if not ranks:
            return 0
        with connection:
            connection.executemany(
                '''INSERT INTO iv_percentiles (
                    source, symbol, obs_ts_ms, tenor_days, method, window_size,
                    observations_used, percentile, computed_ts_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, symbol, obs_ts_ms, tenor_days, method, window_size) DO UPDATE SET
                    observations_used=excluded.observations_used,
                    percentile=excluded.percentile,
                    computed_ts_ms=excluded.computed_ts_ms''',
                ranks,
            )
        return len(ranks)
    finally:
        connection.close()


def _derive_one_day(
    rows: list[Any],
    observed_date: date,
    max_spread_pct: float,
    max_moneyness_pct: float,
    anomaly_z: float,
) -> tuple[int, float, dict[str, Any]] | None:
    spots = [float(row['underlying_px']) for row in rows if _positive_finite(row['underlying_px'])]
    if not spots:
        return None
    spot = statistics.median(spots)
    candidates: dict[date, list[tuple[Any, float, int]]] = defaultdict(list)
    for row in rows:
        if row['right'] != 'C' or not row['expiry'] or not _positive_finite(row['strike']) or not _positive_finite(row['iv']):
            continue
        bid, ask = row['bid'], row['ask']
        if not _positive_finite(bid) or not _positive_finite(ask) or float(ask) < float(bid):
            continue
        mid = (float(bid) + float(ask)) / 2
        spread_pct = (float(ask) - float(bid)) / mid if mid > 0 else math.inf
        if spread_pct > max_spread_pct:
            continue
        if abs(float(row['strike']) - spot) / spot > max_moneyness_pct:
            continue
        try:
            expiry = date.fromisoformat(str(row['expiry'])[:10])
        except ValueError:
            continue
        dte = (expiry - observed_date).days
        iv = float(row['iv'])
        if dte <= 0 or not math.isfinite(iv) or not 0 < iv <= 5:
            continue
        flags = _quality_flags(row['quality_flags'])
        if {'missing_quote', 'nonpositive_quote', 'crossed_quote', 'iv_out_of_range'} & flags:
            continue
        candidates[expiry].append((row, iv, dte))

    expiry_points = []
    for expiry, expiry_rows in candidates.items():
        filtered = _filter_iv_outliers(expiry_rows, anomaly_z)
        if not filtered:
            continue
        nearest_distance = min(abs(float(row['strike']) - spot) for row, _, _ in filtered)
        nearest = [item for item in filtered if abs(float(item[0]['strike']) - spot) == nearest_distance]
        dte = nearest[0][2]
        iv = statistics.median(item[1] for item in nearest)
        timestamp = max(int(item[0]['ts_ms']) for item in nearest)
        expiry_points.append((dte, iv, timestamp, expiry.isoformat()))

    if not expiry_points:
        return None
    below = [point for point in expiry_points if point[0] < TENOR_DAYS]
    above = [point for point in expiry_points if point[0] > TENOR_DAYS]
    exact = [point for point in expiry_points if point[0] == TENOR_DAYS]
    if exact:
        low_point = high_point = exact[0]
        atm_iv = exact[0][1]
    elif below and above:
        low_point = max(below, key=lambda point: point[0])
        high_point = min(above, key=lambda point: point[0])
        low_variance = low_point[1] ** 2 * low_point[0] / 365
        high_variance = high_point[1] ** 2 * high_point[0] / 365
        weight = (TENOR_DAYS - low_point[0]) / (high_point[0] - low_point[0])
        target_variance = low_variance + weight * (high_variance - low_variance)
        atm_iv = math.sqrt(target_variance / (TENOR_DAYS / 365))
    else:
        return None
    if not math.isfinite(atm_iv) or atm_iv <= 0:
        return None
    obs_ts_ms = max(low_point[2], high_point[2])
    provenance_points = [low_point] if low_point is high_point else [low_point, high_point]
    provenance = {
        'algorithm': IV_METHOD,
        'spot_reference': spot,
        'expiry_points': [
            {'expiry': point[3], 'dte': point[0], 'iv': point[1]} for point in provenance_points
        ],
        'max_spread_pct': max_spread_pct,
        'max_moneyness_pct': max_moneyness_pct,
        'anomaly_z': anomaly_z,
    }
    return obs_ts_ms, atm_iv, provenance


def _filter_iv_outliers(rows: list[tuple[Any, float, int]], anomaly_z: float) -> list[tuple[Any, float, int]]:
    if len(rows) < 4:
        return rows
    values = [item[1] for item in rows]
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    if mad == 0:
        return [item for item in rows if item[1] == median]
    return [item for item in rows if 0.6745 * abs(item[1] - median) / mad <= anomaly_z]


def _quality_flags(value: str | None) -> set[str]:
    try:
        parsed = json.loads(value or '[]')
        return set(parsed) if isinstance(parsed, list) else set()
    except (TypeError, ValueError):
        return {'invalid_quality_flags'}


def _positive_finite(value: Any) -> bool:
    try:
        numeric = float(value)
        return math.isfinite(numeric) and numeric > 0
    except (TypeError, ValueError):
        return False