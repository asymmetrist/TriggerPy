import requests
import logging
import json
import threading
import time
import websocket
import datetime
import os
from datetime import time as datetime_time  # Import 'time' with an alias
from typing import Optional, Dict
# Import the new callback manager
from Services.callback_manager import callback_manager, ThreadedCallbackService 
# --- CORRECTED IMPORT ---
# Use the constants from your provided library
from Services.nasdaq_info import EASTERN, MARKET_OPEN

# Try to load dotenv if available (optional dependency)
try:
    from dotenv import load_dotenv
    import sys
    
    # ✅ FIX: Look for .env file in the executable's directory (for PyInstaller)
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        exe_dir = os.path.dirname(sys.executable)
        env_path = os.path.join(exe_dir, '.env')
    else:
        # Running as script
        env_path = '.env'
    
    # Load .env file, don't override existing env vars, don't resolve variables
    load_dotenv(env_path, override=False, interpolate=False)
except ImportError:
    # dotenv not installed, will use system environment variables
    pass
except Exception as e:
    # If dotenv fails for any reason, log and continue (will use system env vars)
    logging.warning(f"[PolygonService] Failed to load .env file: {e}. Using system environment variables.")


class PolygonService:
    def __init__(self):
        # ✅ SECURITY FIX: Get API key from environment variable
        self.api_key = os.getenv("POLYGON_API_KEY")
        if not self.api_key:
            raise ValueError(
                "POLYGON_API_KEY environment variable not set! "
                "Please create a .env file with: POLYGON_API_KEY=your_key_here"
            )

        self.base_url = "https://api.polygon.io"
        self.ws_url = "wss://socket.polygon.io/stocks"

        # WS için:
        # ❌ self.subscriptions = {}  <-- REMOVED: Now managed by callback_manager
        self.ws = None
        self.ws_thread = None
        # Track active WS subscriptions to avoid sending 'subscribe' message multiple times
        self._active_ws_symbols = set() 
        self._ws_lock = threading.Lock()
        
        self._premarket_cache = {}


        # Background websocket start
        #self._start_ws()

    # ---------------- REST METHODS ----------------
    def get_option_snapshot(self, underlying: str, expiry: str, strike: float, right: str):
        """
        Fetch current snapshot for a given option contract (TWS-compatible format).
        Uses the latest Polygon v3 API (no /{occ} path).
        Returns {'symbol', 'bid', 'ask', 'last', 'mid', 'updated'} or None.
        """
        try:
            # Polygon now requires contract filters via query params
            expiration_date = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:8]}"
            contract_type = "call" if right.upper().startswith("C") else "put"

            url = f"{self.base_url}/v3/snapshot/options/{underlying.upper()}"
            params = {
                "apiKey": self.api_key,
                "expiration_date": expiration_date,
                "strike_price": strike,
                "contract_type": contract_type,
                "limit": 1  # just get the matching contract
            }

            resp = requests.get(url, params=params, timeout=6)
            if resp.status_code == 404:
                logging.warning(f"[Polygon] Snapshot not found for {underlying} {strike}{right} {expiry}")
                return None
            resp.raise_for_status()

            payload = resp.json()
            results = payload.get("results", [])
            if not results:
                logging.warning(f"[Polygon] Empty results for {underlying} {expiry} {strike}{right}")
                return None

            data = results[0]
            quote = data.get("last_quote", {})
            trade = data.get("last_trade", {})
            bid = quote.get("bid")
            ask = quote.get("ask")
            last = trade.get("price")
            mid = (bid + ask) / 2 if bid and ask else last

            return {
                "symbol": data.get("details", {}).get("ticker"),
                "bid": bid,
                "ask": ask,
                "last": last,
                "mid": mid,
                "updated": data.get("updated", None)
            }

        except Exception as e:
            logging.error(f"[Polygon] get_option_snapshot failed: {e}")
            return None



    def _get_option_from_chain(self, underlying: str, expiry: str, strike: float, right: str):
        """
        Fallback: scan full chain with wider tolerance and debug logs.
        """
        try:
            url  = f"{self.base_url}/v3/snapshot/options/{underlying.upper()}"
            params = {"apiKey": self.api_key}
            resp = requests.get(url, params=params, timeout=8)
            resp.raise_for_status()

            results = resp.json().get("results", [])
            if not results:
                logging.warning("[Polygon] Chain returned zero contracts for %s", underlying)
                return None

            target_year  = expiry[:4]
            target_month = expiry[4:6]
            target_day   = int(expiry[6:8])
            right        = right.upper()[0]
            best_match   = None
            best_diff    = 999.

            for item in results:
                details = item.get("details", {})
                if not details:
                    continue
                exp  = details.get("expiration_date", "")
                str_strike = details.get("strike_price")
                ctype  = details.get("contract_type", "").upper()

                if not exp or str_strike is None or not ctype:
                    continue

                exp_y, exp_m, exp_d = exp.split("-")
                if exp_y != target_year or exp_m != target_month:
                    continue

                # ±14 calendar days
                if abs(int(exp_d) - target_day) > 14:
                    continue

                if ctype[0] != right:
                    continue

                # 3-decimal strike tolerance
                                # 🔧 PATCH: tolerate small rounding / feed mismatch
                diff = abs(float(str_strike) - float(strike))
                if diff < 0.25 and diff < best_diff:   # was strict 3-decimal
                    best_diff = diff
                    best_match = item

            # 🔧 PATCH: soft-fail + fallback check
            if not best_match:
                logging.warning(
                    "[Polygon] No *exact* match in chain for %s %s %s%s — retrying loose filter",
                    underlying, expiry, strike, right,
                )
                # retry once more with ±1-point tolerance (TSLA 449–451 type cases)
                for item in results:
                    details = item.get("details", {}) or {}
                    s = details.get("strike_price")
                    if s and abs(float(s) - float(strike)) < 1.0 \
                       and right[0] in details.get("contract_type", "").upper():
                        best_match = item
                        break

            if not best_match:
                logging.warning(
                    "[Polygon] No matching contract after loose retry for %s %s %s%s",
                    underlying, expiry, strike, right,
                )
                return None


            quote = best_match.get("last_quote", {})
            trade = best_match.get("last_trade", {})
            bid   = quote.get("bid")
            ask   = quote.get("ask")
            last  = trade.get("price")
            mid   = (bid + ask) / 2 if (bid and ask) else last
            details = best_match.get("details", {})
            return {
                "symbol": details.get("ticker"),
                "bid": bid,
                "ask": ask,
                "last": last,
                "mid": mid,
                "updated": best_match.get("updated"),
            }

        except Exception as e:
            logging.error("[Polygon] _get_option_from_chain failed: %s", e)
            return None


    def get_last_trade(self, symbol: str):
        """
        Get last trade price. During replay mode, returns replayed price.
        """
        # Check if replay is active and get replayed price
        try:
            from Replay.replay_service import get_replay_price
            replay_price = get_replay_price(symbol)
            if replay_price is not None:
                logging.debug(f"[Polygon] Using replayed price for {symbol}: {replay_price}")
                return replay_price
        except Exception as e:
            logging.debug(f"[Polygon] Replay check failed (normal if not replaying): {e}")
        
        # Normal real-time fetch
        url = f"{self.base_url}/v2/last/trade/{symbol.upper()}"
        params = {"apiKey": self.api_key}
        try:
            resp = requests.get(url, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", {}).get("p")
        except Exception as e:
            logging.error(f"[Polygon] get_last_trade failed: {e}")
            return None


    
    def get_snapshot(self, symbol: str):
        """
        Real-time L1 snapshot for a single stock – Polygon v2 Pro endpoint.
        During replay mode, returns simulated snapshot from replayed prices.
        """
        # Check if replay is active and get replayed price
        try:
            from Replay.replay_service import get_replay_price
            replay_price = get_replay_price(symbol)
            if replay_price is not None:
                # Return simulated snapshot from replayed price
                logging.debug(f"[Polygon] Using replayed price for {symbol}: {replay_price}")
                return {
                    "last": replay_price,
                    "bid": replay_price * 0.9999,  # Approximate bid (0.01% below)
                    "ask": replay_price * 1.0001,  # Approximate ask (0.01% above)
                    "today_high": replay_price,
                    "today_low": replay_price,
                    "prev_high": None,
                    "prev_low": None,
                }
        except Exception as e:
            logging.debug(f"[Polygon] Replay check failed (normal if not replaying): {e}")
        
        # Normal real-time fetch
        url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol.upper()}"
        params = {"apiKey": self.api_key}

        try:
            r = requests.get(url, params=params, timeout=5)
            r.raise_for_status()                       # <-- fail fast on 4xx/5xx
            payload = r.json()

            # Polygon sometimes nests under 'results' instead of 'ticker'
            ticker_node = payload.get("ticker") or payload.get("results")
            if not isinstance(ticker_node, dict):
                logging.warning(
                    "[Polygon] Unexpected payload for %s: %s",
                    symbol,
                    payload  # full body for inspection
                )
                return None

            last_trade = ticker_node.get("lastTrade", {})
            last_quote = ticker_node.get("lastQuote", {})
            day_bar    = ticker_node.get("day", {})
            prev_day   = ticker_node.get("prevDay", {})

            return {
                "last":      last_trade.get("p"),  # Polygon uses 'p' for price
                "bid":       last_quote.get("p"),  # bid price
                "ask":       last_quote.get("P"),  # ask price
                "today_high": day_bar.get("h"),
                "today_low":  day_bar.get("l"),
                "prev_high":  prev_day.get("h"),
                "prev_low":   prev_day.get("l"),
            }

        except requests.HTTPError as e:
            logging.error("[Polygon] HTTP error for %s: %s  body=%s",
                        symbol, e, r.text)
        except Exception as e:
            logging.error("[Polygon] get_snapshot failed for %s: %s", symbol, e)

        return None


    def _get_premarket_aggregates(self, symbol: str) -> Optional[Dict]:
        """
        Private helper to get the true premarket H/L.
        Uses EASTERN and MARKET_OPEN from nasdaq_info.py.
        """
        # --- LOGIC CORRECTED ---
        # 1. Use the imported timezone
        now_et = datetime.datetime.now(EASTERN)
        today_str = now_et.strftime('%Y-%m-%d')
        cache_key = f"{symbol}_{today_str}"

        # 2. Check cache first
        if cache_key in self._premarket_cache:
            return self._premarket_cache[cache_key]

        # 3. Define premarket window using imported constants
        PREMARKET_START_TIME = datetime_time(4, 0) # 4:00 AM
        
        premarket_start = datetime.datetime.combine(now_et.date(), PREMARKET_START_TIME, tzinfo=EASTERN)
        market_open = datetime.datetime.combine(now_et.date(), MARKET_OPEN, tzinfo=EASTERN)

        if now_et < premarket_start:
            logging.warning(f"[Polygon] Premarket query for {symbol} run before 4 AM ET.")
            return None # Premarket hasn't started

        # 4. Determine query range (from 4AM until now, or 9:30)
        query_end = min(now_et, market_open)
        start_ms = int(premarket_start.timestamp() * 1000)
        end_ms = int(query_end.timestamp() * 1000)

        # 5. Build and execute API call
        url = f"{self.base_url}/v2/aggs/ticker/{symbol.upper()}/range/1/minute/{start_ms}/{end_ms}"
        params = {"apiKey": self.api_key, "sort": "asc", "adjusted": "true"}

        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("resultsCount", 0) == 0 or not data.get("results"):
                logging.warning(f"[Polygon] No premarket bars found for {symbol}.")
                return None

            # 6. Find the highest high and lowest low
            bars = data.get("results")
            result = {
                "high": max(bar['h'] for bar in bars),
                "low": min(bar['l'] for bar in bars)
            }

            # 7. Only cache if the premarket session is over
            if now_et >= market_open:
                self._premarket_cache[cache_key] = result
            
            return result

        except Exception as e:
            logging.error(f"[Polygon] _get_premarket_aggregates failed for {symbol}: {e}")
            return None

    # --- Public-Facing Data Methods ---

    def get_premarket_high(self, symbol: str) -> Optional[float]:
        """Gets the true premarket high (4:00 - 9:30 AM ET)."""
        data = self._get_premarket_aggregates(symbol)
        return data.get('high') if data else None

    def get_premarket_low(self, symbol: str) -> Optional[float]:
        """Gets the true premarket low (4:00 - 9:30 AM ET)."""
        data = self._get_premarket_aggregates(symbol)
        return data.get('low') if data else None

    def get_intraday_high(self, symbol: str) -> Optional[float]:
        """Gets the current day's high from the snapshot."""
        data = self.get_snapshot(symbol)
        return data.get('today_high') if data else None

    def get_intraday_low(self, symbol: str) -> Optional[float]:
        """Gets the current day's low from the snapshot."""
        data = self.get_snapshot(symbol)
        return data.get('today_low') if data else None

    # ---------------- WS METHODS ----------------
    def subscribe(self, symbol: str, callback):
        """Register a callback and send WS subscription if it's the first for this symbol."""
        sym = symbol.upper()
        # 1. Add callback to manager
        callback_manager.add_callback(sym, callback)
        
        # 2. Check if WS subscription is needed (thread-safe check)
        with self._ws_lock:
            if sym in self._active_ws_symbols:
                logging.debug(f"[Polygon] Callback added for {sym}. WS subscription already active.")
                return

            # If not active, mark it and send WS message
            self._active_ws_symbols.add(sym)
        
        if self.ws:
            msg = {"action": "subscribe", "params": f"T.{sym}"}
            try:
                self.ws.send(json.dumps(msg))
                logging.info(f"[Polygon] WS subscribed to T.{sym}")
            except Exception as e:
                logging.error(f"[Polygon] WS subscribe error: {e}")


    def unsubscribe(self, symbol: str, callback):
        """Remove a specific callback and send WS unsubscribe if it was the last."""
        sym = symbol.upper()

        # 1. Remove callback from manager
        callback_manager.remove_callback(sym, callback)

        # 2. Check if WS unsubscription is needed (thread-safe check)
        remaining_symbols = callback_manager.list_symbols()

        with self._ws_lock:
            # Check if symbol is still required by any other callback
            if sym not in remaining_symbols and sym in self._active_ws_symbols:
                self._active_ws_symbols.remove(sym)
                
                if self.ws:
                    msg = {"action": "unsubscribe", "params": f"T.{sym}"}
                    try:
                        self.ws.send(json.dumps(msg))
                        logging.info(f"[Polygon] WS unsubscribed from T.{sym}")
                    except Exception as e:
                        logging.error(f"[Polygon] WS unsubscribe error: {e}")
            elif sym in self._active_ws_symbols:
                 logging.debug(f"[Polygon] Callback removed for {sym}. WS subscription remains active.")


    def _start_ws(self):
        """Background thread ile WS başlat."""
        def run():
            while True:
                try:
                    self.ws = websocket.WebSocketApp(
                        self.ws_url,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close
                    )
                    self.ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as e:
                    logging.error(f"[Polygon] WS connection error: {e}")
                time.sleep(5)

        self.ws_thread = threading.Thread(target=run, daemon=True)
        self.ws_thread.start()

    def _on_open(self, ws):
        auth_msg = {"action": "auth", "params": self.api_key}
        ws.send(json.dumps(auth_msg))
        logging.info("[Polygon] WS connected & authenticated")

        # After re-authentication, resubscribe to all symbols
        with self._ws_lock:
            for sym in self._active_ws_symbols:
                msg = {"action": "subscribe", "params": f"T.{sym}"}
                try:
                    ws.send(json.dumps(msg))
                    logging.info(f"[Polygon] Re-subscribed to T.{sym}")
                except Exception as e:
                    logging.error(f"[Polygon] WS re-subscribe error for {sym}: {e}")

    def _on_message(self, ws, message):
        """
File `nasdaq_info.py` provided by the user.

        Receives message and triggers ALL registered callbacks via the manager.
        """
        try:
            data = json.loads(message)
            for event in data:
                if event.get("ev") == "T":
                    sym = event.get("sym")
                    price = event.get("p")
                    if sym and price is not None:
                        # 🎯 The FIX: Trigger all callbacks for this symbol via the manager
                        callback_manager.trigger(sym, price)
        except Exception as e:
            logging.error(f"[Polygon] WS message error: {e} | {message}")

    def _on_error(self, ws, error):
        logging.error(f"[Polygon] WS error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logging.warning(f"[Polygon] WS closed: {close_status_code} {close_msg}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    svc = PolygonService()
    snapshot = svc.get_option_snapshot("QQQ", "20251021", 612.5, "C")
    print(snapshot)
    time.sleep(3)

# Note: assuming polygon_service is initialized after callback_manager is available
polygon_service = PolygonService()