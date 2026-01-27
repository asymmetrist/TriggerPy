# Integration Build Testing Checklist

**Build:** `ArcTrigger_integration.exe`  
**Date:** January 27, 2026  
**Branch:** `integration`

---

## Features Included in This Build

1. Premarket popup with ATM strike calculation
2. Real-time premium streaming
3. Option conID caching + persistence
4. Auto-refresh work symbols
5. Strike loading fix
6. State Pattern for order pipeline (with retry logic)
7. Comprehensive logging

---

## Pre-Test Setup

- [ ] TWS/IB Gateway is running and connected
- [ ] `.env` file has valid `POLYGON_API_KEY`
- [ ] Market is open (or use premarket mode for premarket tests)

---

## 1. Work Symbols & ConID Caching

### 1.1 Add New Symbol
- [ ] Open Work Symbols window
- [ ] Type a symbol (e.g., `AAPL`) and press Enter
- [ ] Verify symbol appears in list
- [ ] Verify status shows `⏳` then `✅` when conID resolves
- [ ] Close and reopen app - verify symbol persists

### 1.2 Refresh Individual Symbol
- [ ] Click the refresh button (🔄) next to a symbol
- [ ] Verify status updates to `⏳` then `✅`

### 1.3 Refresh All ConIDs
- [ ] Click "Refresh All ConIDs" button
- [ ] Verify confirmation dialog appears
- [ ] Confirm and wait for completion
- [ ] Verify success message shows count of cached conIDs

### 1.4 Remove Symbol
- [ ] Click the delete button (✕) next to a symbol
- [ ] Verify symbol is removed from list
- [ ] Close and reopen app - verify symbol is gone

---

## 2. Strike Loading

### 2.1 Strike Population on Maturity Selection
- [ ] Select a symbol in order window
- [ ] Wait for maturities to load
- [ ] Select a maturity date
- [ ] Verify strikes populate automatically in dropdown
- [ ] Verify strikes are centered around trigger price

---

## 3. Real-Time Premium Streaming

### 3.1 Premium Stream Starts
- [ ] Place an order (don't trigger yet)
- [ ] Check logs for: `Started premium stream for [SYMBOL]`
- [ ] Verify no "Premium not available" errors

### 3.2 Premium Used at Trigger
- [ ] Let trigger hit
- [ ] Check logs for: `Premium from stream: X.XX (Xms latency)`
- [ ] Verify entry price is calculated from streamed premium

---

## 4. Premarket Popup

### 4.1 Premarket Detection
- [ ] Start app before market open (before 9:30 AM ET)
- [ ] Place an order
- [ ] Verify app detects premarket mode
- [ ] Check for premarket rebase popup

### 4.2 ATM Strike Calculation
- [ ] Verify popup shows ATM strike suggestion
- [ ] Verify trigger price is calculated based on current price + offset

---

## 5. Order Pipeline (State Pattern)

### 5.1 Normal Order Flow
- [ ] Place a BUY CALL order
- [ ] Let trigger hit
- [ ] Verify order progresses through states in logs:
  - `Pending`
  - `PremiumFetching`
  - `Calculating`
  - `Placing`
  - `WaitingForFill`
  - `Finalized`

### 5.2 Premium Retry Logic
- [ ] Check logs for retry attempts if premium is stale
- [ ] Verify order doesn't fail immediately on first premium miss

### 5.3 Order Type (LMT vs MKT)
- [ ] Select MKT order type in UI
- [ ] Trigger order
- [ ] **CRITICAL:** Verify logs show `MKT BUY` not `LMT BUY`
- [ ] Repeat with LMT order type

---

## 6. Order Queue Rebasing

### 6.1 Rebase Queued Order
- [ ] Place order in premarket (before market open)
- [ ] Use rebase button to change trigger price
- [ ] Verify log shows: `Rebased queued order`
- [ ] Verify new trigger is used when market opens

### 6.2 Rebase Pending Order
- [ ] Place order during market hours
- [ ] Before trigger hits, use rebase button
- [ ] Verify log shows: `Rebased pending order in wait service`

---

## 7. Logging Verification

### 7.1 Comprehensive Logging
- [ ] Verify logs include timestamps with milliseconds
- [ ] Verify key events are logged:
  - Order placement
  - Trigger monitoring
  - Trigger hit
  - Premium fetch
  - Order sent
  - Order status updates

### 7.2 Latency Logging
- [ ] Check for `[TWS-LATENCY]` entries
- [ ] Verify trigger-to-order latency is logged

---

## 8. Known Issues to Watch For

### 8.1 ConID Timeout (Fixed?)
- [ ] Add a new symbol
- [ ] Verify conID resolves within 10 seconds
- [ ] Check logs - should NOT see timeout errors for conID resolution

### 8.2 LMT Order Bug (Fixed?)
- [ ] When MKT selected, verify order is sent as MKT
- [ ] Check `REQUEST placeOrder` in logs for order type

### 8.3 Premium Stream Missing
- [ ] If premium stream fails, check for fallback behavior
- [ ] Should NOT see "Order abandoned" due to missing premium

---

## Test Results

| Test | Pass/Fail | Notes |
|------|-----------|-------|
| 1.1 Add Symbol | | |
| 1.2 Refresh Individual | | |
| 1.3 Refresh All | | |
| 1.4 Remove Symbol | | |
| 2.1 Strike Loading | | |
| 3.1 Premium Stream | | |
| 3.2 Premium at Trigger | | |
| 4.1 Premarket Detection | | |
| 4.2 ATM Strike Calc | | |
| 5.1 Order Flow | | |
| 5.2 Premium Retry | | |
| 5.3 Order Type | | |
| 6.1 Rebase Queued | | |
| 6.2 Rebase Pending | | |
| 7.1 Comprehensive Logs | | |
| 7.2 Latency Logs | | |
| 8.1 ConID Timeout | | |
| 8.2 LMT Bug | | |
| 8.3 Premium Fallback | | |

---

## Bugs Found

| Bug ID | Description | Severity | Steps to Reproduce |
|--------|-------------|----------|-------------------|
| | | | |

---

## Notes

- 
- 
- 

---

**Tester:** _______________  
**Date:** _______________  
**Result:** PASS / FAIL
