import logging
from typing import List, Dict, Optional, Tuple
import uuid
from Services.price_watcher import PriceWatcher
from Services.tws_service import create_tws_service, TWSService
from Services.polygon_service import polygon_service
from Services.order_wait_service import wait_service
from Services.order_manager import order_manager
from Helpers.Order import Order, OrderState
import random
import time
from Services.nasdaq_info import is_market_closed_or_pre_market
from Services.order_queue_service import order_queue , OrderQueueService
from Services.order_fixer_service import order_fixer, OrderFixerService

def align_expiry_to_friday(expiry: str) -> str:
    import datetime
    y, m, d = int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8])
    date = datetime.date(y, m, d)
    # shift to Friday if not already
    while date.weekday() != 4:
        date += datetime.timedelta(days=1)
    return date.strftime("%Y%m%d")

# --- Singleton: GeneralApp ---
class GeneralApp:
    def __init__(self, tws : TWSService = create_tws_service(), fixer: OrderFixerService = order_fixer):
        self._fixer = fixer
        self._tws = tws
        self._polygon = None
        self._order_wait = None
        self._connected = False
        self._models = set()

    

    def pre_conid(self,order : Order):
        return self._tws.pre_conid(order)


    def save(self, filename: Optional[str] = "ARCTRIGGER.DAT") -> str:
        """
        Save all models to ARCTRIGGER.DAT (or given filename) in this format:

            N
            AppModel:...
            [Order:...]
            AppModel:...
            ...

        Returns the filename used.
        """
        lines = [str(len(self._models))]
        for m in self._models:
            serialized = m.serialize()
            for ln in serialized.split("\n"):
                lines.append(ln)

        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            logging.info(f"[GeneralApp.save()] Saved {len(self._models)} models → {filename}")
        except Exception as e:
            logging.error(f"[GeneralApp.save()] Failed to save models: {e}")
            raise

        return filename

    def load(self, filename: Optional[str] = "ARCTRIGGER.DAT") -> None:
        """
        Load models from ARCTRIGGER.DAT (or given filename).
        Each model may consume 1 or 2 lines depending on whether it has an order.
        """
        try:
            with open(filename, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
        except FileNotFoundError:
            logging.warning(f"[GeneralApp.load()] File not found: {filename}")
            return
        except Exception as e:
            logging.error(f"[GeneralApp.load()] Failed to read file: {e}")
            return

        if not lines:
            logging.warning(f"[GeneralApp.load()] File empty: {filename}")
            return

        try:
            count = int(lines[0])
        except Exception as e:
            logging.error(f"[GeneralApp.load()] Invalid header line: {e}")
            return

        self._models.clear()
        idx = 1
        loaded = 0

        while idx < len(lines):
            if not lines[idx].startswith("AppModel:"):
                idx += 1
                continue
            try:
                model, consumed = AppModel.deserialize(lines[idx: idx + 2])
                self._models.add(model)
                loaded += 1
                idx += consumed
            except Exception as e:
                logging.error(f"[GeneralApp.load()] Error parsing model at line {idx}: {e}")
                idx += 1  # skip to next line safely

        logging.info(f"[GeneralApp.load()] Restored {loaded}/{count} models from {filename}")

        # Reattach pending orders (safe)
        for model in self._models:
            if model.order and (model.order.state in (OrderState.PENDING, OrderState.ACTIVE)):
                try:
                    self._order_wait.add_order(model.order, mode="poll")
                    logging.info(f"[GeneralApp.load()] Reattached pending order {model.order.order_id}")
                except Exception as e:
                    logging.error(f"[GeneralApp.load()] Failed to reattach order: {e}")
            elif model.order and model.order.state == OrderState.FINALIZED:
                order_manager.add_finalized_order(model.order)
                logging.info(f"[GeneralApp.load()] Reattached finalized order {model.order.order_id}")

    def get_market_data_for_trigger(self, symbol: str, day: str) -> Optional[Dict]:
        """
        Wrapper around polygon service to retrieve high/low data.
        Day can be 'intraday' (today) or 'premarket'.
        Returns {'high', 'low'} or None.
        """
        if not self._polygon:
            raise RuntimeError("GeneralApp: Polygon not connected")
        
        # --- THIS IS THE FIX ---
        # Stop calling get_snapshot() and use the new, correct methods.
        
        if day == 'intraday':
            # Use today's HOD/LOD
            return {
                'high': self._polygon.get_intraday_high(symbol),
                'low': self._polygon.get_intraday_low(symbol)
            }
        elif day == 'premarket':
            # Use the new service to get the true premarket H/L
            return {
                'high': self._polygon.get_premarket_high(symbol),
                'low': self._polygon.get_premarket_low(symbol)
            }
        return None

    def add_model(self, model: "AppModel"):
        self._models.add(model)


    def add_order(self, order: Order):
        self._fixer.fix_async(order)
        self.order_wait.add_order(order, mode="poll")

    def get_models(self):
        return list(self._models)

    def amount_of_models(self):
        return len(self._models)

    def cancel_order(self, order_id):
        self.order_wait.cancel_order(order_id)

    def get_option_chain(self, symbol: str, expiry: str):
        """
        Wrapper around TWSService.get_option_chain.
        Models call this, never touch TWSService directly.
        """
        if not self._tws:
            raise RuntimeError("GeneralApp: TWS not connected")
        return self._tws.get_option_chain(symbol, expiry)

    def place_custom_order(self, order) -> bool:
        """
        Proxy to TWS place_custom_order.
        Prevents models from touching self._tws directly.
        """
        if not self._tws:
            logging.info("TWS NOT CONNECTED ERR PLACING ORDER")
            return False
        return self._tws.place_custom_order(order)

    def get_option_premium(self, symbol: str, expiry: str, strike: float, right: str):
        """
        Returns option premium using ONLY TWS snapshot (Polygon fully bypassed).
        """
        if not self._tws:
            raise RuntimeError("GeneralApp: TWS not connected")
        try:
            snap = self._tws.get_option_snapshot(symbol, expiry, strike, right)
            if snap and snap.get("mid"):
                return snap["mid"]
            logging.warning(f"[GeneralApp] No mid-price from TWS snapshot for {symbol}")
            return None
        except Exception as e:
            logging.error(f"[GeneralApp] Failed to fetch TWS premium: {e}")
            return None


    def get_option_snapshot(self, symbol: str, expiry: str, strike: float, right: str):
        """
        Wrapper around TWSService.get_option_snapshot.
        Returns {'bid', 'ask', 'last', 'mid'} or None.
        Used by AppModel and risk modules to get live option pricing.
        """
        if not self._tws:
            raise RuntimeError("GeneralApp: TWS not connected")
        twsshot = self._tws.get_option_snapshot(symbol, expiry, strike, right)
        return twsshot

    def connect(self) -> bool:
        """Connect global services once for all models."""
        try:
            self._tws = create_tws_service()
            self._polygon = polygon_service
            self._order_wait = wait_service
            if self._tws.connect_and_start():
                self._connected = True
                logging.info("GeneralApp: Services connected")
                return True
            logging.error("GeneralApp: Failed to connect to TWS")
            return False
        except Exception as e:
            logging.error(f"GeneralApp: Connection failed: {e}")
            return False

    def disconnect(self):
        """Disconnect global services once for all models."""
        try:
            if self._tws:
                self._tws.disconnect_gracefully()
            self._tws = None
            self._polygon = None
            self._order_wait = None
            self._connected = False
            logging.info("GeneralApp: Services disconnected")
        except Exception as e:
            logging.error(f"GeneralApp: Disconnection error: {e}")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def watch_price(self, symbol, update_fn):
        watcher = PriceWatcher(symbol, update_fn, polygon_service)
        return watcher

    # --- Wrappers around services ---
    def search_symbol(self, query: str):
        if not self._tws:
            raise RuntimeError("GeneralApp: TWS not connected")
        return self._tws.search_symbol(query)

    def get_snapshot(self, symbol: str):
        if not self._polygon:
            raise RuntimeError("GeneralApp: Polygon not connected")
        return self._polygon.get_snapshot(symbol)

    def get_maturity(self, symbol: str) -> Optional[str]:
        if not self._tws:
            return None
        try:
            maturities = self._tws.get_maturities(symbol)
            return max(maturities['expirations']) if maturities else None
        except Exception as e:
            logging.error(f"GeneralApp: Failed to get maturity for {symbol}: {e}")
            return None

    # expose internal services for AppModel
    @property
    def tws(self):
        return self._tws

    @property
    def polygon(self):
        return self._polygon

    @property
    def order_wait(self):
        return self._order_wait


# Global singleton instance
general_app = GeneralApp()


# --- Per-symbol model ---
class AppModel:
    def __init__(self, symbol: str, que : OrderQueueService = order_queue):
        self._id = str(uuid.uuid4())
        self._symbol = symbol.upper()
        self._underlying_price: Optional[float] = None
        self._expiry: Optional[str] = None
        self._strike: Optional[float] = None
        self._right: Optional[str] = None
        self._stop_loss: Optional[float] = None
        self._take_profit: Optional[float] = None
        self._order: Optional[Order] = None
        self._status_callback: Optional[callable] = None  # added
        self.order_queue = order_queue

    def cancel_queued(self):
        self.order_queue.cancel_queued_orders_for_model(self)

    # ------------------------------------------------------------------
    def set_status_callback(self, fn):
        """Allow UI (OrderFrame) to supply its _set_status method."""
        if callable(fn):
            self._status_callback = fn
        else:
            self._status_callback = None
    # ------------------------------------------------------------------

    
    def get_premarket_trigger_price(self) -> Optional[float]:
        """
        🎯 Gets Premarket High (for CALL) or Premarket Low (for PUT) using
        the previous day's high/low as a proxy.
        """
        if not self._right:
            logging.warning(f"AppModel[{self._symbol}]: Right (C/P) not set for premarket trigger.")
            return None
        
        # Use the wrapper defined in GeneralApp
        market_data = general_app.get_market_data_for_trigger(self._symbol, 'premarket')
        
        if not market_data:
            return None
            
        if self._right == "C":
            # CALL: Trigger price should be Premarket High
            return market_data.get('high')
        elif self._right == "P":
            # PUT: Trigger price should be Premarket Low
            return market_data.get('low')
        return None

    def get_intraday_trigger_price(self) -> Optional[float]:
        """
        🎯 Gets Today's High of Day (HOD) (for CALL) or Low of Day (LOD) (for PUT).
        """
        if not self._right:
            logging.warning(f"AppModel[{self._symbol}]: Right (C/P) not set for intraday trigger.")
            return None
        
        # Use the wrapper defined in GeneralApp
        market_data = general_app.get_market_data_for_trigger(self._symbol, 'intraday')
        
        if not market_data:
            return None
            
        if self._right == "C":
            # CALL: Trigger price should be Today's High (HOD)
            return market_data.get('high')
        elif self._right == "P":
            # PUT: Trigger price should be Today's Low (LOD)
            return market_data.get('low')
        return None

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def order(self):
        return self._order

    def refresh_market_price(self) -> Optional[float]:
        try:
            self._underlying_price = general_app.polygon.get_last_trade(self._symbol)
            logging.info(f"AppModel[{self._symbol}]: Market price {self._underlying_price}")
            return self._underlying_price
        except Exception as e:
            logging.error(f"AppModel[{self._symbol}]: Failed to get market price: {e}")
            return None
    

    def rebase_premarket_to_new_extreme(self) -> Optional[float]:
        """
        Rebase queued premarket order trigger to latest
        premarket high (CALL) or low (PUT).
        Also updates internal strike to ATM.
        Returns new trigger if successful.
        """
        if not self._right:
            logging.warning(f"[{self._symbol}] Cannot rebase: right not set")
            return None

        # 1. Get latest PM extreme
        market_data = general_app.get_market_data_for_trigger(self._symbol, 'premarket')
        if not market_data:
            return None

        new_trigger = (
            market_data["high"] if self._right == "C"
            else market_data["low"]
        )

        if not new_trigger or new_trigger <= 0:
            return None

        # 2. Keep original strike - don't recalculate from trigger
        # Strike stays as originally set

        # 3. Rebase queued action
        success = order_queue.rebase_queued_premarket_order(
            self.order,
            new_trigger=new_trigger
        )

        if success:
            logging.info(
                f"[{self._symbol}] Premarket rebase → trigger {new_trigger}, strike {self._strike}"
            )
            return new_trigger

        return None


    # ---------------- Option & Risk ----------------
    def set_option_contract(self, expiry: str, strike: float, right: str) -> Tuple[str, float, str]:
        right = right.upper()
        if right in ("CALL", "C"):
            self._right = "C"
        elif right in ("PUT", "P"):
            self._right = "P"
        else:
            raise ValueError("Right must be CALL/PUT or C/P")

        if not self._validate_option_contract(expiry, strike, self._right):
            raise ValueError(f"Invalid option contract: {expiry} {strike} {self._right}")

        self._expiry = expiry
        self._strike = strike
        logging.info(f"AppModel[{self._symbol}]: Contract set {expiry} {strike}{self._right}")
        return self._expiry, self._strike, self._right

    def _validate_option_contract(self, expiry: str, strike: float, right: str) -> bool:
        """
        Validate that the given expiry & strike exist in the TWS chain.
        During premarket hours (before 9:30 ET), allow non-Friday expiries by aligning forward.
        """
        from datetime import datetime, time as dtime
        import pytz

        try:
            maturities = general_app.tws.get_maturities(self._symbol)
            if not maturities:
                logging.warning(f"[{self._symbol}] No maturities from TWS yet.")
                return False

            exp_ok = expiry in maturities['expirations']
            strike_ok = strike in maturities['strikes']

            if exp_ok and strike_ok:
                return True

            # 🕓 PREMARKET WINDOW (4:00 – 9:30 AM ET)
            EASTERN = pytz.timezone("US/Eastern")
            now_et = datetime.now(EASTERN).time()
            if dtime(4, 0) <= now_et < dtime(9, 30):
                # ✅ Allow expiry alignment in premarket
                aligned = align_expiry_to_friday(expiry)
                if aligned in maturities['expirations']:
                    logging.warning(f"[{self._symbol}] Premarket expiry {expiry} auto-aligned → {aligned}")
                    self._expiry = aligned
                    # ✅ BUT STILL VALIDATE STRIKE EXISTS - no bypass for invalid strikes!
                    if strike in maturities['strikes']:
                        return True
                    else:
                        logging.error(f"[{self._symbol}] Premarket: Strike {strike} not in chain (even after expiry alignment)")
                        return False
                else:
                    # ✅ Only bypass if chain data is truly unavailable (empty strikes list)
                    if not maturities.get('strikes') or len(maturities.get('strikes', [])) == 0:
                        logging.warning(f"[{self._symbol}] Premarket: Chain not yet loaded (no strikes available) - allowing for now")
                        return True
                    else:
                        # Chain is loaded but strike/expiry not found - reject it
                        logging.error(f"[{self._symbol}] Premarket: Expiry {expiry} and/or strike {strike} not in chain (chain is loaded)")
                        return False

            logging.error(f"[{self._symbol}] Contract invalid → expiry {expiry} or strike {strike} not in chain")
            return False

        except Exception as e:
            logging.error(f"[{self._symbol}] Contract validation error: {e}")
            return False


    def set_risk(self, stop_loss: float, take_profit: float):
        self._stop_loss = stop_loss
        self._take_profit = take_profit
        return self._stop_loss, self._take_profit

    def set_stop_loss(self, value: float):
        self._stop_loss = value
        return self._stop_loss

    def set_profit_taking(self, percent: float):
        entry_price = self.get_option_price(self._expiry, self._strike, self._right)
        if not entry_price:
            return None
        self._take_profit = round(entry_price * (1 + percent / 100), 2)
        return self._take_profit

    def set_breakeven(self):
        entry_price = self.get_option_price(self._expiry, self._strike, self._right)
        if entry_price:
            self._stop_loss = entry_price
        return self._stop_loss

    def calculate_quantity(self, position_size: float, price: float = None):
        if price is None:
            price = self.get_option_price(self._expiry, self._strike, self._right)
        if not price or price <= 0:
            return 0
        return int(position_size // price)

    # ---------------- Options Data via TWS ----------------
    def get_available_maturities(self) -> List[str]:
        try:
            maturities = general_app.tws.get_maturities(self._symbol)
            return sorted(maturities['expirations']) if maturities else []
        except Exception as e:
            logging.error(f"AppModel[{self._symbol}]: Failed to get maturities: {e}")
            return []

    def get_option_chain(self, expiry: str):
        try:
            if not general_app.tws:
                raise RuntimeError("TWS not connected")
            return general_app.get_option_chain(self._symbol, expiry=expiry) or []
        except Exception as e:
            logging.error(f"AppModel[{self._symbol}]: Failed to get option chain: {e}")
            return []

    def get_option_price(self, expiry: str, strike: float, right: str):
        chain = self.get_option_chain(expiry)
        for c in chain:
            if c["strike"] == strike and c["right"] == right:
                price = c.get("marketPrice") or c.get("bid") or c.get("ask")
                if price and price > 0:
                    return price
                else:
                    raise ValueError(f"No valid price for {expiry} {strike} {right}")
        raise ValueError(f"Option {expiry} {strike} {right} not found")

    # ---------------- Orders ----------------
    def _validate_breakout_trigger(self, trigger_price: Optional[float], current_price: float) -> bool:
        if trigger_price is None:
            return True
        if (self._right == "C" and trigger_price <= current_price) or (self._right == "P" and trigger_price >= current_price):
            logging.error(f"AppModel[{self._symbol}]: Breakout violation trigger {trigger_price} vs {current_price}")
            return False
        return True
    
    ### OK very interesting method
    def prepare_option_order(
    self,
    action: str = "BUY",
    position: int = 2000,
    quantity: int = 1,
    trigger_price: Optional[float] = None,
    arcTick: float = 0.01,
    type: str = "LMT",
    status_callback=None,
) -> Order:
        import time
        prep_start = time.time()
        
        logging.info(
            f"[ORDER_PREP] Starting order preparation | symbol={self._symbol} | "
            f"expiry={self._expiry} strike={self._strike} right={self._right} | "
            f"trigger={trigger_price} position=${position} | timestamp={prep_start:.3f}"
        )

        # --- 0. normalize expiry ---
        self._expiry = align_expiry_to_friday(self._expiry)
        logging.debug(f"[ORDER_PREP] Expiry normalized: {self._expiry}")

        if not all([self._symbol, self._expiry, self._strike, self._right]):
            error_msg = "Option parameters not set"
            logging.error(f"[ORDER_PREP] ❌ {error_msg} | symbol={self._symbol} expiry={self._expiry} strike={self._strike} right={self._right}")
            raise ValueError(error_msg)

        # --- 1. underlying price (SLOW) ---
        logging.info(f"[ORDER_PREP] Fetching underlying price for {self._symbol}...")
        price_start = time.time()
        current_price = self.refresh_market_price()
        price_latency = (time.time() - price_start) * 1000
        
        if not current_price:
            error_msg = "Underlying price unavailable"
            logging.error(f"[ORDER_PREP] ❌ {error_msg} | symbol={self._symbol} | latency={price_latency:.1f}ms")
            raise ValueError(error_msg)
        
        logging.info(f"[ORDER_PREP] ✅ Underlying price: ${current_price:.2f} | latency={price_latency:.1f}ms")

        if not self._validate_breakout_trigger(trigger_price, current_price):
            error_msg = f"Invalid breakout trigger: {trigger_price} vs current: {current_price}"
            logging.error(f"[ORDER_PREP] ❌ {error_msg}")
            raise ValueError("Invalid breakout trigger")

        # --- 2. Premium fetching removed - will be done at RTH open in _finalize_order() with retry logic ---
        # --- 3. SL / TP defaults (using placeholder, will be recalculated with actual premium) ---
        sl = self._stop_loss  # Will be recalculated in _finalize_order
        tp = self._take_profit  # Will be recalculated in _finalize_order

        # --- 4. build order (without premium/entry_price/qty - will be set in _finalize_order) ---
        order = Order(
            symbol=self._symbol,
            expiry=self._expiry,
            strike=self._strike,
            right=self._right,
            type=type,
            qty=None,  # Will be calculated in _finalize_order from premium
            entry_price=None,  # Will be calculated in _finalize_order from premium
            tp_price=tp,
            sl_price=sl,
            action=action.upper(),
            trigger=trigger_price,
        )
        order.set_position_size(float(position))
        order._order_ready = False  # Mark as not ready - premium will be fetched at RTH open
        
        logging.info(
            f"[ORDER_PREP] ✅ Order created | order_id={order.order_id} | "
            f"symbol={self._symbol} {self._expiry} {self._strike}{self._right} | "
            f"trigger={trigger_price} SL={sl} TP={tp} position=${position}"
        )

        # --- 6. attach callback EARLY ---
        cb = status_callback or self._status_callback
        if cb:
            order.set_status_callback(cb)
            logging.debug(f"[ORDER_PREP] Status callback attached")

        # --- 7. resolve IB contract (SLOW) ---
        logging.info(f"[ORDER_PREP] Resolving conID for {self._symbol} {self._expiry} {self._strike}{self._right}...")
        conid_start = time.time()
        conid_success = general_app.pre_conid(order)
        conid_latency = (time.time() - conid_start) * 1000
        
        if conid_success:
            logging.info(
                f"[ORDER_PREP] ✅ ConID resolved | conID={getattr(order, '_pre_conid', None)} | "
                f"latency={conid_latency:.1f}ms"
            )
        else:
            logging.warning(f"[ORDER_PREP] ⚠️ ConID resolution failed | latency={conid_latency:.1f}ms")
        
        prep_total = (time.time() - prep_start) * 1000
        logging.info(
            f"[ORDER_PREP] ✅ Order preparation complete | order_id={order.order_id} | "
            f"total_time={prep_total:.1f}ms | price_fetch={price_latency:.1f}ms conid={conid_latency:.1f}ms"
        )

        return order

    def _resolve_mid_premium(self, current_price: float, arcTick: float) -> float:
        """
        Fetch premium with NO fallback - raises ValueError if premium unavailable.
        Premium fetching with retry is handled in _finalize_order() at RTH open.
        """
        premium = general_app.get_option_premium(
            self._symbol, self._expiry, self._strike, self._right
        )

        if premium is None or premium <= 0:
            raise ValueError(f"No live premium available for {self._symbol} {self._expiry} {self._strike}{self._right}")

        mid = premium + arcTick

        if mid < 3:
            tick = 0.01
        elif mid >= 5:
            tick = 0.15
        else:
            tick = 0.05

        mid = round(int(round(mid / tick)) * tick, 2)
        return mid


    def place_option_order(
        self,
        action: str = "BUY",
        position: int = 2000,
        quantity: int = 1,
        trigger_price: Optional[float] = None,
        status_callback=None,
        arcTick=0.01,
        type="LMT"
    ) -> Dict:

        self._expiry = align_expiry_to_friday(self._expiry)

        # --- sanity ---
        if not all([self._symbol, self._expiry, self._strike, self._right]):
            raise ValueError("Option parameters not set")

        current_price = self.refresh_market_price()
        if not current_price:
            raise ValueError("Could not get underlying market price")

        if not self._validate_breakout_trigger(trigger_price, current_price):
            raise ValueError(f"Trigger {trigger_price} invalid for current price {current_price}")

        # ✅ ALWAYS defer premium fetching to pipeline (at trigger time)
        # Premium will be fetched when trigger hits using real-time stream or snapshot retry
        
        # --- SL / TP defaults (using placeholders, will be recalculated with actual premium) ---
        if self._stop_loss is None:
            self._stop_loss = 0.5  # Placeholder, will be recalculated
        if self._take_profit is None:
            self._take_profit = 1.2  # Placeholder, will be recalculated

        order = Order(
            symbol=self._symbol,
            expiry=self._expiry,
            strike=self._strike,
            right=self._right,
            type=type,
            qty=None,  # Will be calculated in _finalize_order from premium
            entry_price=None,  # Will be calculated in _finalize_order from premium
            tp_price=self._take_profit,
            sl_price=self._stop_loss,
            action=action.upper(),
            trigger=trigger_price,
        )

        order.set_position_size(float(position))
        order._order_ready = False  # ✅ ALWAYS False - pipeline will fetch premium
        
        # ✅ Store model and args for pipeline to use
        order._model = self
        order._args = {
            "action": action,
            "arcTick": arcTick,
            "position": position,
            "quantity": quantity,
            "trigger_price": trigger_price,
            "status_callback": status_callback,
            "type": type  # ✅ FIX: Include order type (LMT/MKT) from UI
        }

        cb = status_callback or self._status_callback
        if cb:
            try:
                order.set_status_callback(cb)
            except Exception as e:
                logging.error(f"Failed to attach status callback: {e}")

        # --- ALWAYS START WATCHING (even in premarket) ---
        # Don't queue in premarket - start watching immediately
        is_premarket = is_market_closed_or_pre_market()
        if is_premarket:
            status = f"AppModel[{self._symbol}]: Premarket → watching for trigger."
            logging.info(status)
            if cb:
                cb(status, "orange")
        
        # If trigger already met and RTH, fire immediately
        if not is_premarket and (not trigger_price or order.is_triggered(current_price)):
            attempt = 0
            while True:
                attempt += 1
                try:
                    if general_app.place_custom_order(order):
                        logging.info(
                            f"AppModel[{self._symbol}] Order transmitted after {attempt} attempts"
                        )
                        if cb:
                            cb(f"Order executed successfully (attempt {attempt})", "green")
                        break
                except Exception as e:
                    logging.error(f"AppModel[{self._symbol}] ERROR attempt {attempt}: {e}")

                logging.warning(f"AppModel[{self._symbol}] Retry placing order ({attempt})")
                if cb:
                    cb(f"Retrying to place order… ({attempt})", "orange")
                time.sleep(1)

            order.mark_active(
                result=f"IB Order ID: {getattr(order, '_ib_order_id', 'Unknown')}"
            )
            logging.info(f"AppModel[{self._symbol}] ACTIVE {order.order_id}")
        else:
            # Always add to watch service (premarket or RTH with pending trigger)
            general_app.add_order(order)
            logging.info(
                f"AppModel[{self._symbol}] Watching for trigger {order.order_id} (premarket={is_premarket})"
            )

        self._order = order
        return order.to_dict()

    def prepare_almost_option_order(
            self,
            action: str = "BUY",
            position: int = 2000,
            quantity: int = 1,
            trigger_price: Optional[float] = None,
            arcTick: float = 0.01,
            type: str = "LMT",
            status_callback=None) -> Order:
        logging.info(f"Preparing almost order ARC STYLE FOR {self.symbol}")
        order = Order(
            symbol=self._symbol,
            expiry=self._expiry,
            strike=self._strike,
            right=self._right,
            type=type,
            qty=quantity,
            entry_price=1 + arcTick,
            tp_price=self._take_profit,
            sl_price=self._stop_loss,
            action=action.upper(),
            trigger=trigger_price,
            appmodel=self
        )
        order._args = {"action": action,
                       "position":position,
                       "quantity": quantity,
                       "trigger_price": trigger_price,
                       "arcTick":arcTick,
                       "type": type,
                       "status_callback":status_callback}
        order.set_position_size(position)
        order.set_status_callback(status_callback)
        order._order_ready = False
        general_app.add_order(order)
        return order
        



    def get_available_strikes(self, expiry: str) -> List[float]:
        """
        Get available strikes for an expiry.
        Hybrid approach: Use cached strikes if available, then fetch from TWS for complete list.
        """
        try:
            # ✅ Step 1: Try to get cached strikes first (fast, 0ms)
            from Services.persistent_conid_storage import storage
            cached_data = storage.get_cached_option_conids(self._symbol, expiry)
            if cached_data:
                cached_strikes = sorted(set([float(row["strike"]) for row in cached_data]))
                logging.info(f"AppModel[{self._symbol}]: Found {len(cached_strikes)} cached strikes for {expiry}")
                
                # ✅ Step 2: Still fetch from TWS in background for complete/updated list
                def fetch_fresh():
                    try:
                        maturities = general_app.tws.get_maturities(self._symbol)
                        if maturities and expiry in maturities.get("expirations", []):
                            fresh_strikes = maturities.get("strikes", [])
                            # Merge cached + fresh (dedupe and sort)
                            all_strikes = sorted(set(cached_strikes + fresh_strikes))
                            logging.info(f"AppModel[{self._symbol}]: Merged {len(cached_strikes)} cached + {len(fresh_strikes)} fresh = {len(all_strikes)} total strikes")
                            return all_strikes
                    except Exception as e:
                        logging.warning(f"AppModel[{self._symbol}]: Failed to fetch fresh strikes, using cached: {e}")
                        return cached_strikes
                
                # Return cached immediately, fetch fresh in background
                import threading
                threading.Thread(target=fetch_fresh, daemon=True).start()
                return cached_strikes
            
            # ✅ Step 3: No cache - fetch from TWS (normal flow)
            maturities = general_app.tws.get_maturities(self._symbol)
            return (
                maturities["strikes"]
                if maturities and expiry in maturities["expirations"]
                else []
            )
        except Exception as e:
            logging.error(f"AppModel[{self._symbol}]: Failed to get strikes: {e}")
            return []

    def cancel_pending_order(self, order_id: str) -> bool:
        order = self._order
        if order and order.order_id == order_id:  # Check if order exists
            general_app.cancel_order(order_id)
            order.mark_cancelled()
            self._order = None  # Clear the reference so you can place new order
            logging.info(f"AppModel[{self._symbol}]: Order cancelled {order_id}")
            return True
        return False

    def get_order(self) -> Optional[Order]:
        return self._order

    def reset(self):
        self._underlying_price = None
        self._expiry = None
        self._strike = None
        self._right = None
        self._stop_loss = None
        self._take_profit = None
        self._order = None
        logging.info(f"AppModel[{self._symbol}]: State reset")

    def get_state(self):
        return {
            "symbol": self._symbol,
            "price": self._underlying_price,
            "expiry": self._expiry,
            "strike": self._strike,
            "right": self._right,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "orders": [o.to_dict() for o in self._orders],
        }
    
    def serialize(self) -> str:
        """
        Serialize model in at most two lines:
        Line 1: AppModel:<id>:<symbol>:<expiry>:<strike>:<right>:<stop_loss>:<take_profit>:<has_order>
        Line 2 (optional): serialized order if present
        """
        expiry = self._expiry or "None"
        strike = self._strike or "None"
        right = self._right or "None"
        sl = self._stop_loss if self._stop_loss is not None else "None"
        tp = self._take_profit if self._take_profit is not None else "None"

        has_order = bool(self._order)
        base_line = (
            f"AppModel:{self._id}:{self._symbol}:{expiry}:{strike}:{right}:{sl}:{tp}:{has_order}"
        )

        if has_order:
            return base_line + "\n" + self._order.serialize()
        else:
            return base_line

    @classmethod
    def deserialize(cls, lines: list[str]) -> "AppModel":
        """
        Deserialize model from one or two lines.
        The first line must start with 'AppModel:'.
        If 'has_order' is True, second line must start with 'Order:'.
        Returns the new model and the number of lines consumed.
        """
        header = lines[0].split(":")
        if len(header) < 9 or header[0] != "AppModel":
            raise ValueError("Invalid AppModel serialization")

        _, mid, symbol, expiry, strike, right, sl, tp, has_order = header
        model = cls(symbol)
        model._id = mid
        model._expiry = None if expiry == "None" else expiry
        model._strike = None if strike == "None" else float(strike)
        model._right = None if right == "None" else right
        model._stop_loss = None if sl == "None" else float(sl)
        model._take_profit = None if tp == "None" else float(tp)

        has_order = has_order == "True"
        consumed = 1

        if has_order:
            if len(lines) < 2 or not lines[1].startswith("Order:"):
                raise ValueError("Missing Order line after AppModel")
            model._order = Order.deserialize(lines[1])
            consumed = 2

        return model, consumed



# --- Per-symbol model registry ---
_models: Dict[str, AppModel] = {}


def get_model(symbol: str) -> AppModel:
    s = symbol.upper()
    if s not in _models:
        _models[s] = AppModel(s)
        general_app.add_model(_models[s])
    return _models[s]