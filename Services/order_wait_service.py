import threading
import time
import logging
import gc
from Helpers.Order import Order, OrderState
from Services.order_manager import order_manager
from Services.watcher_info import (
    ThreadInfo, watcher_info,
    STATUS_PENDING, STATUS_RUNNING, STATUS_FINALIZED, STATUS_CANCELLED, STATUS_FAILED
)
from Services.runtime_manager import runtime_man
from Services.tws_service import create_tws_service, TWSService
from Services.polygon_service import polygon_service, PolygonService
from Services.amo_service import amo, LOSS
from Services.nasdaq_info import is_market_closed_or_pre_market

class OrderWaitService:
    def __init__(self, polygon_service: PolygonService, tws_service: TWSService, poll_interval=0.1):
        self.polygon = polygon_service
        self.tws = tws_service
        self.trigger_lock = threading.Lock()
        self.trigger_status = set()
        # Active pending orders, keyed by order_id
        self.pending_orders = {}
        # 💡 NEW: Storage for active stop-loss orders being monitored by WS
        self.active_stop_losses = {}
        # Cancelled order IDs
        self.cancelled_orders = set()
        # Lock for thread-safety
        self.lock = threading.Lock()
        self._arclock = threading.Lock()
        self._stoplosses = dict()
        # Storage for WS callbacks to allow proper unsubscription
        self._ws_callbacks = {} # Dictionary to store {order_id: callback_function}

        # Polling interval for alternate mode (seconds), optimized from 0.1s to 0.5s
        self.poll_interval = poll_interval
        amo.register(LOSS, self.set_stop_loss)
        amo.seal()

    
    def set_stop_loss(self, order: Order, stop_loss_price: float):
        with self._arclock:
            self._stoplosses[order.order_id] =stop_loss_price

    def _poll_snapshot_thread(self, order_id: str, order: Order, tinfo: ThreadInfo):
        """
        Inner polling thread logic for trigger watcher (formerly _poll_snapshot).
        Monitors a stock snapshot until the trigger condition is met or the order is cancelled.
        """
        delay = 5  # Increased from 2 to 5 seconds for status logging
        last = 0

        logging.info(f"[WaitService] Snapshot thread started | order_id={order_id} | symbol={order.symbol}")

        try:
            while runtime_man.is_run() and order.state == OrderState.PENDING:
                logging.debug(f"[WaitService] Loop tick | order_id={order_id} | state={order.state}")
                
                # Order preparation check - should ideally be done before watcher starts
                if not order._order_ready:
                    logging.warning(f"[WaitService] Order not ready, preparing | order_id={order_id}")
                    model = order._model
                    _args = order._args
                    _order = model.prepare_option_order(action= _args["action"]
                                                        ,position=_args["position"]
                                                        ,quantity=_args["quantity"]
                                                        ,trigger_price=_args["trigger_price"]
                                                        ,arcTick=_args["arcTick"]
                                                        ,type="LMT"
                                                        ,status_callback=_args["status_callback"])
                    order = _order
                    logging.info(f"[WaitService] Order prepared in watcher | order_id={order_id}")

                # Consolidated lock check
                with self.lock:
                    if order_id not in self.pending_orders:
                        logging.info(f"[WaitService] Order missing from pending_orders | order_id={order_id}")
                        watcher_info.remove(order_id)
                        return

                    if order_id in self.cancelled_orders:
                        logging.info(f"[WaitService] Order cancelled | order_id={order_id}")
                        watcher_info.remove(order_id)
                        return

                logging.debug(f"[WaitService] Fetching snapshot | symbol={order.symbol}")
                snap = self.polygon.get_snapshot(order.symbol)

                if not snap:
                    logging.debug(f"[WaitService] Empty snapshot | symbol={order.symbol}")
                    time.sleep(self.poll_interval)
                    continue

                last_price = snap.get("last")
                now = time.time()

                logging.debug(
                    f"[WaitService] Snapshot | symbol={order.symbol} | price={last_price}"
                )

                # Periodic status logging (reduced frequency)
                if now - last > delay:
                    logging.info(
                        f"[WaitService] Monitoring {order.symbol} | price={last_price} | trigger={order.trigger}"
                    )
                    last = now

                if last_price:
                    tinfo.update_status(STATUS_RUNNING, last_price=last_price)

                if last_price and order.is_triggered(last_price):
                    logging.info(
                        f"[WaitService] 🎯 TRIGGER MET | order_id={order_id} | price={last_price} | trigger={order.trigger}"
                    )

                    # Check if premarket - if so, prompt rebase/cancel instead of firing
                    if is_market_closed_or_pre_market():
                        logging.info(
                            f"[WaitService] Premarket trigger hit - prompting rebase/cancel | order_id={order_id}"
                        )
                        self._handle_premarket_trigger(order_id, order, tinfo, last_price)
                        # Continue watching (don't return)
                        # Remove from trigger_status so it can trigger again after rebase
                        with self.trigger_lock:
                            if order in self.trigger_status:
                                self.trigger_status.remove(order)
                        continue
                    else:
                        # RTH - fire the order
                        self._finalize_order(order_id, order, tinfo, last_price)

                        with self.lock:
                            if order_id in self.pending_orders:
                                del self.pending_orders[order_id]

                        logging.info(f"[WaitService] Watcher completed | order_id={order_id}")
                        return

                time.sleep(self.poll_interval)

        except Exception as e:
            logging.error(
                f"[WaitService] Exception in snapshot thread | order_id={order_id} | error={str(e)}"
            )
            tinfo.update_status(STATUS_FAILED, info={"error": str(e)})


    def start_trigger_watcher(self, order: Order, mode: str = "ws") -> threading.Thread:
        """
        Start a dedicated thread (or ws subscription) to watch trigger price for an order.
        When trigger condition is met, finalize order via TWS.
        """
        order_id = order.order_id

        # Register ThreadInfo
        tinfo = ThreadInfo(order_id, order.symbol, watcher_type="trigger", stop_loss=order.sl_price, mode=mode,order=order)
        watcher_info.add_watcher(tinfo)
        tinfo.update_status(STATUS_RUNNING)

        if mode == "ws":
            # Define the callback function and store it for unsubscription
            callback_func = lambda price, oid=order_id: self._on_tick(oid, price)
            self._ws_callbacks[order_id] = callback_func # Store it

            self.polygon.subscribe(
                order.symbol,
                callback_func # Pass the stored function
            )
            logging.info(f"[TriggerWatcher] Started WS watcher for {order.symbol} (order {order_id}) - WS mode.")
            return None  # no thread object for ws

        elif mode == "poll":
            # Old-school polling thread path
            t = threading.Thread(
                target=self._poll_snapshot_thread, 
                args=(order_id, order, tinfo),
                daemon=True
            )
            t.start()
            logging.info(f"[TriggerWatcher] Started polling watcher for {order.symbol} (order {order_id}) - Poll mode.")
            return t

        else:
            logging.warning(f"[TriggerWatcher] Unknown mode '{mode}', defaulting to 'ws'")
            # Fallback path must also store the callback
            callback_func = lambda price, oid=order_id: self._on_tick(oid, price)
            self._ws_callbacks[order_id] = callback_func
            self.polygon.subscribe(
                order.symbol,
                callback_func
            )
            return None

    def _finalize_exit_order(self, exit_order: Order, tinfo: ThreadInfo, last_price: float, live_qty: int, contract):
        """
        Sends the stop-loss order to TWS and handles cleanup and status updates.
        This is the consolidated logic from the old _stop_loss_thread.
        """
        order_id = exit_order.order_id
        
        try:
            logging.info(f"[StopLoss] Submitting MKT exit order for {exit_order.symbol} at {last_price}...")
            
            success = self.tws.sell_position_by_order_id(
                exit_order.previous_id,
                contract,
                qty=live_qty,
                limit_price=None,      # market order
                ex_order=exit_order
            )

            if success:
                logging.info(
                    f"[StopLoss] Sold {live_qty} {exit_order.symbol} "
                    f"via TWS position map – MKT Exit. Watcher finalized."
                )
                # ✅ RE-ADDED CRITICAL FINALIZATION LOGIC
                exit_order.mark_finalized(f"Stop-loss triggered @ {last_price}") 
                if tinfo: # tinfo might be None if WS mode lookup failed
                    tinfo.update_status(STATUS_FINALIZED, last_price=last_price)
                return True
            else:
                logging.error(f"[StopLoss] TWS refused stop-loss sell for {order_id} – will retry/fail.")
                # We return False so the while loop in _stop_loss_thread can decide to retry or fail.
                return False 

        except Exception as e:
            logging.exception(f"[StopLoss] Exception in finalize exit for {order_id}: {e}")
            exit_order.mark_failed(str(e))
            if tinfo:
                tinfo.update_status(STATUS_FAILED, last_price=last_price, info={"error": str(e)})
            return False


    def start_stop_loss_watcher(self, order: Order, stop_loss_price: float, mode: str = "ws"):
        """
        💡 MODIFIED
        Start a dedicated watcher to monitor stop-loss for an active order.
        Supports "poll" (thread) and "ws" (event-driven) modes.
        """
        order_id = order.order_id

        tinfo = ThreadInfo(order_id, order.symbol,
                         watcher_type="stop_loss",
                         mode=mode, # 💡 Pass mode to info
                         stop_loss=stop_loss_price)
        self.set_stop_loss(order, stop_loss_price)
        watcher_info.add_watcher(tinfo)
        tinfo.update_status(STATUS_RUNNING)

        # 💡 Route to the correct handler based on mode
        if mode == "ws":
            # --- WebSocket Mode (Event-Driven) ---
            with self.lock:
                self.active_stop_losses[order_id] = order # Store the order for the callback
            
            # Define the callback, baking in the order_id and stop_loss_level
            callback_func = lambda price, oid=order_id, sl=stop_loss_price: \
                                 self._on_stop_loss_tick(oid, price, sl)
            
            self._ws_callbacks[order_id] = callback_func # Store for unsubscription
            
            self.polygon.subscribe(order.symbol, callback_func)
            logging.info(f"[StopLoss-WS] Started WS watcher for {order.symbol} (order {order_id})")
            return None # No thread object

        else:
            # --- Polling Mode (Thread-Based) ---
            if mode != "poll":
                logging.warning(f"[StopLoss] Unknown mode '{mode}' for {order_id}. Defaulting to 'poll'.")
            
            # 💡 Renamed target method
            t = threading.Thread(
                target=self._run_stop_loss_watcher_poll_thread, 
                args=(order, stop_loss_price, tinfo), # Pass necessary variables
                daemon=True,
                name=f"StopLoss-{order.symbol}-{order_id[:4]}-poll"
            )
            t.start()
            logging.info(f"[StopLoss] Started sl watcher for {order.symbol} (order {order_id})")
            return t

    def _run_stop_loss_watcher_poll_thread(self, order: Order, stop_loss_price: float, tinfo: ThreadInfo):
        """
        💡 RENAMED (was _run_stop_loss_watcher_thread)
        THREAD TARGET (POLL): Monitors a single position's stop-loss using polling.
        """
        # --- This is the body of the old inner _stop_loss_thread ---
        last_print   = 0
        delay        = 10  # Increased from 5 to 10 seconds for status logging
        warn_times   = {"contract": 0, "premium": 0, "position": 0}
        
        # Cache for contract ID to avoid repeated resolution
        cached_conid = None

        logging.info(
            f"[StopLoss-POLL] Watching {order.symbol} stop-loss @ {stop_loss_price}  ({order.right})")
        try:

            while runtime_man.is_run():
                # 1. Check order state and cancellation (consolidated lock)
                with self._arclock:
                    sl = self._stoplosses.get(order.order_id)
                    if sl is None:
                        logging.warning(f"[StopLoss-POLL] Stop-loss removed externally | order_id={order.order_id}")
                        tinfo.update_status(STATUS_CANCELLED)
                        return
                
                if order.state not in (OrderState.ACTIVE, OrderState.PENDING):
                    logging.info(
                        f"[StopLoss-POLL] Order {order.order_id} no longer active – stopping watcher.")
                    tinfo.update_status(STATUS_CANCELLED)
                    return
                
                # 💡 Check for external cancellation
                with self.lock:
                    if order.order_id in self.cancelled_orders:
                        logging.info(f"[StopLoss-POLL] Watcher {order.order_id} cancelled by service.")
                        tinfo.update_status(STATUS_CANCELLED)
                        return

                # 2. Fetch market data
                snap = self.polygon.get_snapshot(order.symbol)
                if not snap:
                    logging.debug(f"[StopLoss-POLL] No snapshot for {order.symbol}")
                    time.sleep(self.poll_interval)
                    continue

                now = time.time()
                last_price = snap.get("last")
                
                # Periodic status logging (reduced frequency)
                if last_price and now - last_print >= delay:
                    logging.info(
                        f"[StopLoss-POLL] Monitoring {order.symbol} → {last_price}, stop={sl}")
                    last_print = now
                
                if last_price:
                    tinfo.update_status(STATUS_RUNNING, last_price=last_price)

                # 3. Check trigger logic
                triggered = (last_price >= sl) if order.right in ("P", "PUT") \
                        else (last_price <= sl)

                # 4. Contract resolution (with caching and improved throttling)
                if cached_conid is None:
                    contract = self.tws.create_option_contract(
                        order.symbol, order.expiry, order.strike, order.right)
                    conid = self.tws.resolve_conid(contract)
                    
                    if not conid:
                        if now - warn_times["contract"] >= 30:
                            logging.warning(
                                f"[StopLoss-POLL] No conId for {order.symbol} "
                                f"{order.expiry} {order.strike}{order.right} – retrying")
                            warn_times["contract"] = now
                        time.sleep(self.poll_interval)
                        continue
                    
                    cached_conid = conid
                    contract.conId = conid
                    logging.debug(f"[StopLoss-POLL] Cached conId {conid} for {order.symbol}")
                else:
                    contract = self.tws.create_option_contract(
                        order.symbol, order.expiry, order.strike, order.right)
                    contract.conId = cached_conid

                # 5. Premium fetch (with improved throttling)
                premium = self.tws.get_option_premium(
                    order.symbol, order.expiry, order.strike, order.right)
                
                if premium is None or premium <= 0:
                    pos_fallback = self.tws.get_position_by_order_id(order.previous_id)
                    premium = pos_fallback and pos_fallback.get("avg_price") or None
                    
                    if premium is None:
                        if now - warn_times["premium"] >= 30:
                            logging.warning(
                                f"[StopLoss-POLL] No premium for {order.symbol} "
                                f"{order.expiry} {order.strike}{order.right} – retrying")
                            warn_times["premium"] = now
                        time.sleep(self.poll_interval)
                        continue

                # 6. Position check (with improved throttling)
                pos = self.tws.get_position_by_order_id(order.previous_id)
                if not pos or pos.get("qty", 0) <= 0:
                    if now - warn_times["position"] >= 30:
                        logging.warning(
                            f"[StopLoss-POLL] No live position for {order.previous_id} – will keep watching")
                        warn_times["position"] = now
                    time.sleep(self.poll_interval)
                    continue

                # 7. Exit when triggered
                if triggered:
                    logging.info(
                        f"[StopLoss-POLL] 🚨 TRIGGERED! {order.symbol} "
                        f"Price {last_price} vs Stop {stop_loss_price}"
                    )
                    live_qty = int(pos["qty"])
                    
                    success = self._finalize_exit_order(order, tinfo, last_price, live_qty, contract)

                    if success:
                        return
                
                time.sleep(self.poll_interval)

        except Exception as e:
            logging.exception(f"[StopLoss-POLL] Outer exception in stop-loss watcher: {e}")
            tinfo.update_status(STATUS_FAILED, info={"error": str(e)})
        finally:
            watcher_info.remove(order.order_id) # Cleanup watcher info on thread exit

    def add_order(self, order: Order, mode: str = "ws") -> str:
        """
        Add an order to be executed once its trigger is met.
        mode="ws"   -> subscribe to live ticks (original behavior)
        mode="poll" -> start a polling thread using snapshot
        """
        order_id = order.order_id
        with self.lock:
            self.pending_orders[order_id] = order

        # ✅ IMMEDIATE TRIGGER CHECK
        current_price = self.polygon.get_last_trade(order.symbol)
        if current_price and order.is_triggered(current_price):
            logging.info(
                f"[WaitService] 🚨 TRIGGER ALREADY MET! Executing immediately. "
                f"Current: {current_price}, Trigger: {order.trigger}"
            )
            self._finalize_order(order_id, order, tinfo=None, last_price=current_price)
            return order_id

        # Subscribe / start poller only if trigger not already met
        self.start_trigger_watcher(order, mode) # 💡 Simplified to use the router

        msg = (
            f"[WaitService] Order added {order_id} "
            f"(mode={mode}, waiting for trigger {order.trigger}, current: {current_price})"
        )
        logging.info(msg)
        return order_id

    def cancel_order(self, order_id: str):
        """
        💡 MODIFIED
        Cancel an order. 
        Removes it from pending trigger set OR active stop-loss set.
        Unsubscribes from Polygon.
        """
        order = None
        symbol = None
        
        with self.lock:
            if order_id in self.pending_orders:
                order = self.pending_orders.pop(order_id, None)
                if order:
                    logging.info(f"[WaitService] Cancelling PENDING order {order_id}")
                    symbol = order.symbol
            
            elif order_id in self.active_stop_losses: # 💡 NEW: Check active stop-losses
                order = self.active_stop_losses.pop(order_id, None)
                if order:
                    logging.info(f"[WaitService] Cancelling STOP-LOSS watcher {order_id}")
                    symbol = order.symbol

            if order:
                order.mark_cancelled()
                self.cancelled_orders.add(order_id)
                watcher_info.update_watcher(order_id, STATUS_CANCELLED)
            else:
                logging.warning(f"[WaitService] cancel_order: No active watcher found for {order_id}")
                return # Not found, nothing to do

        # Unsubscribe logic (outside lock)
        callback_func = self._ws_callbacks.pop(order_id, None)
        if callback_func and symbol:
            try:
                self.polygon.unsubscribe(symbol, callback_func)
            except Exception as e:
                logging.debug(f"[WaitService] Unsubscribe ignored for {symbol}: {e}")
        elif not callback_func:
            logging.debug(f"[WaitService] No WS callback found for order {order_id} (likely poll mode).")

    def list_pending_orders(self):
        with self.lock:
            return [o.to_dict() for o in self.pending_orders.values()]

    def _on_tick(self, order_id: str, price: float):
        """Callback from PolygonService for live ENTRY triggers."""
        with self.lock:
            order = self.pending_orders.get(order_id)
            if not order or order_id in self.cancelled_orders:
                return # Order was finalized or cancelled

        # Update watcher info with live price
        if tinfo := watcher_info.get_watcher(order_id):
            tinfo.update_status(STATUS_RUNNING, last_price=price)

        with self.trigger_lock:
            if order.is_triggered(price) and  order not in self.trigger_status:
                self.trigger_status.add(order)
                logging.info(f"[WaitService-WS] TRIGGERED! {order.symbol} @ {price}, trigger={order.trigger}")
                
                # Check if premarket - if so, prompt rebase/cancel instead of firing
                if is_market_closed_or_pre_market():
                    logging.info(
                        f"[WaitService-WS] Premarket trigger hit - prompting rebase/cancel | order_id={order_id}"
                    )
                    self._handle_premarket_trigger(order_id, order, tinfo, price)
                    # Continue watching (don't remove from pending_orders)
                    # Remove from trigger_status so it can trigger again after rebase
                    self.trigger_status.remove(order)
                    return
                else:
                    # RTH - fire the order
                    self._finalize_order(order_id, order, tinfo=None, last_price=price)
                    
                    # --- Unsubscribe and cleanup ---
                    callback_func = self._ws_callbacks.pop(order_id, None)
                    if callback_func:
                        try:
                            self.polygon.unsubscribe(order.symbol, callback_func)
                        except Exception as e:
                            logging.debug(f"[WaitService-WS] Unsubscribe ignored for {order.symbol}: {e}")

                    with self.lock:
                        self.pending_orders.pop(order_id, None) # Remove from pending
                        self.cancelled_orders.add(order_id) # Add to prevent race conditions

    def _on_stop_loss_tick(self, order_id: str, price: float, stop_loss_level: float):
        """💡 NEW: Callback from PolygonService for live STOP-LOSS triggers."""
        
        # 1. Get order from the active stop-loss dictionary
        with self.lock:
            order = self.active_stop_losses.get(order_id)
            if not order or order_id in self.cancelled_orders:
                return # Watcher was cancelled or already triggered
        
        tinfo = watcher_info.get_watcher(order_id) # Get tinfo for status updates

        # 2. Check trigger logic
        triggered = (price >= stop_loss_level) if order.right in ("P", "PUT") \
                    else (price <= stop_loss_level)
        
        if tinfo:
            tinfo.update_status(STATUS_RUNNING, last_price=price)

        if triggered:
            logging.info(
                f"[StopLoss-WS] TRIGGERED! {order.symbol} "
                f"Price {price} vs Stop {stop_loss_level}"
            )
            
            # --- Triggered: Execute TWS-heavy logic NOW ---
            contract = self.tws.create_option_contract(
                order.symbol, order.expiry, order.strike, order.right)
            conid = self.tws.resolve_conid(contract)
            
            if not conid:
                logging.error(f"[StopLoss-WS] Triggered, but FAILED to resolve conid for {order_id}. Retrying next tick.")
                if tinfo: tinfo.update_status(STATUS_FAILED, info={"error": "Failed to resolve conId"})
                return # Will retry on next tick if still triggered
            
            contract.conId = conid

            pos = self.tws.get_position_by_order_id(order.previous_id)
            if not pos or pos.get("qty", 0) <= 0:
                logging.warning(f"[StopLoss-WS] Triggered, but no position found for {order.previous_id}. Closing watcher.")
                self._cleanup_ws_watcher(order_id, order.symbol) # Position is gone, close watcher
                if tinfo: tinfo.update_status(STATUS_FINALIZED, info={"message": "Position not found"})
                return

            live_qty = int(pos["qty"])

            # 4. Finalize
            success = self._finalize_exit_order(order, tinfo, price, live_qty, contract)

            if success:
                # 5. Unsubscribe and cleanup
                self._cleanup_ws_watcher(order_id, order.symbol)
        
        # --- Not triggered: Do nothing, wait for next tick ---

    def _cleanup_ws_watcher(self, order_id: str, symbol: str):
        """💡 NEW: Helper to remove WS callback and order from active monitoring."""
        with self.lock:
            self.active_stop_losses.pop(order_id, None)
            self.cancelled_orders.add(order_id) # Add to prevent race conditions

        callback_func = self._ws_callbacks.pop(order_id, None)
        if callback_func:
            try:
                self.polygon.unsubscribe(symbol, callback_func)
            except Exception as e:
                logging.error(f"[WaitService] WS unsubscribe failed for {order_id}: {e}")

  
    def _handle_premarket_trigger(self, order_id: str, order: Order, tinfo: ThreadInfo, last_price: float):
        """
        Handle trigger hit during premarket.
        Shows popup dialog prompting user to rebase or cancel.
        When rebasing, updates trigger to new premarket extreme AND strike to ATM (closest to trigger).
        - CALLS: closest strike >= trigger (upside)
        - PUTS: closest strike <= trigger (downside)
        - NO AUTO-REBASE: User must choose. Timeout = cancel order.
        """
        cb = getattr(order, "_status_callback", None)
        
        # Get the model to access rebase functionality
        model = getattr(order, "_model", None)
        if not model:
            # Try to find model from general_app
            from model import general_app
            models = general_app.get_models()
            for m in models:
                if m.symbol == order.symbol and m.order and m.order.order_id == order_id:
                    model = m
                    break
        
        if not model:
            logging.warning(
                f"[WaitService] Premarket trigger hit but no model found | order_id={order_id}"
            )
            if cb:
                cb(
                    f"⚠️ Premarket trigger hit @ {last_price:.2f} - Order watching continues.",
                    "orange"
                )
            if tinfo:
                tinfo.update_status(STATUS_RUNNING, last_price=last_price)
            return
        
        # Get latest premarket extreme and calculate new trigger
        if not is_market_closed_or_pre_market():
            logging.warning(f"[WaitService] _handle_premarket_trigger called outside premarket")
            return
        
        from model import general_app
        market_data = general_app.get_market_data_for_trigger(order.symbol, 'premarket')
        if not market_data:
            logging.warning(
                f"[WaitService] Premarket trigger hit but no market data | order_id={order_id}"
            )
            if cb:
                cb(
                    f"⚠️ Premarket trigger hit @ {last_price:.2f} - Rebase failed. Order watching continues.",
                    "orange"
                )
            if tinfo:
                tinfo.update_status(STATUS_RUNNING, last_price=last_price)
            return
        
        new_trigger = (
            market_data["high"] if order.right in ("C", "CALL")
            else market_data["low"]
        )
        
        if not new_trigger or new_trigger <= 0:
            logging.warning(
                f"[WaitService] Premarket trigger hit but invalid market data | order_id={order_id}"
            )
            if cb:
                cb(
                    f"⚠️ Premarket trigger hit @ {last_price:.2f} - Rebase failed. Order watching continues.",
                    "orange"
                )
            if tinfo:
                tinfo.update_status(STATUS_RUNNING, last_price=last_price)
            return
        
        # ✅ Calculate ATM strike based on trigger price (not current price)
        # For CALLS: closest strike >= trigger (upside)
        # For PUTS: closest strike <= trigger (downside)
        atm_strike = None
        try:
            if model._expiry:
                available_strikes = model.get_available_strikes(model._expiry)
                if available_strikes:
                    if order.right in ("C", "CALL"):
                        # CALL: Find closest strike >= trigger (upside)
                        eligible_strikes = [s for s in available_strikes if s >= new_trigger]
                        if eligible_strikes:
                            atm_strike = min(eligible_strikes, key=lambda s: abs(s - new_trigger))
                            logging.info(
                                f"[WaitService] CALL: Calculated ATM strike: {atm_strike} "
                                f"(trigger={new_trigger:.2f}, eligible strikes >= trigger: {len(eligible_strikes)})"
                            )
                        else:
                            # Fallback: if no strike >= trigger, use closest overall
                            atm_strike = min(available_strikes, key=lambda s: abs(s - new_trigger))
                            logging.warning(
                                f"[WaitService] CALL: No strike >= trigger, using closest: {atm_strike}"
                            )
                    else:
                        # PUT: Find closest strike <= trigger (downside)
                        eligible_strikes = [s for s in available_strikes if s <= new_trigger]
                        if eligible_strikes:
                            atm_strike = max(eligible_strikes, key=lambda s: abs(s - new_trigger))
                            logging.info(
                                f"[WaitService] PUT: Calculated ATM strike: {atm_strike} "
                                f"(trigger={new_trigger:.2f}, eligible strikes <= trigger: {len(eligible_strikes)})"
                            )
                        else:
                            # Fallback: if no strike <= trigger, use closest overall
                            atm_strike = min(available_strikes, key=lambda s: abs(s - new_trigger))
                            logging.warning(
                                f"[WaitService] PUT: No strike <= trigger, using closest: {atm_strike}"
                            )
                else:
                    logging.warning(
                        f"[WaitService] No available strikes for expiry {model._expiry} - keeping original strike"
                    )
            else:
                logging.warning(
                    f"[WaitService] No expiry set in model - cannot calculate ATM strike"
                )
        except Exception as e:
            logging.error(f"[WaitService] Error calculating ATM strike: {e}", exc_info=True)
        
        # ✅ Show popup dialog to user (NO AUTO-REBASE)
        import tkinter as tk
        from tkinter import ttk
        
        user_choice = None  # "rebase" or "cancel"
        choice_event = threading.Event()
        
        # Find the OrderFrame widget that has this model
        widget = None
        try:
            import main
            app = None
            # Try to get the main app instance
            logging.info(f"[WaitService] Searching for ArcTriggerApp instance...")
            for obj in gc.get_objects():
                if isinstance(obj, main.ArcTriggerApp):
                    app = obj
                    logging.info(f"[WaitService] ✅ Found ArcTriggerApp instance: {app}")
                    break
            
            if app:
                logging.info(f"[WaitService] Checking {len(app.order_frames)} order frames for matching model")
                # Find OrderFrame with matching model
                for idx, frame in enumerate(app.order_frames):
                    if hasattr(frame, 'model'):
                        if frame.model == model:
                            widget = frame
                            logging.info(f"[WaitService] ✅ Found matching widget at index {idx} for model {model.symbol}")
                            break
                        else:
                            logging.debug(f"[WaitService] Frame {idx}: model={getattr(frame.model, 'symbol', 'None')}, target={model.symbol}")
                    else:
                        logging.debug(f"[WaitService] Frame {idx}: no model attribute")
                
                if not widget:
                    logging.warning(f"[WaitService] ❌ No matching widget found in {len(app.order_frames)} frames for model {model.symbol}")
            else:
                logging.warning(f"[WaitService] ❌ No ArcTriggerApp instance found in gc.get_objects()")
        except Exception as e:
            logging.error(f"[WaitService] ❌ Exception finding widget for popup: {e}", exc_info=True)
        
        if not widget:
            # ✅ NO AUTO-REBASE: If widget not found, cancel the order
            logging.error(
                f"[WaitService] Widget not found - cancelling order (no auto-rebase) | order_id={order_id}"
            )
            self.cancel_order(order_id)
            if cb:
                cb(
                    f"⚠️ Premarket trigger hit @ {last_price:.2f} - Order cancelled (UI unavailable)",
                    "red"
                )
            if tinfo:
                tinfo.update_status(STATUS_CANCELLED, last_price=last_price)
            return
        
        # Show popup dialog in UI thread
        def show_popup():
            try:
                popup = tk.Toplevel(widget)
                popup.title("⚠️ Premarket Trigger Hit")
                popup.geometry("450x220")
                popup.configure(bg="#222")
                popup.grab_set()  # Make it modal
                popup.transient(widget.winfo_toplevel())  # Keep on top
                
                # Center the popup
                popup.update_idletasks()
                x = (popup.winfo_screenwidth() // 2) - (popup.winfo_width() // 2)
                y = (popup.winfo_screenheight() // 2) - (popup.winfo_height() // 2)
                popup.geometry(f"+{x}+{y}")
                
                # Title
                ttk.Label(
                    popup,
                    text=f"Premarket Trigger Hit: {order.symbol}",
                    font=("Arial", 12, "bold"),
                    background="#222",
                    foreground="white"
                ).pack(pady=(15, 5))
                
                # Details
                strike_info = f"New Strike: ${atm_strike:.2f} (ATM)" if atm_strike else "Strike: (unchanged)"
                details_text = (
                    f"Current Price: ${last_price:.2f}\n"
                    f"Old Trigger: ${order.trigger:.2f} → New: ${new_trigger:.2f}\n"
                    f"Old Strike: ${order.strike:.2f} → {strike_info}"
                )
                ttk.Label(
                    popup,
                    text=details_text,
                    font=("Arial", 9),
                    background="#222",
                    foreground="#ccc",
                    justify="left"
                ).pack(pady=(5, 15))
                
                # Buttons
                btn_frame = ttk.Frame(popup)
                btn_frame.pack(pady=10)
                
                def choose_rebase():
                    nonlocal user_choice
                    user_choice = "rebase"
                    popup.destroy()
                    choice_event.set()
                
                def choose_cancel():
                    nonlocal user_choice
                    user_choice = "cancel"
                    popup.destroy()
                    choice_event.set()
                
                ttk.Button(
                    btn_frame,
                    text="Rebase to New Trigger + ATM",
                    command=choose_rebase,
                    width=20
                ).pack(side="left", padx=10)
                
                ttk.Button(
                    btn_frame,
                    text="Cancel Order",
                    command=choose_cancel,
                    width=18
                ).pack(side="left", padx=10)
                
            except Exception as e:
                logging.error(f"[WaitService] Popup error: {e}", exc_info=True)
                # ✅ NO AUTO-REBASE: On error, cancel the order
                user_choice = "cancel"
                choice_event.set()
        
        # Schedule popup in UI thread
        widget.after(0, show_popup)
        
        # Wait for user response (with 60 second timeout → CANCEL if timeout)
        timeout_occurred = not choice_event.wait(timeout=60.0)
        
        # Handle user choice
        if timeout_occurred:
            # ✅ Timeout = cancel order (NO AUTO-REBASE)
            logging.warning(
                f"[WaitService] Premarket trigger popup timeout (60s) - cancelling order | order_id={order_id}"
            )
            self.cancel_order(order_id)
            if cb:
                cb(
                    f"Premarket trigger hit @ {last_price:.2f} - Order cancelled (timeout)",
                    "orange"
                )
            if tinfo:
                tinfo.update_status(STATUS_CANCELLED, last_price=last_price)
        elif user_choice == "rebase":
            old_trigger = order.trigger
            old_strike = order.strike
            order.trigger = new_trigger
            if atm_strike:
                order.strike = atm_strike
                model._strike = atm_strike
                logging.info(
                    f"[WaitService] User chose to rebase premarket trigger + ATM strike | order_id={order_id} | "
                    f"old_trigger={old_trigger} | new_trigger={new_trigger} | "
                    f"old_strike={old_strike} | new_strike={atm_strike} (ATM)"
                )
            else:
                logging.info(
                    f"[WaitService] User chose to rebase premarket trigger (strike unchanged) | order_id={order_id} | "
                    f"old_trigger={old_trigger} | new_trigger={new_trigger} | strike={order.strike}"
                )
            if cb:
                strike_msg = f", strike → {atm_strike:.2f} (ATM)" if atm_strike else ""
                cb(
                    f"Premarket trigger hit @ {last_price:.2f} - Rebased to {new_trigger:.2f}{strike_msg}",
                    "blue"
                )
            if tinfo:
                tinfo.update_status(STATUS_RUNNING, last_price=last_price)
        elif user_choice == "cancel":
            # Cancel the order
            self.cancel_order(order_id)
            logging.info(
                f"[WaitService] User chose to cancel order after premarket trigger | order_id={order_id}"
            )
            if cb:
                cb(
                    f"Premarket trigger hit @ {last_price:.2f} - Order cancelled by user",
                    "orange"
                )
            if tinfo:
                tinfo.update_status(STATUS_CANCELLED, last_price=last_price)
        else:
            # Should not happen, but just in case
            logging.error(
                f"[WaitService] Unknown user choice: {user_choice} - cancelling order | order_id={order_id}"
            )
            self.cancel_order(order_id)
            if cb:
                cb(
                    f"Premarket trigger hit @ {last_price:.2f} - Order cancelled (unknown state)",
                    "red"
                )
            if tinfo:
                tinfo.update_status(STATUS_CANCELLED, last_price=last_price)

    def _finalize_order(self, order_id: str, order: Order, tinfo: ThreadInfo, last_price):
        """Sends the entry order to TWS and handles cleanup and status updates."""
        
        # Ensure we're in RTH (shouldn't be called in premarket, but double-check)
        if is_market_closed_or_pre_market():
            logging.error(
                f"[WaitService] _finalize_order called in premarket - this should not happen! | order_id={order_id}"
            )
            return
        
        # If order not ready, prepare it now (fetch price, calculate quantity)
        if not getattr(order, "_order_ready", False):
            logging.info(f"[WaitService] Order not ready - preparing now | order_id={order_id}")
            model = getattr(order, "_model", None)
            if model and hasattr(order, "_args"):
                try:
                    _args = order._args
                    prepared_order = model.prepare_option_order(
                        action=_args.get("action", "BUY"),
                        position=_args.get("position", 2000),
                        quantity=_args.get("quantity", 1),
                        trigger_price=_args.get("trigger_price"),
                        arcTick=_args.get("arcTick", 0.01),
                        type=_args.get("type", "LMT"),
                        status_callback=_args.get("status_callback")
                    )
                    # Update order with prepared values
                    order.entry_price = prepared_order.entry_price
                    order.qty = prepared_order.qty
                    order._order_ready = True
                    logging.info(
                        f"[WaitService] Order prepared | order_id={order_id} | "
                        f"price={order.entry_price} | qty={order.qty}"
                    )
                except Exception as e:
                    logging.error(f"[WaitService] Failed to prepare order: {e}")
                    order.mark_failed(f"Preparation failed: {e}")
                    return
        
        # Helper to update tinfo if it exists (for poll mode)
        def _update_tinfo_status(status, **kwargs):
            # 💡 MODIFIED: Find the watcher info, whether from poll thread (tinfo) or WS (lookup)
            active_tinfo = tinfo or watcher_info.get_watcher(order_id)
            if active_tinfo:
                active_tinfo.update_status(status, last_price=last_price, **kwargs)


        try:
            start_ts = time.time() * 1000
            logging.info(f"[TWS-LATENCY] {order.symbol} Trigger hit → sending ENTRY order "
                        f"({order.right}{order.strike}) at {start_ts:.0f} ms")
            success = self.tws.place_custom_order(order)
            if success:
                end_ts = time.time() * 1000
                latency = end_ts - start_ts
                logging.info(f"[TWS-LATENCY] {order.symbol} Order sent in {latency:.1f} ms "
                            f"(start {start_ts:.0f} → end {end_ts:.0f})")

                order.mark_active(result=f"IB Order ID: {order._ib_order_id}")
                if getattr(order, "_status_callback", None):
                    try:
                        order._status_callback(f"Finalized: {order.symbol} {order.order_id}", "green")
                    except Exception as e:
                        logging.error(f"[WaitService] UI callback failed for finalized order {order.order_id}: {e}")
                
                if getattr(order, "_fill_event", None):
                    filled = order._fill_event.wait(timeout=60)
                    if filled and order.state == OrderState.FINALIZED:
                        order_manager.add_finalized_order(order_id, order)
                        msg = f"[WaitService] Order finalized {order_id} → IB ID: {order._ib_order_id}"
                        logging.info(msg)
                        watcher_info.update_watcher(order_id, STATUS_FINALIZED)
                        _update_tinfo_status(STATUS_FINALIZED)
                    else:
                        logging.warning(f"[WaitService] Order {order_id} not filled within timeout window.")
                        # Even if not filled, we mark the *watcher* as finalized if the order was sent
                        _update_tinfo_status(STATUS_FAILED, info={"error": "Fill event timed out"}) 

                # ✅ if stop-loss configured, launch stop-loss watcher
                    if order.trigger or (order.sl_price and order.state == OrderState.FINALIZED):
                        stop_loss_level = order.trigger - order.sl_price if order.right == 'C' or order.right == "CALL" else order.trigger + order.sl_price
                        exit_order = Order(
                            symbol=order.symbol,
                            expiry=order.expiry,
                            strike=order.strike,
                            right=order.right,
                            qty=order.qty,
                            entry_price=order.entry_price,   # keeps breakeven reference
                            tp_price=None,
                            sl_price=order.sl_price,
                            action="SELL",
                            type="MKT", # Use MKT for guaranteed stop-loss exit
                            trigger=None
                        )
                        ex_order = exit_order.set_position_size(order._position_size) 
                        ex_order.previous_id = order.order_id
                        ex_order.mark_active()
                        logging.info(f"[WAITSERVICE] Spawned EXIT watcher {ex_order.order_id} "
                                f"stop={stop_loss_level} ({order.right})")
                        
                 
                        self.start_stop_loss_watcher(ex_order, stop_loss_level, mode="poll")


            else:
                order.mark_failed("Failed to place order with TWS")
                msg = f"[WaitService] Order placement failed {order_id}"
                logging.error(msg)
                watcher_info.update_watcher(order_id, STATUS_FAILED)
                _update_tinfo_status(STATUS_FAILED, info={"error": "TWS place_custom_order failed"})

        except Exception as e:
            order.mark_failed(str(e))
            msg = f"[WaitService] Finalize failed {order_id}: {e}"
            logging.exception(msg) # 💡 Use exception logging
            _update_tinfo_status(STATUS_FAILED, info={"error": str(e)})

    def get_order_status(self, order_id: str):
        return self.tws.get_order_status(order_id)

    def cancel_active_order(self, order_id: str) -> bool:
        """
        Cancels an order that is live at TWS.
        This is different from cancel_order, which stops a local watcher.
        """
        try:
            if self.tws.cancel_custom_order(order_id):
                # Also cancel any local watcher associated with it
                self.cancel_order(order_id) 
                watcher_info.update_watcher(order_id, STATUS_CANCELLED)
                return True
            return False
        except Exception as e:
            logging.error(f"[WaitService] Cancel active order failed {order_id}: {e}")
            watcher_info.update_watcher(order_id, STATUS_FAILED, info={"error": str(e)})
            return False

    def get_all_orders_status(self):
        result = {
            'pending': self.list_pending_orders(),
            'active': {} # 💡 This seems to be legacy, TWS tracks active orders
        }
        # This logic is likely flawed as TWS is the source of truth for active orders
        # Re-kept as per original file, but recommend review.
        for order_id in list(self.pending_orders.keys()):
            status = self.get_order_status(order_id)
            if status:
                result['active'][order_id] = status
        return result
    


wait_service = OrderWaitService(polygon_service, create_tws_service())