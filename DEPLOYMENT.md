# ArcTrigger Deployment Guide

## What to Share

### Required Files
1. **Executable**: `ArcTrigger00_MV1.0.exe` or `ArcTrigger00_MV1.1.exe`
   - The executable is **standalone** - no Python installation needed
   - All dependencies are bundled inside

### Optional Files (Created at Runtime)
- `arctrigger.dat` - Session data (created automatically)
- `conids.db` - Contract ID cache (created automatically)
- `logs/` - Log files (created automatically)

## Prerequisites

### 1. Interactive Brokers TWS (Required)
- **Must have TWS (Trader Workstation) installed and running**
- TWS must be logged in and connected
- Default port: 7497 (paper trading) or 7496 (live trading)
- The application connects to TWS via the IB API

### 2. Optional: Polygon API Key
- Only needed if using Polygon data features
- Set environment variable: `POLYGON_API_KEY=your_key_here`
- Or create a `.env` file in the same folder as the executable:
  ```
  POLYGON_API_KEY=your_key_here
  ```

## Installation Steps

1. **Copy the executable** to any folder on the target machine
2. **Ensure TWS is installed and running** (must be logged in)
3. **Run the executable** - no installation needed!

## First Run

- The application will create:
  - `arctrigger.dat` - Your saved configurations
  - `conids.db` - Contract ID cache
  - `logs/` - Log files for debugging

## Sharing Options

### Option 1: Just the Executable
- Share only `ArcTrigger00_MV1.0.exe` or `ArcTrigger00_MV1.1.exe`
- User needs TWS installed and running
- All other files are created automatically

### Option 2: Executable + Data Files
- Share the executable + `arctrigger.dat` (if you want to include saved configurations)
- Share `conids.db` (optional - speeds up contract lookups)
- User still needs TWS installed and running

### Option 3: Full Package (for support/debugging)
- Executable
- `arctrigger.dat`
- `conids.db`
- `logs/` folder (for troubleshooting)

## System Requirements

- **Windows 10/11** (64-bit)
- **TWS installed and running**
- No Python installation required
- No additional dependencies needed

## Troubleshooting

### "Connection failed" errors
- Ensure TWS is running and logged in
- Check TWS API settings (Enable ActiveX and Socket Clients)
- Verify port number (7497 for paper, 7496 for live)

### Missing data
- Check TWS connection status
- Verify market data subscriptions in TWS
- Check logs folder for error messages

## Version Information

- **MV 1.0**: Previous version with premarket rebase fixes
- **MV 1.1**: New version with premium fetching pipeline (4 retries, 500ms delay)

Version number is displayed in the UI banner.
