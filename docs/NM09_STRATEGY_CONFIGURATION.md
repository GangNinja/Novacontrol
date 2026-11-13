# NM-09 Strategy Configuration

A strategy is created from a catalog id with a name, config, horizons, asset classes and priority. Creation always starts in `draft`; activation is explicit.

Configuration changes produce a new monotonic version (`nm09-<catalog>-<n>`) and a retained configuration snapshot. Validation rejects unknown parameters, out-of-range values, unsupported horizons, empty asset classes and priorities outside 0..100.

Lifecycle is `draft -> active <-> paused -> suspended -> retired`, with `degraded` as a recorded operational state. Illegal transitions raise `StrategyStateError`. Only `active` strategies may evaluate or propose. Retired and suspended strategies are read-only. The store persists the strategy, every version and a lifecycle audit row with actor and reason.
