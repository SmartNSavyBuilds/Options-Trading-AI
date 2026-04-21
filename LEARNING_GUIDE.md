# Mathematical Foundations of the Options Trading AI Project

## Introduction

This document explains the mathematical logic behind the current project in a more formal and educational style. It is intended as a collegiate learning companion for anyone studying quantitative trading, option structure selection, and predictive signal design.

---

## 1. Return Series Construction

Let the closing price at time $t$ be $P_t$. A simple return can be written as:

$$
r_t = \frac{P_t - P_{t-1}}{P_{t-1}}
$$

This converts raw price movement into a standardized percentage measure so that different tickers can be compared more meaningfully.

---

## 2. Trend Estimation Using Moving Averages

A moving average smooths short-term noise and helps identify whether price is consistently above or below its recent mean.

For an $n$-period simple moving average:

$$
SMA_n(t) = \frac{1}{n} \sum_{i=0}^{n-1} P_{t-i}
$$

In the project, short- and medium-horizon averages are compared. A bullish trend condition is inferred when price is above both averages and the short average is itself above the medium average.

---

## 3. Relative Strength Index

The RSI is a bounded oscillator used to measure recent directional strength. First compute average gains and losses over a window. Then define:

$$
RS = \frac{\text{Average Gain}}{\text{Average Loss}}
$$

and

$$
RSI = 100 - \frac{100}{1 + RS}
$$

High RSI values suggest strong upside momentum; low values suggest downside weakness.

---

## 4. Annualized Volatility

Volatility is estimated from the standard deviation of returns. If $\sigma_d$ is the daily standard deviation of returns, then annualized volatility is approximated by:

$$
\sigma_{ann} = \sigma_d \sqrt{252}
$$

The factor $252$ is a common approximation for the number of trading days in a year.

This measure is important because option prices are highly sensitive to expected variability, not just direction.

---

## 5. Expected Move Approximation

A basic expected move over $T$ trading days can be approximated as:

$$
EM = S \cdot \sigma \cdot \sqrt{\frac{T}{252}}
$$

where:
- $S$ is the current underlying price
- $\sigma$ is annualized volatility
- $T$ is days to expiration

This is used in the project to estimate how much the underlying might reasonably move within the targeted option life.

---

## 6. Signal Score Construction

The current MVP uses a rule-based signal score rather than a fully trained predictive model. Conceptually, we are constructing a function:

$$
Score = f(\text{trend}, \text{momentum}, \text{RSI}, \text{volume})
$$

Each component contributes positively or negatively. The sign and magnitude of the final score determine whether the trade bias is bullish, bearish, or neutral.

This approach is simple, interpretable, and appropriate for a first-stage research system.

---

## 7. Debit Spread Economics

For a bull call spread, the terminal payoff can be described as:

$$
\Pi(S_T) = \max(0, S_T - K_1) - \max(0, S_T - K_2) - D
$$

where:
- $S_T$ is price at expiration
- $K_1$ is the long call strike
- $K_2$ is the short call strike
- $D$ is the initial debit paid

The same logic applies analogously to bear put spreads, with the payoff inverted for downside exposure.

The core advantage is that a spread controls cost and limits risk while still allowing directional participation.

---

## 8. Profit Estimation Logic

The profit estimator in the project is not a guarantee. It is a scenario-based projection using:
- expected move
- days to expiration
- current volatility
- structure type
- initial debit estimate

The return on investment estimate is given by:

$$
ROI = \frac{\text{Projected Profit}}{\text{Estimated Cost}} \times 100
$$

This provides a standardized way to compare candidate trades relative to the capital required.

---

## 9. Position Sizing as Risk Control

A mathematically sound trading system must not only estimate upside but also constrain downside. The project therefore recommends smaller allocation percentages for higher-volatility trades.

In practice, position size is treated as a function of both conviction and volatility:

$$
Allocation = g(|Score|, \sigma_{ann})
$$

This is a risk-management layer, not merely a forecasting layer.

---

## 10. Why This Matters Educationally

This project demonstrates an important principle from quantitative finance:

> a trading system is not just a prediction engine. It is a structured decision process combining signal generation, uncertainty measurement, trade structuring, and risk control.

That is why the project is built in layers rather than as a single black-box claim about market prediction.

---

## 11. Execution Mathematics and Paper Routing

A real trading system must map forecast strength into a broker action. If capital is denoted by $C$ and the target allocation percentage by $a$, then a simple notional target is:

$$
N = C \cdot \frac{a}{100}
$$

If the underlying proxy price is $P$, then a first-pass share quantity can be estimated by:

$$
q = \left\lfloor \frac{N}{P} \right\rfloor
$$

This is not the final options execution logic, but it is a practical bridge for paper trading because it allows the research engine, risk engine, and broker engine to be tested together before more complex option-leg routing is introduced.

---

## 12. Approval Gates and Operational Safety

In applied quantitative trading, a signal is not automatically the same thing as a valid order. A practical system includes an approval and validation stage between research output and broker submission.

Conceptually:

$$
\text{Execution Eligibility} = h(\text{signal quality}, \text{risk limits}, \text{liquidity checks}, \text{human approval})
$$

This reduces operational mistakes and helps separate model quality from brokerage and execution risk.

In practical system design, this also means credentials and broker settings should be stored in local runtime configuration rather than inside a reusable template file.

---

## 13. Why Paper Results Differ from Live Results

In professional trading, there is always a reality gap between simulated and live results. The gap arises from factors such as:
- slippage
- spread crossing
- latency
- partial fills
- liquidity changes
- event-driven volatility jumps

A better mental model is:

$$
\text{Live P\&L} = \text{Model Edge} - \text{Costs} - \text{Execution Friction}
$$

This is why the project now emphasizes execution logs and paper-first validation rather than jumping immediately to real-money automation.

---

## 14. Opportunity Discovery Beyond Basic Charts

A broader quant workflow should search for opportunities from several sources of information:
- market microstructure and volume behavior
- event-driven catalysts such as earnings
- cross-sectional relative strength or weakness
- volatility mispricing
- news and sentiment shifts

A useful way to think about it is:

$$
\text{Trade Idea Quality} = f(\text{price action}, \text{volume}, \text{volatility}, \text{events}, \text{fundamental context})
$$

Community discussion and news can be useful for idea discovery, but they should mostly serve as inputs into a screening and validation process rather than as direct instructions to trade.

A broader paper portfolio can also produce more observations for the research loop, provided the positions are diversified and risk-capped. In that sense, more varied paper data can improve the quality of future model calibration.

From a volatility perspective, an event can be thought of as a shock that changes the expected distribution of returns. That is one reason event-driven information can matter so much for both long and short trade selection.

Congressional disclosure reports can be treated similarly: they are not direct forecasts, but they may highlight names worth deeper review when combined with other evidence.

---

## 15. Why This Can Later Become a Website or Mobile App

From a systems-design perspective, the current project already has the beginnings of a layered architecture:
- data ingestion
- model scoring
- trade selection
- execution preview
- dashboard presentation

Once these pieces are separated cleanly, the same backend can serve multiple frontends. A browser dashboard, a web application, and a mobile application can all consume the same analytics and trade state.

In other words, the investment in good architecture today increases optionality later.

---

## Conclusion

A serious options research engine combines statistical reasoning, market structure knowledge, and disciplined risk management. The educational value of this project lies in showing how those parts interact mathematically and operationally in one coherent framework.
