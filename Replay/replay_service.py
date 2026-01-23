"""
Replay Service - Manages replay state and injects historical ticks
Completely isolated - doesn't modify existing services
"""
import threading
import time
import logging
from typing import List, Dict, Optional, Callable
from datetime import datetime, timedelta
from Replay.historical_data_service import HistoricalDataService
from Replay.time_mock import ReplayTimeContext
from Services.callback_manager import callback_manager


class ReplayService:
    """
    Manages replay of historical market data.
    Injects ticks at correct timestamps using the existing callback system.
    """
    
    def __init__(self, hist_service: Optional[HistoricalDataService] = None):
        self.hist_service = hist_service or HistoricalDataService()
        self.time_context: Optional[ReplayTimeContext] = None
        self.replay_thread: Optional[threading.Thread] = None
        self.is_replaying = False
        self.is_paused = False
        self.speed_multiplier = 1.0  # 1.0 = real-time, 2.0 = 2x speed, etc.
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Start unpaused
        
    def start_replay(
        self,
        date: str,
        symbols: List[str],
        start_time: str = "04:00:00",
        end_time: str = "20:00:00",
        speed: float = 1.0,
        order_mode: str = "simulate"
    ):
        """
        Start replaying historical data for given symbols on a specific date.
        
        Args:
            date: Date in YYYY-MM-DD format
            symbols: List of symbols to replay
            start_time: Start time in HH:MM:SS format (ET)
            end_time: End time in HH:MM:SS format (ET)
            speed: Speed multiplier (1.0 = real-time)
            order_mode: "simulate" or "real" (for TWS orders)
        """
        if self.is_replaying:
            logging.warning("[ReplayService] Replay already in progress. Stop first.")
            return
        
        self.speed_multiplier = speed
        self.is_replaying = True
        self.is_paused = False
        self._stop_event.clear()
        self._pause_event.set()
        
        # Initialize time context
        self.time_context = ReplayTimeContext(date=date, start_time=start_time)
        self.time_context.__enter__()
        
        logging.info(f"[ReplayService] 🎬 Starting replay for {symbols} on {date}")
        logging.info(f"[ReplayService] Speed: {speed}x, Order mode: {order_mode}")
        
        # Start replay thread
        self.replay_thread = threading.Thread(
            target=self._replay_worker,
            args=(date, symbols, start_time, end_time),
            daemon=True
        )
        self.replay_thread.start()
    
    def pause_replay(self):
        """Pause the replay."""
        if self.is_replaying and not self.is_paused:
            self.is_paused = True
            self._pause_event.clear()
            logging.info("[ReplayService] ⏸️ Replay paused")
    
    def resume_replay(self):
        """Resume the replay."""
        if self.is_replaying and self.is_paused:
            self.is_paused = False
            self._pause_event.set()
            logging.info("[ReplayService] ▶️ Replay resumed")
    
    def stop_replay(self):
        """Stop the replay and restore time functions."""
        if not self.is_replaying:
            return
        
        logging.info("[ReplayService] 🛑 Stopping replay...")
        self.is_replaying = False
        self._stop_event.set()
        self._pause_event.set()  # Unpause to allow thread to exit
        
        if self.replay_thread and self.replay_thread.is_alive():
            self.replay_thread.join(timeout=5)
        
        # Restore time context
        if self.time_context:
            self.time_context.__exit__(None, None, None)
            self.time_context = None
        
        logging.info("[ReplayService] ✅ Replay stopped")
    
    def _replay_worker(
        self,
        date: str,
        symbols: List[str],
        start_time: str,
        end_time: str
    ):
        """Worker thread that replays historical data."""
        try:
            # Fetch historical data for all symbols
            all_ticks = {}  # symbol -> list of ticks
            
            for symbol in symbols:
                logging.info(f"[ReplayService] Fetching data for {symbol}...")
                bars = self.hist_service.fetch_historical_aggregates(
                    symbol, date, start_time, end_time
                )
                
                if not bars:
                    logging.warning(f"[ReplayService] No data for {symbol}, skipping")
                    continue
                
                # Convert bars to ticks (using close price)
                ticks = self.hist_service.get_tick_data_from_aggregates(bars)
                all_ticks[symbol] = ticks
                logging.info(f"[ReplayService] Loaded {len(ticks)} ticks for {symbol}")
            
            if not all_ticks:
                logging.error("[ReplayService] No data available for any symbol")
                return
            
            # Merge and sort all ticks by timestamp
            merged_ticks = []
            for symbol, ticks in all_ticks.items():
                for tick in ticks:
                    merged_ticks.append({
                        "symbol": symbol,
                        "timestamp_ms": tick["timestamp_ms"],
                        "price": tick["price"]
                    })
            
            # Sort by timestamp
            merged_ticks.sort(key=lambda x: x["timestamp_ms"])
            
            logging.info(f"[ReplayService] Total ticks to replay: {len(merged_ticks)}")
            
            # Replay ticks
            last_timestamp_ms = None
            
            for i, tick in enumerate(merged_ticks):
                # Check for stop
                if self._stop_event.is_set():
                    break
                
                # Wait if paused
                self._pause_event.wait()
                
                # Calculate delay until this tick
                current_timestamp_ms = tick["timestamp_ms"]
                
                if last_timestamp_ms is not None:
                    # Calculate real delay
                    delay_ms = current_timestamp_ms - last_timestamp_ms
                    delay_seconds = (delay_ms / 1000.0) / self.speed_multiplier
                    
                    # Sleep for the delay
                    if delay_seconds > 0:
                        time.sleep(delay_seconds)
                
                # Update simulated time
                tick_datetime = datetime.fromtimestamp(current_timestamp_ms / 1000.0)
                if self.time_context:
                    self.time_context.set_simulated_time(tick_datetime)
                
                # Inject tick via callback manager
                symbol = tick["symbol"]
                price = tick["price"]
                
                callback_manager.trigger(symbol, price)
                
                # Log progress every 100 ticks
                if (i + 1) % 100 == 0:
                    current_time = self.time_context.get_simulated_time() if self.time_context else None
                    logging.info(
                        f"[ReplayService] Progress: {i+1}/{len(merged_ticks)} ticks "
                        f"| Time: {current_time} | {symbol}: ${price:.2f}"
                    )
                
                last_timestamp_ms = current_timestamp_ms
            
            logging.info("[ReplayService] ✅ Replay completed")
            
        except Exception as e:
            logging.error(f"[ReplayService] ❌ Replay error: {e}", exc_info=True)
        finally:
            self.is_replaying = False
            if self.time_context:
                self.time_context.__exit__(None, None, None)
    
    def get_replay_status(self) -> Dict:
        """Get current replay status."""
        return {
            "is_replaying": self.is_replaying,
            "is_paused": self.is_paused,
            "speed": self.speed_multiplier,
            "current_time": self.time_context.get_simulated_time() if self.time_context else None
        }
