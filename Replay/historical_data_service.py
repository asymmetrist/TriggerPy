"""
Historical Data Service - Fetches historical market data from Polygon API
Completely isolated - doesn't modify existing services
"""
import requests
import logging
import os
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pathlib import Path
import sys

# Try to load dotenv if available
try:
    from dotenv import load_dotenv
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        env_path = os.path.join(exe_dir, '.env')
    else:
        env_path = '.env'
    load_dotenv(env_path, override=False, interpolate=False)
except ImportError:
    pass
except Exception as e:
    logging.warning(f"[HistoricalData] Failed to load .env: {e}")


class HistoricalDataService:
    """
    Fetches historical market data from Polygon API.
    Uses 1-minute aggregates (works on all Polygon plans).
    """
    
    def __init__(self, cache_dir: str = "replay_cache"):
        self.api_key = os.getenv("POLYGON_API_KEY")
        if not self.api_key:
            raise ValueError("POLYGON_API_KEY environment variable not set!")
        
        self.base_url = "https://api.polygon.io"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        
    def _get_cache_path(self, symbol: str, date: str) -> Path:
        """Get cache file path for symbol/date combination."""
        return self.cache_dir / f"{symbol}_{date}.json"
    
    def _load_from_cache(self, symbol: str, date: str) -> Optional[List[Dict]]:
        """Load historical data from cache if available."""
        cache_path = self._get_cache_path(symbol, date)
        if cache_path.exists():
            try:
                with open(cache_path, 'r') as f:
                    data = json.load(f)
                    logging.info(f"[HistoricalData] Loaded {len(data)} bars from cache for {symbol} on {date}")
                    return data
            except Exception as e:
                logging.warning(f"[HistoricalData] Cache load failed: {e}")
        return None
    
    def _save_to_cache(self, symbol: str, date: str, data: List[Dict]):
        """Save historical data to cache."""
        cache_path = self._get_cache_path(symbol, date)
        try:
            with open(cache_path, 'w') as f:
                json.dump(data, f, indent=2)
            logging.info(f"[HistoricalData] Cached {len(data)} bars for {symbol} on {date}")
        except Exception as e:
            logging.warning(f"[HistoricalData] Cache save failed: {e}")
    
    def fetch_historical_aggregates(
        self,
        symbol: str,
        date: str,
        start_time: str = "04:00:00",
        end_time: str = "20:00:00",
        use_cache: bool = True
    ) -> List[Dict]:
        """
        Fetch 1-minute aggregates for a symbol on a specific date.
        
        Args:
            symbol: Stock symbol (e.g., "TSLA")
            date: Date in YYYY-MM-DD format
            start_time: Start time in HH:MM:SS format (ET)
            end_time: End time in HH:MM:SS format (ET)
            use_cache: Whether to use cached data if available
            
        Returns:
            List of bar dictionaries with keys: timestamp_ms, open, high, low, close, volume
        """
        # Check cache first
        if use_cache:
            cached = self._load_from_cache(symbol, date)
            if cached:
                return cached
        
        # Parse date and times
        try:
            date_obj = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Invalid date format: {date}. Use YYYY-MM-DD")
        
        # Convert ET times to UTC timestamps (Polygon uses UTC)
        # For simplicity, assume ET = UTC-5 (EST) or UTC-4 (EDT)
        # We'll use a simple approximation
        from pytz import timezone
        eastern = timezone('US/Eastern')
        
        # Create datetime objects in Eastern time
        start_dt = eastern.localize(
            datetime.combine(date_obj.date(), datetime.strptime(start_time, "%H:%M:%S").time())
        )
        end_dt = eastern.localize(
            datetime.combine(date_obj.date(), datetime.strptime(end_time, "%H:%M:%S").time())
        )
        
        # Convert to UTC and then to milliseconds
        start_utc = start_dt.astimezone(timezone('UTC'))
        end_utc = end_dt.astimezone(timezone('UTC'))
        
        start_ms = int(start_utc.timestamp() * 1000)
        end_ms = int(end_utc.timestamp() * 1000)
        
        # Fetch from Polygon API
        url = f"{self.base_url}/v2/aggs/ticker/{symbol.upper()}/range/1/minute/{start_ms}/{end_ms}"
        params = {
            "apiKey": self.api_key,
            "sort": "asc",
            "adjusted": "true",
            "limit": 50000  # Max limit
        }
        
        logging.info(f"[HistoricalData] Fetching aggregates for {symbol} on {date}...")
        
        all_bars = []
        next_url = url
        
        # Handle pagination
        while next_url:
            try:
                resp = requests.get(next_url, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                
                results = data.get("results", [])
                if not results:
                    logging.warning(f"[HistoricalData] No data returned for {symbol} on {date}")
                    break
                
                # Convert to our format
                for bar in results:
                    all_bars.append({
                        "timestamp_ms": bar.get("t"),  # Unix timestamp in milliseconds
                        "open": bar.get("o"),
                        "high": bar.get("h"),
                        "low": bar.get("l"),
                        "close": bar.get("c"),
                        "volume": bar.get("v")
                    })
                
                # Check for next page
                next_url = data.get("next_url")
                if next_url:
                    next_url = f"{next_url}&apiKey={self.api_key}"
                    params = {}  # Clear params for subsequent requests
                
                logging.info(f"[HistoricalData] Fetched {len(results)} bars (total: {len(all_bars)})")
                
            except requests.exceptions.RequestException as e:
                logging.error(f"[HistoricalData] API request failed: {e}")
                break
            except Exception as e:
                logging.error(f"[HistoricalData] Error processing response: {e}")
                break
        
        if all_bars:
            # Save to cache
            self._save_to_cache(symbol, date, all_bars)
            logging.info(f"[HistoricalData] ✅ Fetched {len(all_bars)} bars for {symbol} on {date}")
        else:
            logging.warning(f"[HistoricalData] ⚠️ No data available for {symbol} on {date}")
        
        return all_bars
    
    def get_tick_data_from_aggregates(self, bars: List[Dict]) -> List[Dict]:
        """
        Convert 1-minute aggregates to simulated tick data.
        Uses close price as tick price (can be enhanced later).
        
        Args:
            bars: List of bar dictionaries
            
        Returns:
            List of tick dictionaries with: timestamp_ms, price
        """
        ticks = []
        for bar in bars:
            # Use close price as the tick price
            # In a real implementation, you might interpolate or use high/low
            ticks.append({
                "timestamp_ms": bar["timestamp_ms"],
                "price": bar["close"]
            })
        return ticks
