from __future__ import annotations

import math

import numpy as np
from scipy.optimize import brentq


MIN_VOL = 1e-4
MAX_VOL = 5.0


def american_price(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    dividend_yield: float,
    sigma: float,
    right: str,
    steps: int = 150,
) -> float:
    if right not in {'C', 'P'}:
        raise ValueError('right must be C or P')
    if min(spot, strike, years, sigma) <= 0 or steps < 1:
        raise ValueError('spot, strike, years, sigma and steps must be positive')
    dt = years / steps
    up = math.exp(sigma * math.sqrt(dt))
    down = 1.0 / up
    growth = math.exp((rate - dividend_yield) * dt)
    probability = (growth - down) / (up - down)
    if not 0.0 < probability < 1.0:
        raise ValueError('Binomial tree is not arbitrage-free for these inputs')
    discount = math.exp(-rate * dt)
    sign = 1.0 if right == 'C' else -1.0

    nodes = np.arange(steps + 1)
    prices = spot * up ** nodes * down ** (steps - nodes)
    values = np.maximum(sign * (prices - strike), 0.0)
    for step in range(steps - 1, -1, -1):
        prices = prices[: step + 1] * up
        continuation = discount * (probability * values[1 : step + 2] + (1.0 - probability) * values[: step + 1])
        values = np.maximum(continuation, sign * (prices - strike))
    return float(values[0])


def american_implied_vol(
    price: float,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    dividend_yield: float,
    right: str,
    steps: int = 150,
) -> float | None:
    if not all(math.isfinite(value) for value in (price, spot, strike, years, rate, dividend_yield)):
        return None
    if min(price, spot, strike, years) <= 0:
        return None
    intrinsic = max(spot - strike, 0.0) if right == 'C' else max(strike - spot, 0.0)
    upper_bound = spot if right == 'C' else strike
    if price < intrinsic or price >= upper_bound:
        return None

    def objective(sigma: float) -> float:
        return american_price(spot, strike, years, rate, dividend_yield, sigma, right, steps) - price

    # CRR needs sigma*sqrt(dt) > |r-q|*dt for valid risk-neutral probabilities.
    floor = max(MIN_VOL, 2.0 * abs(rate - dividend_yield) * math.sqrt(years / steps))
    try:
        low, high = objective(floor), objective(MAX_VOL)
        if low > 0 or high < 0:
            return None
        if low == 0:
            return floor
        return float(brentq(objective, floor, MAX_VOL, xtol=1e-6, maxiter=100))
    except (ValueError, RuntimeError):
        return None
