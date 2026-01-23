"""
Time Mocking Module - Context manager to override time functions during replay
Completely isolated - uses unittest.mock to patch time functions
"""
import datetime
import time
from unittest.mock import patch
from typing import Optional
import logging


class ReplayTimeContext:
    """
    Context manager that mocks datetime.now() and time.time() to simulate a specific date/time.
    
    Usage:
        with ReplayTimeContext(date="2026-01-22", start_time="04:00:00"):
            # All time functions now return simulated time
            print(datetime.datetime.now())  # Returns 2026-01-22 04:00:00
    """
    
    def __init__(
        self,
        date: str,
        start_time: str = "04:00:00",
        timezone_str: str = "US/Eastern"
    ):
        """
        Args:
            date: Date in YYYY-MM-DD format
            start_time: Start time in HH:MM:SS format
            timezone_str: Timezone string (default: US/Eastern)
        """
        from pytz import timezone
        self.tz = timezone(timezone_str)
        
        # Parse date and time
        try:
            date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
            time_obj = datetime.datetime.strptime(start_time, "%H:%M:%S").time()
            self.base_datetime = self.tz.localize(
                datetime.datetime.combine(date_obj.date(), time_obj)
            )
        except ValueError as e:
            raise ValueError(f"Invalid date/time format: {e}")
        
        # Current simulated time (starts at base)
        self.current_simulated_time = self.base_datetime
        
        # Patches
        self._datetime_patch = None
        self._time_patch = None
        self._nasdaq_info_patches = []
        
        logging.info(f"[TimeMock] Initialized for {date} {start_time} {timezone_str}")
    
    def set_simulated_time(self, dt: datetime.datetime):
        """Update the current simulated time."""
        if dt.tzinfo is None:
            dt = self.tz.localize(dt)
        self.current_simulated_time = dt
    
    def get_simulated_time(self) -> datetime.datetime:
        """Get current simulated time."""
        return self.current_simulated_time
    
    def _mock_datetime_now(self, tz=None):
        """Mock function for datetime.datetime.now()"""
        if tz:
            return self.current_simulated_time.astimezone(tz)
        return self.current_simulated_time
    
    def _mock_time_time(self):
        """Mock function for time.time()"""
        return self.current_simulated_time.timestamp()
    
    def __enter__(self):
        """Enter context - activate time mocking."""
        # Patch datetime.datetime.now
        self._datetime_patch = patch('datetime.datetime')
        mock_datetime = self._datetime_patch.start()
        mock_datetime.now.side_effect = self._mock_datetime_now
        mock_datetime.utcnow.return_value = self.current_simulated_time.astimezone(
            datetime.timezone.utc
        )
        
        # Patch time.time
        self._time_patch = patch('time.time')
        mock_time = self._time_patch.start()
        mock_time.side_effect = self._mock_time_time
        
        # Patch nasdaq_info functions to use simulated time
        # We need to patch the module after it's imported
        import Services.nasdaq_info as nasdaq_info_module
        
        # Create wrapper functions that use simulated time
        original_is_market_open = nasdaq_info_module.is_market_open
        original_is_market_closed = nasdaq_info_module.is_market_closed_or_pre_market
        
        def mock_is_market_open(now=None):
            if now is None:
                now = self.current_simulated_time
            return original_is_market_open(now)
        
        def mock_is_market_closed(now=None):
            if now is None:
                now = self.current_simulated_time
            return original_is_market_closed(now)
        
        # Patch the functions
        nasdaq_info_module.is_market_open = mock_is_market_open
        nasdaq_info_module.is_market_closed_or_pre_market = mock_is_market_closed
        
        self._nasdaq_info_patches = [
            (nasdaq_info_module, 'is_market_open', original_is_market_open),
            (nasdaq_info_module, 'is_market_closed_or_pre_market', original_is_market_closed)
        ]
        
        logging.info(f"[TimeMock] ✅ Time mocking activated - current time: {self.current_simulated_time}")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context - restore original time functions."""
        # Restore nasdaq_info functions
        for module, attr_name, original_func in self._nasdaq_info_patches:
            setattr(module, attr_name, original_func)
        
        # Stop patches
        if self._datetime_patch:
            self._datetime_patch.stop()
        if self._time_patch:
            self._time_patch.stop()
        
        logging.info("[TimeMock] ✅ Time mocking deactivated - restored original functions")
        return False  # Don't suppress exceptions
