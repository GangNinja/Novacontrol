# Performance Optimization

The Phase 14 performance subsystem provides lightweight primitives for metrics, profiling, caching, and load testing.

## Components

- `MetricsRegistry`: counters, gauges, timings, and snapshots
- `MetricSample`: individual metric sample
- `TtlCache`: in-memory cache with optional expiration
- `CacheEntry`: cached value and expiration metadata
- `Profiler`: sync and async operation timing
- `ProfileResult`: measured result and duration
- `LoadTester`: async bounded-concurrency load-test helper
- `LoadTestResult`: request count, successes, failures, duration, and requests per second

## CLI

```powershell
python -m novacontrol demo phase14
```

## Future Work

These primitives can feed the GUI performance tab, API diagnostics, and external telemetry exporters.
