# Historical Data Replay System

Completely isolated replay system for testing premarket-to-RTH transitions and order placement logic.

## Features

- ✅ Fetch historical market data from Polygon API (1-minute aggregates)
- ✅ Replay at real-time or accelerated speeds
- ✅ Mock time functions (datetime.now(), time.time(), market hours checks)
- ✅ Inject historical ticks via existing callback system
- ✅ Optional TWS order simulation
- ✅ Zero modifications to existing application code

## Usage

### Basic Replay

```bash
# Replay TSLA on Jan 22, 2026 at real-time speed
python replay_test.py --date 2026-01-22 --symbol TSLA
```

### Accelerated Replay

```bash
# Replay at 2x speed
python replay_test.py --date 2026-01-22 --symbol TSLA --speed 2.0
```

### Multiple Symbols

```bash
# Replay multiple symbols
python replay_test.py --date 2026-01-22 --symbol TSLA AAPL --speed 1.0
```

### Custom Time Range

```bash
# Replay only premarket hours (4 AM - 9:30 AM)
python replay_test.py --date 2026-01-22 --symbol TSLA --start-time 04:00:00 --end-time 09:30:00
```

### Order Simulation

```bash
# Use simulated orders (no real TWS)
python replay_test.py --date 2026-01-22 --symbol TSLA --order-mode simulate
```

## Architecture

### Files

- `historical_data_service.py` - Fetches and caches Polygon aggregates
- `time_mock.py` - Context manager for time function patching
- `replay_service.py` - Core replay engine
- `tws_mock_service.py` - Optional TWS order simulation
- `replay_test.py` - Standalone CLI tool

### How It Works

1. **Data Fetching**: Fetches 1-minute aggregates from Polygon API
2. **Time Mocking**: Patches `datetime.now()`, `time.time()`, and market hours functions
3. **Tick Injection**: Injects historical ticks via `callback_manager.trigger()`
4. **Order Testing**: Tests order placement logic with simulated or real TWS

### Safety

- ✅ **Zero modifications** to existing code
- ✅ **Opt-in only** - only runs when you explicitly run `replay_test.py`
- ✅ **Context manager cleanup** - time patches automatically restore
- ✅ **Isolated directory** - all replay code in `Replay/` folder

## Cache

Historical data is cached in `replay_cache/` directory to avoid repeated API calls.

## Requirements

- Polygon API key (set in `.env` as `POLYGON_API_KEY`)
- Python 3.7+
- `pytz` for timezone handling
- `requests` for API calls

## Testing Scenarios

1. **Premarket Trigger Hit** - Test rebase popup logic
2. **RTH Transition** - Test order finalization at market open
3. **Order Placement Timing** - Verify orders place at correct times
4. **Price Updates** - Verify all watchers receive correct prices
5. **Market Hours Detection** - Verify premarket vs RTH detection
