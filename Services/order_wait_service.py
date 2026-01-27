import threading
import time
import logging
from typing import Optional
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
from Services.order_pipeline_states import OrderPipeline

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

        # ✅ Premium streaming cache (real-time bid/ask ticks)
        self._premium_streams = {}  # key: (symbol, expiry, strike, right) → stream data
        self._premium_streams_by_req_id = {}  # req_id → stream key (for tickPrice routing)
        self._stream_lock = threading.Lock()  # Thread-safe access to streams

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
                # ✅ FIX: Skip legacy preparation if model is None (pipeline handles it in _finalize_order)
                if not order._order_ready:
                    logging.warning(f"[WaitService] Order not ready, preparing | order_id={order_id}")
                    model = order._model
                    _args = order._args
                    
                    # ✅ Check if model exists before using it
                    if model is None:
                        logging.warning(f"[WaitService] Order model is None - skipping preparation (pipeline will handle) | order_id={order_id}")
                        # Pipeline will handle premium fetching at trigger time
                        # Just mark as ready to proceed with monitoring
                        order._order_ready = True
                    elif _args and "action" in _args:
                        try:
                            _order = model.prepare_option_order(
                                action=_args["action"],
                                position=_args["position"],
                                quantity=_args["quantity"],
                                trigger_price=_args["trigger_price"],
                                arcTick=_args["arcTick"],
                                type=_args.get("type", "LMT"),  # ✅ FIX: Use order type from UI, default to LMT
                                status_callback=_args["status_callback"]
                            )
                            order = _order
                            logging.info(f"[WaitService] Order prepared in watcher | order_id={order_id}")
                        except Exception as e:
                            logging.error(f"[WaitService] Failed to prepare order in watcher | order_id={order_id} | error={e}")
                            # Continue anyway - pipeline will handle it
                            order._order_ready = True
                    else:
                        logging.warning(f"[WaitService] Order args missing - skipping preparation | order_id={order_id}")
                        order._order_ready = True

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
                    trigger_ts = time.time() * 1000
                    logging.info(
                        f"[TRIGGER_POLL] 🚨 TRIGGERED! | order_id={order_id} | "
                        f"symbol={order.symbol} | price={last_price} trigger={order.trigger} | "
                        f"timestamp={trigger_ts:.0f}ms"
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
        add_start = time.time() * 1000
        
        order_id = order.order_id
        logging.info(
            f"[ADD_ORDER] Starting | order_id={order_id} | symbol={order.symbol} | "
            f"{order.expiry} {order.strike}{order.right} | trigger={order.trigger} | "
            f"mode={mode} | timestamp={add_start:.0f}ms"
        )
        
        with self.lock:
            self.pending_orders[order_id] = order
        logging.debug(f"[ADD_ORDER] Order added to pending_orders dict | order_id={order_id}")

        # UI callback for status updates
        cb = getattr(order, "_status_callback", None)
        
        # ✅ START PREMIUM STREAM FIRST (before checking trigger)
        if order.trigger:  # Only stream if order has trigger
            stream_start = time.time() * 1000
            logging.info(
                f"[ADD_ORDER] Starting premium stream | order_id={order_id} | "
                f"symbol={order.symbol} {order.expiry} {order.strike}{order.right} | "
                f"timestamp={stream_start:.0f}ms"
            )
            if cb:
                cb(f"Starting premium stream for {order.symbol}...", "blue")
            self._start_premium_stream(order)
            stream_latency = time.time() * 1000 - stream_start
            logging.info(
                f"[ADD_ORDER] ✅ Premium stream started | order_id={order_id} | "
                f"latency={stream_latency:.1f}ms"
            )
            # Give stream a moment to start receiving ticks (100ms)
            time.sleep(0.1)

        # ✅ IMMEDIATE TRIGGER CHECK (after stream started)
        logging.debug(f"[ADD_ORDER] Checking if trigger already met | order_id={order_id}")
        current_price = self.polygon.get_last_trade(order.symbol)
        
        if current_price and order.is_triggered(current_price):
            # ✅ Check if premarket - if so, show rebase popup instead of finalizing
            if is_market_closed_or_pre_market():
                logging.info(
                    f"[ADD_ORDER] 🚨 TRIGGER MET IN PREMARKET | order_id={order_id} | "
                    f"current={current_price} trigger={order.trigger} | showing rebase popup"
                )
                # Create a dummy ThreadInfo for the popup
                tinfo = ThreadInfo(order_id, order.symbol, watcher_type="trigger", mode="poll")
                watcher_info.add_watcher(tinfo)
                self._handle_premarket_trigger(order_id, order, tinfo, current_price)
                # Don't return - continue to start watcher so it can trigger again after rebase
            else:
                # RTH - fire immediately
                total_latency = time.time() * 1000 - add_start
                logging.info(
                    f"[ADD_ORDER] 🚨 TRIGGER ALREADY MET (RTH) | order_id={order_id} | "
                    f"current={current_price} trigger={order.trigger} | executing immediately | "
                    f"total_latency={total_latency:.1f}ms"
                )
                self._finalize_order(order_id, order, tinfo=None, last_price=current_price)
                return order_id

        # Subscribe / start poller only if trigger not already met
        logging.info(f"[ADD_ORDER] Starting trigger watcher | order_id={order_id} | mode={mode}")
        watcher_start = time.time() * 1000
        self.start_trigger_watcher(order, mode) # 💡 Simplified to use the router
        watcher_latency = time.time() * 1000 - watcher_start
        
        # ✅ UI: Show watcher armed status
        if cb:
            trigger_dir = "↑" if order.action == "BUY" else "↓"
            cb(f"🎯 Watcher ARMED | Trigger: {trigger_dir}{order.trigger}", "green")
        
        total_latency = time.time() * 1000 - add_start
        logging.info(
            f"[ADD_ORDER] ✅ Order added and watching | order_id={order_id} | "
            f"mode={mode} trigger={order.trigger} current={current_price} | "
            f"watcher_latency={watcher_latency:.1f}ms total={total_latency:.1f}ms"
        )
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
        
        # ✅ Stop premium stream for this order
        if order:
            self._stop_premium_stream(order)

    def _start_premium_stream(self, order: Order):
        """Start streaming bid/ask ticks for option premium (real-time cache)"""
        stream_start = time.time() * 1000
        
        key = (order.symbol.upper(), order.expiry, float(order.strike), order.right.upper())
        logging.info(
            f"[PREMIUM_STREAM] Starting | order_id={order.order_id} | "
            f"symbol={order.symbol} {order.expiry} {order.strike}{order.right} | "
            f"key={key} | timestamp={stream_start:.0f}ms"
        )
        
        with self._stream_lock:
            # Check if stream already exists
            if key in self._premium_streams:
                # Stream exists - just add this order to reference count
                self._premium_streams[key]["order_ids"].add(order.order_id)
                existing_req_id = self._premium_streams[key].get("req_id")
                latency = time.time() * 1000 - stream_start
                logging.info(
                    f"[PREMIUM_STREAM] ✅ Reusing existing stream | order_id={order.order_id} | "
                    f"req_id={existing_req_id} | latency={latency:.1f}ms (reused)"
                )
                return
            
            # Need to start new stream
            # Get conId (use cached if available)
            conid_start = time.time() * 1000
            conid = self.tws._pre_conid_cache.get(key)
            if conid:
                conid_latency = time.time() * 1000 - conid_start
                logging.info(
                    f"[PREMIUM_STREAM] ConID from cache | order_id={order.order_id} | "
                    f"conID={conid} | latency={conid_latency:.1f}ms"
                )
            else:
                # Resolve conId with retry logic and UI feedback
                logging.info(f"[PREMIUM_STREAM] Resolving conID (cache miss) | order_id={order.order_id}")
                from ibapi.contract import Contract
                contract = self.tws.create_option_contract(
                    order.symbol, order.expiry, order.strike, order.right
                )
                
                # UI callback for status updates
                cb = getattr(order, "_status_callback", None)
                
                # Retry logic: 3 attempts with 15s timeout each
                max_retries = 3
                conid = None
                
                for attempt in range(max_retries):
                    attempt_start = time.time() * 1000
                    
                    # Update UI to show conID resolution in progress
                    if cb:
                        cb(f"Resolving option conID... (attempt {attempt + 1}/{max_retries})", "blue")
                    
                    logging.info(
                        f"[PREMIUM_STREAM] ConID resolution attempt {attempt + 1}/{max_retries} | "
                        f"order_id={order.order_id} | symbol={order.symbol} {order.expiry} {order.strike}{order.right}"
                    )
                    
                    conid = self.tws.resolve_conid(contract, timeout=15)
                    attempt_latency = time.time() * 1000 - attempt_start
                    
                    if conid:
                        logging.info(
                            f"[PREMIUM_STREAM] ✅ ConID resolved on attempt {attempt + 1} | "
                            f"order_id={order.order_id} | conID={conid} | latency={attempt_latency:.1f}ms"
                        )
                        if cb:
                            cb(f"ConID resolved: {conid}", "green")
                        break
                    else:
                        logging.warning(
                            f"[PREMIUM_STREAM] ConID attempt {attempt + 1} failed | "
                            f"order_id={order.order_id} | latency={attempt_latency:.1f}ms"
                        )
                        if attempt < max_retries - 1:
                            # Wait a bit before retry
                            time.sleep(1)
                
                conid_latency = time.time() * 1000 - conid_start
                
                if not conid:
                    total_latency = time.time() * 1000 - stream_start
                    logging.error(
                        f"[PREMIUM_STREAM] ❌ ConID resolution failed after {max_retries} attempts | "
                        f"order_id={order.order_id} | symbol={order.symbol} {order.expiry} {order.strike}{order.right} | "
                        f"conid_latency={conid_latency:.1f}ms total={total_latency:.1f}ms"
                    )
                    if cb:
                        cb(f"❌ ConID resolution failed after {max_retries} attempts", "red")
                    return
                    
                # Cache it
                self.tws._pre_conid_cache[key] = conid
                logging.info(
                    f"[PREMIUM_STREAM] ✅ ConID resolved | order_id={order.order_id} | "
                    f"conID={conid} | latency={conid_latency:.1f}ms"
                )
            
            # Create contract with conId
            from ibapi.contract import Contract
            contract = Contract()
            contract.conId = conid
            contract.symbol = order.symbol
            contract.secType = "OPT"
            contract.lastTradeDateOrContractMonth = order.expiry
            contract.strike = order.strike
            contract.right = order.right
            contract.exchange = "SMART"
            contract.currency = "USD"
            
            # Get next req_id
            req_id = self.tws._get_next_req_id()
            
            # Store stream data
            self._premium_streams[key] = {
                "req_id": req_id,
                "bid": None,
                "ask": None,
                "mid": None,
                "last_update": 0,
                "order_ids": {order.order_id}
            }
            self._premium_streams_by_req_id[req_id] = key
            
            # Request streaming market data (snapshot=False means continuous)
            try:
                req_start = time.time() * 1000
                self.tws.reqMktData(req_id, contract, "", False, False, [])
                req_latency = time.time() * 1000 - req_start
                total_latency = time.time() * 1000 - stream_start
                
                logging.info(
                    f"[PREMIUM_STREAM] ✅ Stream started | order_id={order.order_id} | "
                    f"symbol={order.symbol} {order.expiry} {order.strike}{order.right} | "
                    f"req_id={req_id} conID={conid} | "
                    f"req_latency={req_latency:.1f}ms total={total_latency:.1f}ms | "
                    f"timestamp={time.time()*1000:.0f}ms"
                )
            except Exception as e:
                total_latency = time.time() * 1000 - stream_start
                logging.error(
                    f"[PREMIUM_STREAM] ❌ Failed to start | order_id={order.order_id} | "
                    f"error={e} | total_latency={total_latency:.1f}ms"
                )
                # Cleanup on error
                del self._premium_streams[key]
                del self._premium_streams_by_req_id[req_id]
    
    def _stop_premium_stream(self, order: Order):
        """Stop premium stream when order is done (reference counting)"""
        key = (order.symbol.upper(), order.expiry, float(order.strike), order.right.upper())
        
        with self._stream_lock:
            stream = self._premium_streams.get(key)
            if not stream:
                return  # Stream doesn't exist
            
            # Remove this order from reference count
            stream["order_ids"].discard(order.order_id)
            
            # If no more orders using this stream, cancel it
            if not stream["order_ids"]:
                req_id = stream["req_id"]
                try:
                    self.tws.cancelMktData(req_id)
                    logging.info(
                        f"[WaitService] Stopped premium stream for {order.symbol} {order.expiry} {order.strike}{order.right} "
                        f"(req_id={req_id})"
                    )
                except Exception as e:
                    logging.warning(f"[WaitService] Error cancelling premium stream: {e}")
                
                # Cleanup
                del self._premium_streams[key]
                del self._premium_streams_by_req_id[req_id]
    
    def get_streamed_premium(self, order: Order) -> Optional[float]:
        """Get premium from active stream (0ms latency) - returns None if stream not available (PUBLIC API)"""
        key = (order.symbol.upper(), order.expiry, float(order.strike), order.right.upper())
        
        with self._stream_lock:
            stream = self._premium_streams.get(key)
            if not stream:
                logging.debug(f"[WaitService] Stream not found for {order.symbol} {order.expiry} {order.strike}{order.right}")
                return None  # Stream doesn't exist
            
            # ✅ Enhanced logging for stream reliability testing
            req_id = stream.get("req_id")
            last_update = stream.get("last_update", 0)
            age = time.time() - last_update if last_update > 0 else float('inf')
            
            bid = stream.get("bid")
            ask = stream.get("ask")
            mid = stream.get("mid")
            
            # Log stream state for debugging
            logging.debug(
                f"[WaitService] Stream state | req_id={req_id} | "
                f"bid={bid} ask={ask} mid={mid} | "
                f"age={age:.2f}s | last_update={last_update}"
            )
            
            # If we have mid, use it
            if mid and mid > 0:
                logging.info(
                    f"[WaitService] ✅ Premium from stream: {mid} (0ms latency) | "
                    f"bid={bid} ask={ask} | age={age:.2f}s"
                )
                return mid
            
            # If we have bid and ask but no mid, calculate it
            if bid and ask and bid > 0 and ask > 0:
                calculated_mid = (bid + ask) / 2
                stream["mid"] = calculated_mid
                stream["last_update"] = time.time()
                logging.info(
                    f"[WaitService] ✅ Premium calculated from stream bid/ask: {calculated_mid} (0ms latency) | "
                    f"bid={bid} ask={ask} | age={age:.2f}s"
                )
                return calculated_mid
            
            # Check if stream is stale (updated more than 5 seconds ago)
            if last_update > 0 and age > 5:
                logging.warning(
                    f"[WaitService] ⚠️ Premium stream stale for {order.symbol} {order.expiry} {order.strike}{order.right} | "
                    f"age={age:.1f}s | bid={bid} ask={ask} mid={mid}"
                )
                return None
            
            # Stream exists but no data yet
            logging.debug(
                f"[WaitService] Stream exists but no valid price data yet for {order.symbol} | "
                f"bid={bid} ask={ask} mid={mid} | age={age:.2f}s"
            )
        
        return None

    def list_pending_orders(self):
        with self.lock:
            return [o.to_dict() for o in self.pending_orders.values()]

    def _on_tick(self, order_id: str, price: float):
        """Callback from PolygonService for live ENTRY triggers."""
        tick_ts = time.time() * 1000
        
        with self.lock:
            order = self.pending_orders.get(order_id)
            if not order or order_id in self.cancelled_orders:
                logging.debug(f"[TRIGGER_WS] Tick ignored | order_id={order_id} | price={price} | order not found or cancelled")
                return # Order was finalized or cancelled

        # Update watcher info with live price
        if tinfo := watcher_info.get_watcher(order_id):
            tinfo.update_status(STATUS_RUNNING, last_price=price)

        with self.trigger_lock:
            if order.is_triggered(price) and  order not in self.trigger_status:
                self.trigger_status.add(order)
                logging.info(
                    f"[TRIGGER_WS] 🚨 TRIGGERED! | order_id={order_id} | "
                    f"symbol={order.symbol} | price={price} trigger={order.trigger} | "
                    f"timestamp={tick_ts:.0f}ms"
                )
                
                # Check if premarket - if so, prompt rebase/cancel instead of firing
                if is_market_closed_or_pre_market():
                    logging.info(
                        f"[TRIGGER_WS] Premarket trigger hit | order_id={order_id} | "
                        f"prompting rebase/cancel | price={price} trigger={order.trigger}"
                    )
                    self._handle_premarket_trigger(order_id, order, tinfo, price)
                    # Continue watching (don't remove from pending_orders)
                    # Remove from trigger_status so it can trigger again after rebase
                    self.trigger_status.remove(order)
                    return
                else:
                    # RTH - fire the order
                    logging.info(
                        f"[TRIGGER_WS] RTH trigger hit - finalizing order | order_id={order_id} | "
                        f"price={price} trigger={order.trigger} | timestamp={tick_ts:.0f}ms"
                    )
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
            import sys
            # Use global reference instead of unreliable gc.get_objects()
            logging.info(f"[WaitService] Attempting to get app instance via main.get_app_instance()...")
            logging.info(f"[WaitService] main module: {main}, main._app_instance: {getattr(main, '_app_instance', 'NOT_FOUND')}")
            app = main.get_app_instance()
            logging.info(f"[WaitService] get_app_instance() returned: {app} (type: {type(app)})")
            
            # Fallback: try to get it directly from the module
            if not app:
                logging.warning(f"[WaitService] get_app_instance() returned None, trying direct access...")
                app = getattr(main, '_app_instance', None)
                logging.info(f"[WaitService] Direct access to _app_instance: {app}")
            
            if app:
                logging.info(f"[WaitService] ✅ Found ArcTriggerApp instance: {app}")
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
                logging.warning(f"[WaitService] ❌ ArcTriggerApp instance is None (app not initialized yet)")
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
        """
        Finalizes an order using the State Pattern pipeline.
        
        Delegates to OrderPipeline which handles:
        - Premium fetching
        - Entry price calculation
        - Order placement
        - Fill waiting
        - Stop-loss watcher setup
        """
        finalize_start = time.time() * 1000
        
        # ✅ UI: Show trigger hit status
        cb = getattr(order, "_status_callback", None)
        if cb:
            cb(f"🚨 TRIGGER HIT @ {last_price:.2f} | Executing...", "orange")
        
        logging.info(
            f"[FINALIZE] Starting order finalization | order_id={order_id} | "
            f"symbol={order.symbol} {order.expiry} {order.strike}{order.right} | "
            f"trigger_price={last_price} | entry_price={getattr(order, 'entry_price', None)} | "
            f"_order_ready={getattr(order, '_order_ready', None)} | timestamp={finalize_start:.0f}ms"
        )
        
        # ✅ Use State Pattern pipeline
        pipeline = OrderPipeline(order, self, self.tws, tinfo)
        pipeline_start = time.time() * 1000
        success = pipeline.execute()
        pipeline_latency = time.time() * 1000 - pipeline_start
        total_latency = time.time() * 1000 - finalize_start
        
        if not success:
            # Pipeline failed - update watcher info
            watcher_info.update_watcher(order_id, STATUS_FAILED)
            if tinfo:
                tinfo.update_status(STATUS_FAILED, last_price=last_price)
            logging.error(
                f"[FINALIZE] ❌ FAILED | order_id={order_id} | "
                f"pipeline_latency={pipeline_latency:.1f}ms total={total_latency:.1f}ms"
            )
            if cb:
                cb(f"❌ Order FAILED | {order.symbol}", "red")
        else:
            logging.info(
                f"[FINALIZE] ✅ SUCCESS | order_id={order_id} | "
                f"pipeline_latency={pipeline_latency:.1f}ms total={total_latency:.1f}ms"
            )
            if cb:
                cb(f"✅ Order SENT | {order.symbol} | Waiting for fill...", "green")

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