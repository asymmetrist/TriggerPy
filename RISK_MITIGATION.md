# Risk Mitigation Guide for ArcTrigger

## 🎯 Core Principles

1. **Test in Paper Trading First** - Never deploy untested changes to live trading
2. **Make Small, Incremental Changes** - One change at a time, test, then proceed
3. **Monitor Everything** - Watch logs, thread health, order states
4. **Have Rollback Plan** - Keep previous working version ready

---

## 📋 Pre-Change Checklist

Before making ANY code change:

- [ ] **Backup current working version** (git commit or copy)
- [ ] **Document what you're changing and why**
- [ ] **Identify all affected services/components**
- [ ] **Plan testing strategy** (what to test, how to verify)
- [ ] **Check for threading implications** (locks, shared state, race conditions)
- [ ] **Review error handling** (will exceptions be caught?)

---

## 🧪 Testing Strategy

### 1. **Unit Testing** (Add Basic Tests)

Create test files for critical paths:

```python
# test_order_validation.py
def test_strike_validation():
    # Test that invalid strikes are filtered
    pass

def test_order_state_transitions():
    # Test order state machine
    pass
```

### 2. **Integration Testing** (Paper Trading)

- Always test in paper trading account first
- Test each scenario:
  - ✅ Order placement (LMT and MKT)
  - ✅ Trigger conditions (premarket and RTH)
  - ✅ Stop loss activation
  - ✅ Connection drops and recovery
  - ✅ Multiple concurrent orders

### 3. **Threading Stress Tests**

```python
# Test concurrent order operations
# Test lock contention
# Test thread cleanup on shutdown
```

---

## 🔒 Threading Safety Guidelines

### Critical Rules:

1. **Lock Ordering** - Always acquire locks in the same order to prevent deadlocks
   ```python
   # BAD - Can deadlock:
   with lock_a:
       with lock_b:
           ...
   
   # Elsewhere:
   with lock_b:
       with lock_a:  # DEADLOCK RISK!
           ...
   
   # GOOD - Consistent order:
   # Always: lock_a, then lock_b
   ```

2. **Lock Scope** - Keep lock scope minimal
   ```python
   # BAD:
   with self.lock:
       data = expensive_operation()  # Blocks other threads
       process(data)
   
   # GOOD:
   data = expensive_operation()  # Outside lock
   with self.lock:
       process(data)  # Minimal lock time
   ```

3. **Shared State** - Always protect shared state
   ```python
   # BAD:
   self.pending_orders[order_id] = order  # Race condition!
   
   # GOOD:
   with self.lock:
       self.pending_orders[order_id] = order
   ```

### Current Risk Areas:

- **order_wait_service.py** has 4 locks - review for deadlock potential
- **ORDER_LOCK** in tws_service.py - ensure it's not blocking too long
- Multiple daemon threads - ensure proper cleanup

---

## 🛡️ Error Handling Best Practices

### 1. **Never Let Exceptions Escape Threads**

```python
# BAD:
def worker():
    risky_operation()  # Exception kills thread silently

# GOOD:
def worker():
    try:
        risky_operation()
    except Exception as e:
        logging.error(f"Worker error: {e}", exc_info=True)
        # Handle gracefully - don't crash silently
```

### 2. **Validate Inputs Early**

```python
# BAD:
strike = float(strike_str)  # Can crash if invalid

# GOOD:
try:
    strike = float(strike_str)
    if strike <= 0:
        raise ValueError("Strike must be positive")
except (ValueError, TypeError) as e:
    logging.error(f"Invalid strike: {strike_str}")
    return None
```

### 3. **Defensive Programming**

```python
# Always check for None/empty before use
if not self.model:
    logging.warning("Model not initialized")
    return

if not order or order.state == OrderState.CANCELLED:
    return
```

---

## 📊 Monitoring & Observability

### 1. **Add Health Checks**

```python
# Add to runtime_manager.py or create health_check.py
def check_thread_health():
    """Verify all critical threads are alive"""
    threads = threading.enumerate()
    critical_threads = ['OrderWaitService', 'OrderFixerService', ...]
    for name in critical_threads:
        if not any(t.name == name for t in threads):
            logging.error(f"Critical thread {name} is dead!")
```

### 2. **Add Metrics**

```python
# Track order state transitions
# Track thread pool queue depth
# Track lock contention times
# Track API call latencies
```

### 3. **Watch Logs For:**

- Thread errors or deadlocks
- Order state inconsistencies
- Connection drops
- Exception patterns
- Performance degradation

---

## 🔄 Change Management Process

### Step-by-Step:

1. **Create Feature Branch**
   ```bash
   git checkout -b feature/my-change
   ```

2. **Make Small Change** (one service/function at a time)

3. **Test Immediately**
   - Run in paper trading
   - Check logs for errors
   - Verify expected behavior

4. **Commit with Clear Message**
   ```bash
   git commit -m "Add strike validation - prevents invalid order placement"
   ```

5. **Document in CHANGELOG.md**

6. **Merge Only After Verification**

---

## 🚨 High-Risk Change Categories

### ⚠️ EXTRA CAUTION REQUIRED:

1. **Threading Changes**
   - Adding/removing locks
   - Changing thread creation
   - Modifying shared state access
   - **→ Test with concurrent operations**

2. **Order Pipeline Changes**
   - Modifying order state machine
   - Changing order preparation logic
   - **→ Test all order types and scenarios**

3. **TWS Service Changes**
   - Modifying IBKR API calls
   - Changing connection handling
   - **→ Test connection drops, reconnects**

4. **State Management**
   - Changing in-memory state structure
   - Modifying service dependencies
   - **→ Test state persistence/restoration**

---

## 🛠️ Development Tools

### 1. **Add Debug Mode**

```python
# In main.py or config
DEBUG_MODE = os.getenv("ARCTRIGGER_DEBUG", "false").lower() == "true"

if DEBUG_MODE:
    logging.setLevel(logging.DEBUG)
    # Enable additional checks
    enable_deadlock_detection()
    enable_state_validation()
```

### 2. **Add Validation Checks**

```python
# Validate order state consistency
def validate_order_state(order: Order):
    if order.state == OrderState.PENDING and order._ib_order_id:
        logging.warning(f"Order {order.order_id} has IB ID but is PENDING")
    
    if order.state == OrderState.ACTIVE and not order._ib_order_id:
        logging.warning(f"Order {order.order_id} is ACTIVE but has no IB ID")
```

### 3. **Use Type Hints**

```python
# Helps catch errors early
def place_order(
    self,
    symbol: str,
    strike: float,  # Not Optional[float] - must be provided
    quantity: int
) -> Order:
    ...
```

---

## 📝 Code Review Checklist

Before committing, ask:

- [ ] Are all exceptions caught and logged?
- [ ] Are locks acquired in consistent order?
- [ ] Is shared state protected?
- [ ] Are inputs validated?
- [ ] Are None checks in place?
- [ ] Is logging sufficient for debugging?
- [ ] Are threads properly cleaned up?
- [ ] Does it handle connection failures?
- [ ] Does it handle market closed scenarios?
- [ ] Is it tested in paper trading?

---

## 🎓 Learning Resources

1. **Python Threading Best Practices**
   - https://docs.python.org/3/library/threading.html
   - Focus on: Lock ordering, deadlock prevention

2. **Trading System Design**
   - Error recovery patterns
   - State machine design
   - Idempotency

3. **IBKR API Documentation**
   - Understand callback guarantees
   - Error code meanings

---

## 🚀 Quick Wins (Low Risk, High Value)

1. **Add Input Validation** - Prevents crashes from bad data
2. **Improve Error Messages** - Makes debugging easier
3. **Add Logging** - Better visibility into issues
4. **Add Type Hints** - Catches errors at development time
5. **Add Docstrings** - Helps understand code later

---

## ⚡ Emergency Procedures

### If Application Breaks:

1. **Stop Trading Immediately**
   - Close application
   - Check TWS for any pending orders
   - Manually cancel if needed

2. **Check Logs**
   - Look for last successful operation
   - Identify error pattern
   - Check for thread deadlocks

3. **Rollback**
   ```bash
   git log  # Find last working commit
   git checkout <commit-hash>
   ```

4. **Report Issue**
   - Document what happened
   - Save logs
   - Note time and conditions

---

## 📈 Continuous Improvement

1. **Weekly Review**
   - Review logs for patterns
   - Identify recurring issues
   - Plan fixes

2. **Monthly Audit**
   - Review threading code
   - Check for deadlock risks
   - Update documentation

3. **Version Control**
   - Tag stable versions
   - Keep release notes
   - Document known issues

---

## 🎯 Success Metrics

Track these to measure improvement:

- **Stability**: Days without crashes
- **Error Rate**: Exceptions per day
- **Recovery Time**: Time to recover from errors
- **Test Coverage**: % of critical paths tested
- **Code Quality**: Linter warnings, complexity metrics

---

**Remember**: In trading systems, **stability > features**. 
A simple, working system is better than a complex, broken one.
