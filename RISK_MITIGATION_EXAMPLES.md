# Risk Mitigation - Code Examples

## Using Validation Helpers

### Before (Risky):
```python
# view.py - place_order()
strike_str = self.combo_strike.get().strip()
if not strike_str:
    raise ValueError("Please select a strike price")

strike = float(strike_str)  # ⚠️ Can crash if invalid format
```

### After (Safe):
```python
# view.py - place_order()
from Helpers.validation import validate_strike, validate_trigger_price, validate_quantity

strike_str = self.combo_strike.get().strip()
is_valid, strike, error_msg = validate_strike(strike_str)

if not is_valid:
    self._set_status(f"Error: {error_msg}", "red")
    logging.error(f"Invalid strike: {strike_str} - {error_msg}")
    return

# Now strike is guaranteed to be a valid float > 0
```

---

## Adding Health Checks

### In main.py:
```python
from Helpers.health_check import health_checker, register_critical_threads

class ArcTriggerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        # ... existing code ...
        
        # Register critical threads
        register_critical_threads()
        
        # Start health monitoring
        health_checker.start_monitoring(interval=60)
        
        # Add health check button (optional)
        ttk.Button(
            top_frame, 
            text="Health Check", 
            command=self.show_health_status
        ).pack(side="left", padx=5)
    
    def show_health_status(self):
        """Display current health status in a popup."""
        from Helpers.health_check import get_health_status
        
        status = get_health_status()
        
        win = tk.Toplevel(self)
        win.title("System Health")
        win.geometry("400x300")
        
        if status["healthy"]:
            ttk.Label(win, text="✅ System Healthy", font=("Arial", 12, "bold")).pack(pady=10)
        else:
            ttk.Label(win, text="⚠️ Issues Found", font=("Arial", 12, "bold"), foreground="red").pack(pady=10)
            for issue in status["issues"]:
                ttk.Label(win, text=f"• {issue}", foreground="red").pack(anchor="w", padx=20)
        
        # Show thread stats
        stats = status["thread_stats"]
        ttk.Label(win, text=f"\nThreads: {stats['total']} total ({stats['daemon']} daemon)").pack()
```

---

## Defensive Programming Examples

### 1. Safe Dictionary Access
```python
# Before:
order = self.pending_orders[order_id]  # ⚠️ KeyError if missing

# After:
order = self.pending_orders.get(order_id)
if not order:
    logging.warning(f"Order {order_id} not found in pending_orders")
    return
```

### 2. Safe Thread Operations
```python
# Before:
thread.start()  # ⚠️ Can fail if thread already started

# After:
if not thread.is_alive():
    try:
        thread.start()
    except RuntimeError as e:
        logging.error(f"Failed to start thread: {e}")
else:
    logging.warning(f"Thread {thread.name} already running")
```

### 3. Safe Service Access
```python
# Before:
service = create_tws_service()
service.place_order(...)  # ⚠️ Can fail if not connected

# After:
service = create_tws_service()
if not service.is_connected():
    logging.error("TWS not connected - cannot place order")
    self._set_status("Error: Not connected to TWS", "red")
    return

try:
    service.place_order(...)
except Exception as e:
    logging.error(f"Order placement failed: {e}", exc_info=True)
    self._set_status(f"Order failed: {e}", "red")
```

---

## Error Recovery Patterns

### 1. Retry with Backoff
```python
def place_order_with_retry(order, max_attempts=3):
    """Place order with automatic retry on failure."""
    for attempt in range(1, max_attempts + 1):
        try:
            if general_app.place_custom_order(order):
                logging.info(f"Order placed successfully (attempt {attempt})")
                return True
        except Exception as e:
            logging.warning(f"Order placement attempt {attempt} failed: {e}")
            if attempt < max_attempts:
                wait_time = 2 ** attempt  # Exponential backoff: 2s, 4s, 8s
                time.sleep(wait_time)
            else:
                logging.error(f"Order placement failed after {max_attempts} attempts")
                return False
    return False
```

### 2. Graceful Degradation
```python
def get_option_premium_safe(symbol, expiry, strike, right):
    """Get option premium with fallback."""
    try:
        # Try primary method
        premium = tws_service.get_option_premium(symbol, expiry, strike, right)
        if premium:
            return premium
    except Exception as e:
        logging.warning(f"Primary premium fetch failed: {e}")
    
    # Fallback: return None and let caller handle
    logging.warning("Premium unavailable - order may need manual pricing")
    return None
```

### 3. State Validation
```python
def validate_order_state(order: Order) -> bool:
    """Validate order state consistency."""
    issues = []
    
    if order.state == OrderState.PENDING and order._ib_order_id:
        issues.append(f"Order {order.order_id} has IB ID but is PENDING")
    
    if order.state == OrderState.ACTIVE and not order._ib_order_id:
        issues.append(f"Order {order.order_id} is ACTIVE but has no IB ID")
    
    if order.state == OrderState.FINALIZED and not order.result:
        issues.append(f"Order {order.order_id} is FINALIZED but has no result")
    
    if issues:
        for issue in issues:
            logging.warning(f"[StateValidation] {issue}")
        return False
    
    return True
```

---

## Threading Safety Examples

### 1. Consistent Lock Ordering
```python
# Define lock order globally (in module)
_LOCK_ORDER = ["trigger_lock", "lock", "_arclock", "_stream_lock"]

def acquire_locks_in_order(service, lock_names):
    """Acquire locks in consistent order to prevent deadlocks."""
    locks = []
    for name in sorted(lock_names):  # Sort ensures consistent order
        lock = getattr(service, name, None)
        if lock:
            lock.acquire()
            locks.append(lock)
    return locks

def release_locks(locks):
    """Release locks in reverse order."""
    for lock in reversed(locks):
        try:
            lock.release()
        except Exception as e:
            logging.error(f"Error releasing lock: {e}")
```

### 2. Timeout on Lock Acquisition
```python
import threading

def acquire_lock_with_timeout(lock, timeout=5.0):
    """Acquire lock with timeout to prevent indefinite blocking."""
    acquired = lock.acquire(timeout=timeout)
    if not acquired:
        logging.error(f"Failed to acquire lock within {timeout}s - possible deadlock")
        return False
    return True

# Usage:
if acquire_lock_with_timeout(self.lock, timeout=5.0):
    try:
        # Do work
        pass
    finally:
        self.lock.release()
else:
    logging.error("Cannot proceed - lock acquisition failed")
```

---

## Testing Examples

### 1. Simple Unit Test
```python
# test_validation.py
import unittest
from Helpers.validation import validate_strike, validate_trigger_price

class TestValidation(unittest.TestCase):
    def test_validate_strike_valid(self):
        is_valid, strike, msg = validate_strike("245.0")
        self.assertTrue(is_valid)
        self.assertEqual(strike, 245.0)
    
    def test_validate_strike_invalid(self):
        is_valid, strike, msg = validate_strike("invalid")
        self.assertFalse(is_valid)
        self.assertIsNone(strike)
        self.assertIn("Invalid", msg)
    
    def test_validate_strike_negative(self):
        is_valid, strike, msg = validate_strike("-10")
        self.assertFalse(is_valid)
        self.assertIn("positive", msg)

if __name__ == "__main__":
    unittest.main()
```

### 2. Integration Test Pattern
```python
# test_order_flow.py
def test_order_placement_flow():
    """Test complete order placement flow."""
    # Setup
    app = create_test_app()
    symbol = "AMZN"
    strike = 245.0
    
    # Test
    order = app.place_order(
        symbol=symbol,
        strike=strike,
        quantity=1,
        trigger_price=244.0
    )
    
    # Verify
    assert order is not None
    assert order.symbol == symbol
    assert order.strike == strike
    assert order.state == OrderState.PENDING
    
    # Cleanup
    app.cleanup()
```

---

## Logging Best Practices

### 1. Structured Logging
```python
# Use consistent log format
logging.info(
    f"[OrderWaitService] Trigger hit | "
    f"order_id={order.order_id} | "
    f"symbol={order.symbol} | "
    f"trigger={order.trigger} | "
    f"current_price={current_price}"
)
```

### 2. Error Context
```python
# Always include context in errors
try:
    result = risky_operation()
except Exception as e:
    logging.error(
        f"[ServiceName] Operation failed | "
        f"order_id={order_id} | "
        f"symbol={symbol} | "
        f"error={e}",
        exc_info=True  # Include stack trace
    )
```

---

## Quick Integration Checklist

- [ ] Import validation helpers in view.py
- [ ] Replace float() calls with validate_strike()
- [ ] Add health check to main.py
- [ ] Add None checks before service calls
- [ ] Wrap risky operations in try/except
- [ ] Add logging to error paths
- [ ] Test in paper trading
- [ ] Review logs after changes

---

**Next Steps:**
1. Start with validation helpers (low risk, high value)
2. Add health checks gradually
3. Improve error handling one service at a time
4. Test each change before moving to next
