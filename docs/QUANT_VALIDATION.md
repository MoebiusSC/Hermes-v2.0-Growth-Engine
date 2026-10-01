# Validation Engine V1

The automatic optimizer proposes exactly one bounded alpha change before examining the current historical windows. Promotion requires every deterministic gate to pass. This service remains paper only.

| Gate | Rule |
| --- | --- |
| Data and timing | Complete synchronized OHLC on the UTC 15m/1h grids; future hourly perturbations must not change past signals; next-open fills precede processing any asset's closing price |
| Chronological holdouts | Four complete 30-calendar-day windows; at least 30 trades and three traded assets; at least 75% positive windows; worst fold no worse than the fixed weekly loss budget; median improvement and comparable drawdown |
| Cost stress | Fees, spread and slippage on both sides, including terminal liquidation; positive compounded daily returns under 2x and 3x costs |
| DSR | Probability >= 0.95; daily unannualised Sharpe for all inputs; actual trial dispersion with null-error floor; non-normal moments; conservative Bartlett autocorrelation adjustment |
| PBO / CSCV | Probability <= 0.20 across 70 splits of eight chronological blocks of one aligned daily-return matrix |
| Paired block bootstrap | 500 fixed-seed circular seven-day samples; 5th percentile mean daily advantage > 0; 95th percentile drawdown within the existing portfolio limit |
| Recent confirmation | Most recent window: >=5 trades, positive net and 2x-cost return, >=0.25 percentage-point advantage |
| Application | Same configuration as the research job; all gate evidence present; no positions, pending signals or latched halt |
| Prospective paper | Existing rollback guard after >=14 days and >=10 closed trades, while flat; observed paper is never live approval |

The reference cohort is the current configuration, the preregistered candidate and one nearby control (preferably the opposite parameter change). All use the same assets, capital, risk, costs, timestamps and fold boundaries. The optimizer never selects the best cohort member. PBO diagnoses this **local family only**, not every possible strategy or all historical tuning. Lifetime trials also cover prior cycles and the two stress scenarios. Reservations occur before work starts and survive crashes; failed downloads may conservatively overcount trials. Display history truncation does not reset the counter. Older history is explicitly marked as a lower bound when complete past search records are unavailable.

Fold replays warm indicators using only past prices, start flat and settle all positions with exit costs at the terminal observed close. No training labels or positions cross a fold boundary. These are fixed-alpha rolling historical holdouts, not train/test refitting or a claim of untouched data. Repeated cycles reuse historical observations; prospective paper observations remain necessary. A future model fitted on overlapping labels must implement its own train-only fit and purging/embargo before using this gate.

The DSR dispersion floor and effective-sample adjustment are conservative extensions of the published formula, not an exact serial-dependence theorem. The bootstrap preserves local dependence and cannot model unseen liquidity shocks. Crypto spot costs remain the owner's existing fixed estimates; exchange-specific market impact, funding and borrow are not inferred or fabricated. The only automatic portfolio is spot crypto; manual equities/ETFs are outside this validation scope.

The dashboard distinguishes legacy paper configurations, rejected candidates, historically validated candidates and paper observation. The new candidate evidence covers the **shared portfolio**, including the existing SUI replica; it does not independently approve SUI. The API exposes the data SHA-256, configuration cohort, calendar interval, gate statistics and cumulative trials. Unknown values remain unknown.

Reproducibility: `uv sync --frozen`, `uv run python -m unittest discover -s tests -p 'test_*.py' -v`, `node --check hermes_trading/growth_dashboard.js`. The branch benchmark workflow downloads public candles and saves the full rejected/passed assessment. It never writes production state or sends broker orders.

References: Bailey & Lopez de Prado, The Deflated Sharpe Ratio (2014), https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf; Bailey et al., The Probability of Backtest Overfitting, https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf.
