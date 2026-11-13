# NM-11 Historical Data

The service loads bars from the NM-02 `MarketDataEngine.bars()` surface,
paginating in 1,000-bar pages up to a 100,000-bar per-instrument cap. Each row
becomes a `HistoricalBar` with instrument, observation time, OHLC, volume,
source and availability time. Partial and `invalid`/`suspect` rows are skipped
by the loader; the engine itself refuses a partial, non-positive or internally
inconsistent bar.

For completed NM-02 bars, `available_at` is the bar's `observed_at`: a daily
bar becomes usable at its own timestamp. The assumption is explicit and is the
point-in-time convention the replay enforces.

`normalise_bars`:

1. filters to the requested inclusive period;
2. sorts by `(observed_at, instrument_id)`;
3. removes duplicate instrument/timestamp observations and records a warning;
4. leaves missing sessions missing rather than fabricating a zero return.

The completed run persists its bars, a SHA-256 bar-data hash, dataset id and
version, sources, time zones, removed duplicates, unavailable bars, halted
bars, exchange-closed bars and survivorship/corporate-action notes. `verify()`
replays the persisted bars, so verification does not depend on the provider
still being reachable.

NM-02's dated calendar is enforced for a known MIC: a bar whose local date is a
weekend or a published holiday is excluded and counted in
`data_quality.closed_bars`; it cannot fill an order. An unknown MIC has no
calendar and is reported as unverified rather than assumed open.

The loading path is tested against the real NM-02 `MarketDataStore` with
registered instruments and appended bars, not only a fake adapter.

## Limitations

- Only daily (`1d`) bars are supported.
- Calendar coverage is dated (2026 for XNSE/XBOM/US venues); outside a covered
  year the weekday/session facts apply but the holiday coverage is reported as
  unverified.
- Time-zone normalisation relies on the bar's own aware timestamp.
- Corporate actions require caller-adjusted bars; no adjustment is applied.
- Historical universe membership and delisted instruments are not
  reconstructed, so results carry survivorship-bias risk.
- No live provider history was fetched in the test environment; the real
  NM-02 store path is exercised with ingested fixture bars.
