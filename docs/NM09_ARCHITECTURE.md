# NM-09 Strategy Architecture

NM-09 is a strategy and proposal subsystem, not an execution engine. It composes NM-02 market data, NM-04 events, NM-06 forecasts, NM-07 portfolios and NM-08 risk.

```
market data / events / forecasts
            |
      StrategyEngine
   evaluate -> signal -> proposal
                 |          |
          conflict resolver  NM-08 RiskEngine
                 |          |
             audit/store  approved | rejected | restricted
```

The package is `novacontrol.novamarket.strategies`: `models` (records and lifecycle), `indicators` (pure math), `catalog` (six built-ins and config validation), `store` (SQLite persistence) and `engine` (orchestration). `StrategyEngine` never mutates a portfolio, wallet or order book itself. `forward()` hands an approved proposal to the injected NM-10 paper-execution service; NM-10 remains the only owner of orders, fills and settlement.

Signals are observations, proposals are intents, NM-08 decisions are risk opinions, and execution is a separate NM-10 handoff. Keeping those objects distinct is the central invariant.
