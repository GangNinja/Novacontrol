# NM-09 Signal Engine

Signals are `BUY`, `SELL`, `HOLD`, `EXIT` and `NO_SIGNAL`. `BUY` opens or adds; `SELL` is a partial reduction of a holding; `EXIT` closes a holding. Short selling is unsupported, so a `SELL` without a holding becomes `NO_SIGNAL`.

A signal records strategy and version, portfolio, instrument, reference price, data cutoff, horizon, indicators, conditions, forecast/event references, confidence, explanation, expiry and data quality. Its id is a digest of those inputs, so evaluating the same strategy/version/portfolio/instrument/cutoff replays the stored signal instead of duplicating it.

Data gates run first: missing or unusable quotes, stale quotes (over 24 hours), too few bars or missing required inputs produce a non-actionable signal. Signals are append-only and exposed through `GET /market/strategies/signals` and strategy-scoped reads.
