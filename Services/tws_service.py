# tws_service.py
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order as IBOrder

import logging
import random
from typing import List, Dict, Optional
from Helpers.Order import Order
import traceback
from Services.nasdaq_info import is_market_closed_or_pre_market
from Services.persistent_conid_storage import storage
import time, threading
ORDER_LOCK = threading.Lock()   # <-- only one order can pass at a time

class TWSService(EWrapper, EClient):
    """
    TWS Service that integrates with Helpers.Order system
    """
    def __init__(self):
        EClient.__init__(self, self)
        self.next_valid_order_id = None
        self.connection_ready = threading.Event()
        self.client_id = random.randint(1, 999999)
        self.connected = False
        
        # For data requests
        self._maturities_data = {}
        self._maturities_req_id = None
        self._maturities_event = threading.Event()
        
        self._contract_details = {}
        self._contract_details_req_id = None
        self._contract_details_event = threading.Event()
        
        self._request_counter = 1
        self.symbol_samples = {}
        self._pre_conid_cache = {}   # key: (symbol, expiry, strike, right) → conId

        # Track custom orders from Helpers.Order
        self.option_chains = {}  # Add this line
        self._pending_orders = {}  # custom_order_id -> Helpers.Order object
        self._last_print = 0
        self._positions_by_order_id: dict[str, dict] = {}
        self._ib_to_order_id: dict[int, str] = {}
        self._ib_to_custom_id: dict[int, str] = {}   # <-- NEW: map IB orderId -> custom UUID
        
        # ✅ Temporary tickPrice callbacks for snapshot requests
        self._temporary_tick_callbacks = {}  # req_id → callback function
        
        logging.info("[TWSService] __init__ finished – empty caches, counters reset")

    def conn_status(self) -> bool:
        """
        Returns True if currently connected to TWS and next_valid_order_id has been set.
        This method checks both the IB API connection and the internal event flag.
        """
        logging.info(f"[TWSService] conn_status() called – checking connection health")
        is_alive = self.isConnected() and self.connection_ready.is_set() and self.next_valid_order_id is not None
        if not is_alive:
            logging.warning("[TWSService] conn_status: Not connected to TWS")
        else:
            now = time.time()
            if now - self._last_print >= 60:
                logging.info("[TWSService] conn_status: Connected and healthy")
                self._last_print = now
        logging.info(f"[TWSService] conn_status() returning {is_alive}")
        return is_alive

    def disconnect(self) -> None:
        """
        Wrap the real disconnect() so we can log WHO/WHERE called it,
        then delegate to the genuine EClient implementation.
        """
        logging.info("[TWSService] disconnect() entry – building caller stack")
        # Build a short human-readable caller string (last 2 frames)
        caller = "\n".join(
            f"  {s}" for s in traceback.format_stack(limit=3)[:-1][-2:]
        )
        logging.warning(
            f"[TWSService] disconnect() invoked — socket will close.\n{caller}"
        )

        # *** NOW run the real IB code ***
        super().disconnect()

        # Mark our own state
        self.connected = False
        self.connection_ready.clear()
        logging.info("[TWSService] disconnect() complete – state cleared")

    def reconnect(self, host: str = "127.0.0.1", port: int = 7497, timeout: int = 10) -> bool:
        """
        Attempts to reconnect to TWS/IB Gateway.
        Safely disconnects first if a stale session exists.
        Returns True if the reconnection is successful.
        """
        logging.info(f"[TWSService] reconnect() start – target {host}:{port}")
        try:
            if self.isConnected():
                logging.info("[TWSService] reconnect(): Closing existing connection before retry...")
                try:
                    self.disconnect_gracefully()
                    time.sleep(1)
                except Exception as e:
                    logging.warning(f"[TWSService] reconnect(): Error while disconnecting: {e}")

            logging.info(f"[TWSService] Attempting reconnection to {host}:{port} (Client ID: {self.client_id})")
            result = self.connect_and_start(host=host, port=port, timeout=timeout)
            
            if result:
                logging.info("[TWSService] reconnect(): Reconnected successfully.")
                return True
            else:
                logging.error("[TWSService] reconnect(): Reconnection failed.")
                return False

        except Exception as e:
            logging.error(f"[TWSService] reconnect(): Exception during reconnection: {e}")
            return False

    def nextValidId(self, orderId: int):
        logging.info(f"[TWSService] nextValidId callback entry – orderId={orderId}")
        super().nextValidId(orderId)
        self.next_valid_order_id = orderId
        logging.info(f"NextValidId: {orderId} (Client ID: {self.client_id})")
        self.connection_ready.set()
        logging.info("[TWSService] connection_ready event set")

    # ---------------- Symbol Search ----------------
    def symbolSamples(self, reqId, contractDescriptions):
        logging.info(f"[TWSService] symbolSamples fired – reqId={reqId}")
        results = []
        for desc in contractDescriptions:
            c = desc.contract
            results.append({
                "symbol": c.symbol,
                "secType": c.secType,
                "currency": c.currency,
                "exchange": c.exchange,
                "primaryExchange": c.primaryExchange,
                "description": desc.derivativeSecTypes
            })
            logging.info(f"[TWSService] symbolSamples appended – {c.symbol} {c.secType}")
        self.symbol_samples[reqId] = results
        logging.info(f"[TWSService] symbolSamples finished – stored {len(results)} rows")

    def search_symbol(self, name: str, reqId: int = None):
        """
        Search for symbols matching a pattern.
        Hybrid approach: Check cache first, then fetch from TWS.
        """
        logging.info(f"[TWSService] search_symbol() called – name={name}")
        
        # ✅ Step 1: Check cache first (instant, 0ms)
        cached_results = storage.get_symbol_search(name)
        if cached_results:
            logging.info(f"[TWSService] ✅ Using cached symbol search for '{name}' ({len(cached_results)} results, 0ms)")
            # Still fetch fresh in background to update cache
            def fetch_fresh():
                try:
                    fresh_results = self._fetch_symbols_from_tws(name, reqId)
                    if fresh_results:
                        storage.store_symbol_search(name, fresh_results)
                except Exception as e:
                    logging.debug(f"[TWSService] Background refresh failed for '{name}': {e}")
            threading.Thread(target=fetch_fresh, daemon=True).start()
            return cached_results
        
        # ✅ Step 2: No cache - fetch from TWS (normal flow)
        return self._fetch_symbols_from_tws(name, reqId)
    
    def _fetch_symbols_from_tws(self, name: str, reqId: int = None) -> list:
        """Internal method to fetch symbols from TWS"""
        if reqId is None:
            reqId = self._get_next_req_id()
            logging.info(f"[TWSService] search_symbol() auto-selected reqId={reqId}")
        self.reqMatchingSymbols(reqId, name)
        time.sleep(2)
        out = self.symbol_samples.get(reqId, [])
        if reqId in self.symbol_samples:
            del self.symbol_samples[reqId]
        logging.info(f"[TWSService] search_symbol() returning {len(out)} matches")
        # Cache the results
        if out:
            storage.store_symbol_search(name, out)
        return out

    def tickPrice(self, reqId, tickType, price, attrib):
        """
        Handle tick price updates from TWS.
        Routes to: 1) Premium streams (real-time), 2) Temporary snapshot callbacks
        """
        if price <= 0:
            return
        
        # ✅ FIRST: Check if this is a premium stream request
        # Use a safer import that won't cause circular dependency issues
        try:
            import sys
            if 'Services.order_wait_service' in sys.modules:
                wait_service = sys.modules['Services.order_wait_service'].wait_service
                if hasattr(wait_service, '_premium_streams_by_req_id'):
                    stream_key = wait_service._premium_streams_by_req_id.get(reqId)
                    if stream_key:
                        # This is a streaming request - update stream
                        with wait_service._stream_lock:
                            stream = wait_service._premium_streams.get(stream_key)
                            if stream:
                                if tickType == 1:  # BID
                                    stream["bid"] = price
                                    logging.info(f"[TWSService] ✅ Stream BID update: reqId={reqId} price={price}")
                                elif tickType == 2:  # ASK
                                    stream["ask"] = price
                                    logging.info(f"[TWSService] ✅ Stream ASK update: reqId={reqId} price={price}")
                                
                                # Calculate mid when both available
                                bid = stream.get("bid")
                                ask = stream.get("ask")
                                if bid is not None and ask is not None and bid > 0 and ask > 0:
                                    stream["mid"] = (bid + ask) / 2
                                    stream["last_update"] = time.time()
                                    logging.info(f"[TWSService] ✅ Stream MID calculated: reqId={reqId} bid={bid} ask={ask} mid={stream['mid']}")
                        return  # Handled by stream
        except (ImportError, AttributeError, KeyError) as e:
            # Log error for debugging
            logging.debug(f"[TWSService] Error checking premium stream: {e}")
        
        # ✅ SECOND: Check if this is a temporary snapshot callback
        callback = self._temporary_tick_callbacks.get(reqId)
        if callback:
            try:
                callback(reqId, tickType, price, attrib)
            except Exception as e:
                logging.warning(f"[TWSService] Error in temporary tick callback: {e}")
            return  # Handled by snapshot
        
        # No handler - ignore (might be other market data subscriptions)
    
    def error(self, reqId, errorCode, errorString, *args):
        """Error callback - handles both regular and protobuf errors"""
        logging.info(f"[TWSService] error() fired – reqId={reqId} code={errorCode} msg={errorString}")
        actual_error_code = errorCode
        if isinstance(errorCode, int) and errorCode > 10000:
            if "errorCode:" in str(errorString):
                try:
                    parts = str(errorString).split("errorCode:")
                    if len(parts) > 1:
                        actual_error_code = int(parts[1].split()[0])
                except:
                    actual_error_code = errorCode
        
        # Handle specific error codes
        if actual_error_code in [2104, 2106, 2158]:
            logging.info(f"TWS Info. Code: {actual_error_code}, Msg: {errorString}")
        elif actual_error_code == 502:
            logging.error("Connection failed - check TWS/IB Gateway")
            self.connection_ready.clear()
        elif actual_error_code == 504:
            logging.error(f"Not connected to TWS: {errorString}")
            self.connection_ready.clear()
        elif actual_error_code == 200:
            logging.warning(f"No security definition for reqId {reqId}")
            if reqId == self._maturities_req_id:
                self._maturities_event.set()
            elif reqId == self._contract_details_req_id:
                self._contract_details_event.set()
        elif actual_error_code == 321:
            logging.error(f"Contract validation error for reqId {reqId}: {errorString}")
            if reqId == self._maturities_req_id:
                self._maturities_event.set()
        else:
            logging.error(f"API Error. reqId: {reqId}, Code: {actual_error_code}, Msg: {errorString}")

    def orderStatus(
    self, orderId, status, filled, remaining, avgFillPrice,
    permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice
):
        logging.info(f"[TWSService] orderStatus entry – orderId={orderId} status={status} filled={filled}")
        custom_uuid = self._ib_to_custom_id.get(orderId)
        if not custom_uuid:
            logging.info(f"[TWSService] orderStatus – no custom_uuid mapping for IB orderId={orderId}")
            return

        status_str = status.lower()
        pos = self._positions_by_order_id.get(custom_uuid)

        if not pos:
            logging.info(f"[TWSService] orderStatus – no position cached for custom_uuid={custom_uuid}")
            return

        # ✅ ONLY SOURCE OF TRUTH FOR QTY
        if filled is not None:
            pos["qty"] = int(filled)

        if avgFillPrice:
            pos["avg_price"] = float(avgFillPrice)

        pos["status"] = status_str
        self._positions_by_order_id[custom_uuid] = pos

        logging.info(
            f"[TWSService] orderStatus update {custom_uuid}: "
            f"status={status_str} qty={pos['qty']} avg={pos.get('avg_price')}"
        )
        
        # ✅ FIX: When order is Filled, set fill event and mark as finalized
        if status_str == "filled":
            order = self._pending_orders.get(custom_uuid)
            if order:
                # Set fill event so order_wait_service can proceed
                if hasattr(order, "_fill_event"):
                    order._fill_event.set()
                    logging.info(f"[TWSService] Set fill event for order {custom_uuid}")
                
                # Mark order as finalized
                from Helpers.Order import OrderState
                if order.state != OrderState.FINALIZED:
                    result = f"IB Order ID: {orderId}, Filled: {filled}, Avg Price: {avgFillPrice}"
                    order.mark_finalized(result)
                    logging.info(f"[TWSService] Marked order {custom_uuid} as FINALIZED")
                    
                    # ✅ Also add to finalized_orders immediately so TP buttons work right away
                    from Services.order_manager import order_manager
                    order_manager.add_finalized_order(custom_uuid, order)
                    logging.info(f"[TWSService] Added order {custom_uuid} to finalized_orders")
            else:
                logging.warning(f"[TWSService] Order {custom_uuid} not found in _pending_orders when filled")

    def openOrder(self, orderId, contract, order: IBOrder, orderState):
        logging.info(f"Order opened - ID: {orderId}, Symbol: {contract.symbol}")

    def execDetails(self, reqId, contract, execution):
        logging.info(f"[TWSService] execDetails – reqId={reqId} orderId={execution.orderId} side={execution.side}")
        order_id = self._ib_to_order_id.get(execution.orderId)
        if not order_id:
            logging.info(f"[TWSService] execDetails – no mapping for IB orderId={execution.orderId}")
            return
        
        pos = self._positions_by_order_id.get(order_id)
        if not pos:
            # 🔧 If not found, check if SELL execution maps to a BUY UUID
            target_uuid = self._ib_to_order_id.get(execution.orderId)
            if target_uuid:
                pos = self._positions_by_order_id.get(target_uuid)
        if not pos:
            logging.info(f"[TWSService] execDetails – no position for order_id={order_id}")
            return


        side = (execution.side or "").upper()   # BOT or SLD
        old_qty = int(pos["qty"])
        old_avg = float(pos["avg_price"])
        shares = int(execution.shares)
        price = float(execution.price)

        if side == "BOT":
            new_qty = old_qty + shares
            new_avg = ((old_avg * old_qty) + (price * shares)) / new_qty if new_qty > 0 else old_avg
        elif side == "SLD":
            new_qty = max(0, old_qty - shares)
            new_avg = old_avg
        else:
            new_qty, new_avg = old_qty, old_avg

        pos["qty"] = new_qty
        pos["avg_price"] = new_avg
        self._positions_by_order_id[order_id] = pos

        logging.info(f"[TWSService] execDetails update {order_id}: side={side} qty={new_qty}, avg={new_avg}")

    def securityDefinitionOptionParameter(
    self, reqId: int, exchange: str,
    underlyingConId: int, tradingClass: str,
    multiplier: str, expirations: List[str],
    strikes: List[float]
):
        logging.info(f"[TWSService] securityDefinitionOptionParameter – reqId={reqId} exchange={exchange}")
        try:
            if reqId not in self._maturities_data or self._maturities_data[reqId] is None:
                self._maturities_data[reqId] = {
                    "exchange": exchange,
                    "underlyingConId": underlyingConId,
                    "tradingClass": tradingClass,
                    "multiplier": multiplier,
                    "expirations": set(),
                    "strikes": set(),
                }
                logging.info(f"[TWSService] securityDefinitionOptionParameter – initialized fresh dict for reqId={reqId}")
            data = self._maturities_data[reqId]
            before_e, before_s = len(data["expirations"]), len(data["strikes"])
            data["expirations"].update(expirations or [])
            data["strikes"].update(strikes or [])
            logging.info(
                f"[TWSService] Option chain fragment merged for {exchange}: "
                f"{len(data['expirations'])} expirations (+{len(data['expirations']) - before_e}), "
                f"{len(data['strikes'])} strikes (+{len(data['strikes']) - before_s})"
            )
        except Exception as e:
            logging.exception(f"[TWSService] securityDefinitionOptionParameter crash, reqId={reqId}")

    def securityDefinitionOptionParameterEnd(self, reqId: int):
        """Finalize merged option chain"""
        logging.info(f"[TWSService] securityDefinitionOptionParameterEnd – reqId={reqId}")
        if reqId in self._maturities_data:
            data = self._maturities_data[reqId]
            expirations = sorted(data["expirations"])
            strikes = sorted(data["strikes"])
            self._maturities_data[reqId]["expirations"] = expirations
            self._maturities_data[reqId]["strikes"] = strikes
            logging.info(
                f"[TWSService] Option chain complete: {len(expirations)} expirations, {len(strikes)} strikes"
            )
        self._maturities_event.set()
        logging.info(f"[TWSService] securityDefinitionOptionParameterEnd – event set for reqId={reqId}")

    def contractDetails(self, reqId: int, contractDetails):
        logging.info(f"[TWSService] contractDetails – reqId={reqId}")
        self._contract_details[reqId] = contractDetails
        self._contract_details_event.set()
        logging.info(f"[TWSService] contractDetails – event set for reqId={reqId}")

    def contractDetailsEnd(self, reqId: int):
        logging.info(f"[TWSService] contractDetailsEnd – reqId={reqId}")
        self._contract_details_event.set()

    def connectionClosed(self):
        logging.warning("Connection to TWS closed")
        self.connection_ready.clear()

    def _reader_wrapper(self):
        """Catch everything that kills the reader loop."""
        logging.info("[TWSService] _reader_wrapper – starting IB run loop")
        try:
            self.run()                 # real IB loop
        except Exception as exc:
            logging.exception("IB reader thread died with exception")
        finally:
            logging.warning("IB reader thread ended -> auto-reconnect")
            self.connected = False
            self.connection_ready.clear()
            # optional: schedule reconnect here or raise a flag
            self.connect_and_start()

    def connect_and_start(self, host='127.0.0.1', port=7497, timeout=10):
        """Connect to TWS/IB Gateway"""
        logging.info(f"[TWSService] connect_and_start – {host}:{port} timeout={timeout}")
        if self.connected:
            logging.info("[TWSService] connect_and_start – already connected, skipping")
            return True
        try:
            logging.info(f"Connecting to TWS on {host}:{port} with Client ID: {self.client_id}")
            self.connect(host, port, self.client_id)
            self.connected = True
            api_thread = threading.Thread(
                target=self._reader_wrapper,          # ← new wrapper
                daemon=True,
                name=f"TWS-API-Thread-{self.client_id}"
            )
            
            api_thread.start()
            logging.info("[TWSService] connect_and_start – IB reader thread launched")
            
            if self.connection_ready.wait(timeout=timeout):
                logging.info("Successfully connected to TWS")
                return True
            else:
                logging.error("Connection timeout")
                return False
                
        except Exception as e:
            logging.error(f"Failed to connect to TWS: {str(e)}")
            return False

    def is_connected(self):
        flag = self.connection_ready.is_set() and self.next_valid_order_id is not None
        logging.info(f"[TWSService] is_connected() returning {flag}")
        return flag

    def _get_next_req_id(self):
        req_id = self._request_counter
        self._request_counter += 1
        logging.info(f"[TWSService] _get_next_req_id() -> {req_id}")
        return req_id

    def get_maturities(self, symbol: str, exchange: str = "SMART", currency: str = "USD", 
                      timeout: int = 10) -> Optional[Dict]:
        """Get option expirations and strikes for a symbol"""
        logging.info(f"[TWSService] get_maturities() – symbol={symbol} exchange={exchange}")
        if not self.is_connected():
            logging.error("Not connected to TWS")
            return None

        # Resolve underlying contract first
        underlying_contract = self.create_stock_contract(symbol, exchange, currency)
        underlying_conid = self.resolve_conid(underlying_contract)
        
        if not underlying_conid:
            logging.error(f"Failed to resolve conId for {symbol}")
            return None

        req_id = self._get_next_req_id()
        self._maturities_req_id = req_id
        self._maturities_data[req_id] = None
        self._maturities_event.clear()

        try:
            logging.info(f"Requesting option chain for {symbol}")
            self.reqSecDefOptParams(
                reqId=req_id, 
                underlyingSymbol=symbol,
                futFopExchange="", 
                underlyingSecType="STK",
                underlyingConId=underlying_conid
            )

            if self._maturities_event.wait(timeout=timeout):
                data = self._maturities_data.get(req_id)
                if data:
                    logging.info(f"Retrieved {len(data['expirations'])} expirations for {symbol}")
                    return data
                else:
                    logging.warning(f"No option chain data for {symbol}")
                    return None
            else:
                logging.error(f"Timeout getting option chain for {symbol}")
                return None

        except Exception as e:
            logging.error(f"Error getting maturities for {symbol}: {str(e)}")
            return None
        finally:
            if req_id in self._maturities_data:
                del self._maturities_data[req_id]
                logging.info(f"[TWSService] get_maturities() – cleaned up reqId={req_id}")

    def resolve_conid(self, contract: Contract, timeout: int = 10) -> Optional[int]:
        """Resolve contract to conId"""
        logging.info(f"[TWSService] resolve_conid() – contract={contract.symbol} secType={getattr(contract, 'secType', '?')}")
        
        # Check cache for STOCK contracts
        if contract.secType == "STK":
            conid = storage.get_conid(contract.symbol) 
            if conid != None:
                logging.info(f"[TWSService] using stored STOCK conid at resolve_conid({contract.symbol})")
                return int(conid)
        
        # ✅ NEW: Check cache for OPTION contracts (hybrid approach)
        if contract.secType == "OPT":
            expiry = getattr(contract, 'lastTradeDateOrContractMonth', None)
            strike = getattr(contract, 'strike', None)
            right = getattr(contract, 'right', None)
            if expiry and strike is not None and right:
                cached_conid = storage.get_option_conid(
                    contract.symbol, expiry, float(strike), right
                )
                if cached_conid:
                    logging.info(f"[TWSService] ✅ Using cached OPTION conid for {contract.symbol} {expiry} {strike}{right} → {cached_conid} (0ms)")
                    return int(cached_conid)
                else:
                    logging.debug(f"[TWSService] No cached OPTION conid for {contract.symbol} {expiry} {strike}{right}, resolving from TWS")
        
        # For contracts not in cache, resolve fresh from TWS
        if not self.is_connected():
            return None

        req_id = self._get_next_req_id()
        event = threading.Event()
        self._contract_details[req_id] = {"event": event, "details": None}

        logging.info(f"[ResolveConId] Starting for {contract.symbol} "
                 f"{getattr(contract, 'lastTradeDateOrContractMonth', '?')} "
                 f"{getattr(contract, 'strike', '?')}{getattr(contract, 'right', '?')} "
                 f"(req_id={req_id}, timeout={timeout}s)")


        def on_contract_details(reqId, contractDetails):
            if reqId == req_id:
                self._contract_details[req_id]["details"] = contractDetails
                event.set()

        def on_contract_details_end(reqId):
            if reqId == req_id:
                event.set()

        # temporarily hook callbacks
        orig_cd = self.contractDetails
        orig_cde = self.contractDetailsEnd
        self.contractDetails = on_contract_details
        self.contractDetailsEnd = on_contract_details_end

        try:
            logging.info(f"[ResolveConId] Requesting contract details from IBKR for {contract.symbol}") 
            start_time = time.time()

            self.reqContractDetails(req_id, contract)
            if event.wait(timeout):
                elapsed = time.time() - start_time
                logging.info(f"[ResolveConId] Callback received for {contract.symbol} after {elapsed:.2f}s")
                data = self._contract_details[req_id]["details"]
                if data:
                    conid = data.contract.conId
                    logging.info(f"[ResolveConId] ✅ Resolved conId={conid} "
                                 f"for {contract.symbol} in {elapsed:.2f}s")
                    
                    # ✅ FIX: Cache option conID after resolving (for future use)
                    if contract.secType == "OPT":
                        expiry = getattr(contract, 'lastTradeDateOrContractMonth', None)
                        strike = getattr(contract, 'strike', None)
                        right = getattr(contract, 'right', None)
                        if expiry and strike is not None and right:
                            storage.store_option_conid(
                                contract.symbol, expiry, float(strike), right, str(conid)
                            )
                            logging.info(
                                f"[ResolveConId] ✅ Cached option conID for {contract.symbol} "
                                f"{expiry} {strike}{right} → {conid}"
                            )
                    
                    return conid
                else:
                    logging.info(f"[ResolveConId] ⚠️ Empty data for {contract.symbol}, "
                                 f"IBKR returned no contract details (elapsed={elapsed:.2f}s)")
                    return None
            else:
                logging.info(f"[ResolveConId] ⏱ Timeout waiting {timeout}s for {contract.symbol} "
                             f"(req_id={req_id})")
                return None

        except Exception as e:
            logging.info(f"[ResolveConId] ❌ Exception resolving {contract.symbol}: {str(e)}")
            return None
        finally:
            self.contractDetails = orig_cd
            self.contractDetailsEnd = orig_cde
            del self._contract_details[req_id]
            logging.info(f"[ResolveConId] Cleanup done for reqId={req_id}")


    def create_option_contract(self, symbol: str, last_trade_date: str, strike: float, right: str, 
                             exchange: str = "SMART", currency: str = "USD") -> Contract:
        """Create IB option contract - converts CALL/PUT to C/P"""
        logging.info(f"[TWSService] create_option_contract – {symbol} {last_trade_date} {strike}{right}")
        ib_right = "C" if right.upper() in ["C", "CALL"] else "P"
        
        contract = Contract()
        contract.symbol = symbol.upper()
        contract.secType = "OPT"
        contract.exchange = exchange
        contract.currency = currency
        contract.lastTradeDateOrContractMonth = last_trade_date
        contract.strike = float(strike)
        contract.right = ib_right
        contract.multiplier = "100"
        
        return contract

    def create_stock_contract(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> Contract:
        logging.info(f"[TWSService] create_stock_contract – {symbol} {exchange}")
        contract = Contract()
        contract.symbol = symbol.upper()
        contract.secType = "STK"
        contract.exchange = exchange
        contract.currency = currency
        return contract
    
    def get_option_chain(self, symbol: str, expiry: str, exchange: str = "SMART", currency: str = "USD",
                        timeout: int = 10) -> Optional[List[Dict]]:
        """
        Build a basic option chain for a given symbol and expiry.
        Returns a list of dicts with strike/right.
        """
        logging.info(f"[TWSService] get_option_chain – {symbol} {expiry}")
        try:
            maturities = self.get_maturities(symbol, exchange, currency, timeout)
            if not maturities:
                return []

            if expiry not in maturities['expirations']:
                logging.error(f"TWSService: expiry {expiry} not in available expirations for {symbol}")
                return []

            strikes = maturities.get('strikes', [])
            chain = []
            for strike in strikes:
                chain.append({"expiry": expiry, "strike": strike, "right": "C"})
                chain.append({"expiry": expiry, "strike": strike, "right": "P"})
            logging.info(f"[TWSService] get_option_chain – built {len(chain)} legs")
            return chain
        except Exception as e:
            logging.error(f"TWSService: Failed to build option chain for {symbol}: {e}")
            return []

    def get_option_snapshot(self, symbol: str, expiry: str, strike: float, right: str, timeout: int = 3):
        logging.info(f"[TWSService] get_option_snapshot – {symbol} {expiry} {strike}{right}")
        if not self.is_connected():
            logging.error("TWSService.get_option_snapshot(): not connected")
            return None

        ts_req = time.time() * 1000
        logging.info(
            "[PREMIUM_REQUEST] "
            f"ts={ts_req:.0f} symbol={symbol} expiry={expiry} strike={strike} right={right} "
            f"timeout={timeout}"
        )

        contract = self.create_option_contract(symbol, expiry, strike, right)
        conid = self.resolve_conid(contract)
        logging.info(
            "[PREMIUM_CONTRACT] "
            f"ts={time.time()*1000:.0f} symbol={symbol} conId={conid}"
        )

        if not conid:
            logging.error(f"TWSService: Failed to resolve conId for {symbol} {expiry} {strike}{right}")
            return None
        contract.conId = conid

        req_id = self._get_next_req_id()
        result = {"bid": None, "ask": None, "last": None, "mid": None}
        event = threading.Event()

        def tickPrice_callback(reqId, tickType, price, attrib):
            if reqId != req_id or price <= 0:
                return
            if tickType == 1:
                result["bid"] = price
            elif tickType == 2:
                result["ask"] = price
            elif tickType == 4:
                result["last"] = price
            if result["bid"] and result["ask"]:
                result["mid"] = (result["bid"] + result["ask"]) / 2
                event.set()

        # ✅ Use temporary callback dict instead of overriding tickPrice
        self._temporary_tick_callbacks[req_id] = tickPrice_callback

        try:
            self.reqMktData(req_id, contract, "", True, False, [])
            logging.info(
            "[PREMIUM_SUBSCRIBE] "
            f"ts={time.time()*1000:.0f} req_id={req_id} symbol={symbol} conId={conid}"
        )

            event.wait(timeout)
            bid, ask = result["bid"], result["ask"]
            result["mid"] = (bid + ask) / 2 if bid and ask else bid or ask
            logging.info(
                "[PREMIUM] "
                f"ts={time.time()*1000:.0f} symbol={symbol} contract={expiry} {strike}{right} "
                f"source=SNAPSHOT bid={result['bid']} ask={result['ask']} "
                f"mid={result['mid']} marketDataType=RTH"
            )
            return result
        finally:
            try:
                self.cancelMktData(req_id)
            except Exception:
                pass
            # ✅ Cleanup temporary callback
            self._temporary_tick_callbacks.pop(req_id, None)

    def pre_conid(self, custom_order: Order) -> bool:
        """
        Pre-resolve conId BEFORE order placement.
        Useful for pre-market where we want everything ready.
        """
        logging.info(f"[TWSSwervice] doing pre-conid for order: {custom_order}")
        try:
            key = (
                custom_order.symbol.upper(),
                custom_order.expiry,
                float(custom_order.strike),
                custom_order.right.upper(),
            )

            # 1. Already cached?
            if key in self._pre_conid_cache:
                conid = self._pre_conid_cache[key]
                custom_order._pre_conid = conid
                logging.info(f"[TWSService] pre_conid CACHE HIT {key} → {conid}")
                return True

            # 2. Build contract
            contract = self.create_option_contract(
                symbol=custom_order.symbol,
                last_trade_date=custom_order.expiry,
                strike=custom_order.strike,
                right=custom_order.right,
            )

            # 3. Resolve it
            conid = self.resolve_conid(contract)
            if not conid:
                logging.error(f"[TWSService] pre_conid FAILED {key}")
                return False

            # 4. Save to cache
            self._pre_conid_cache[key] = conid
            custom_order._pre_conid = conid

            logging.info(f"[TWSService] pre_conid READY {key} → {conid}")
            return True

        except Exception as e:
            logging.error(f"[TWSService] pre_conid ERROR {e}")


            return False
        

    
    def place_custom_order(self, custom_order, account="") -> bool:
        with ORDER_LOCK:                     # wait your turn
            while self.next_valid_order_id is None:
                time.sleep(0.1)              # don’t move until TWS gives us an ID
            return self._real_place_custom_order(custom_order, account)   # old logic
            

    def _real_place_custom_order(self, custom_order, account="") -> bool:
    # paste your old place_custom_order code here (nothing deleted)
        """
        Place an order using your custom Order object from Helpers.Order.
        """
        logging.info(f"[TWSService] place_custom_order – order_id={custom_order.order_id}")
        if not self.is_connected():
            logging.error(f"Cannot place order: Not connected to TWS")
            return False

        try:
            # Convert your custom order to IB contract

            logging.info(
                "[ORDER_INTENT] "
                f"ts={time.time()*1000:.0f} order_id={custom_order.order_id} "
                f"symbol={custom_order.symbol} expiry={custom_order.expiry} "
                f"strike={custom_order.strike} right={custom_order.right} action=BUY "
                f"pos_usd={getattr(custom_order, '_position_size', None)}"
            )

            ib_right = "C" if custom_order.right.upper() in ["C", "CALL"] else "P"
            
            contract = self.create_option_contract(
                symbol=custom_order.symbol,
                last_trade_date=custom_order.expiry,
                strike=custom_order.strike,
                right=ib_right,
                exchange="SMART",
                currency="USD"
            )

            # ✅ Resolve contract to avoid error 200
            key = (
                custom_order.symbol.upper(),
                custom_order.expiry,
                float(custom_order.strike),
                custom_order.right.upper(),
            )
            precon = self._pre_conid_cache.get(key)
            if precon:
                conid = precon 
            else:
                conid = self.resolve_conid(contract)
            if not conid:
                logging.error(f"Could not resolve contract for {custom_order.symbol} {custom_order.expiry} {custom_order.strike}{ib_right}")
                custom_order.mark_failed("Contract resolution failed")
                return False

            contract.conId = conid

            # --- Premium snapshot ---
            # NOTE: We use get_option_premium now for a robust (Polygon fallback) price
            #premium = self.get_option_premium(custom_order.symbol, custom_order.expiry, custom_order.strike, ib_right)
            #if not premium or premium <= 0:
                #raise RuntimeError(f"No live premium for {custom_order.symbol} {custom_order.expiry} {custom_order.strike}{ib_right}")

            base_price = custom_order.entry_price #or premium
            
            # ✅ SAFETY CHECK: Ensure entry_price is set
            if base_price is None or base_price <= 0:
                error_msg = f"Order {custom_order.order_id} has invalid entry_price: {base_price}. Order must be finalized first."
                logging.error(f"[TWSService] {error_msg}")
                raise ValueError(error_msg)

            # ✅ FIXED QTY CALC
            if getattr(custom_order, "_position_size", None):
                qty = custom_order.calc_contracts_from_premium(base_price)
            else:
                # fallback to manually set qty (legacy behavior)
                qty = custom_order.qty if getattr(custom_order, "qty", None) else 1

            logging.info(
            "[SIZE_INTENT] "
            f"ts={time.time()*1000:.0f} order_id={custom_order.order_id} "
            f"symbol={custom_order.symbol} pos_usd={custom_order._position_size} "
            f"premium_used={base_price} calculated_qty={qty} "
            f"rounding=floor risk_cap=none"
            )


            #Safety clamp
            notional = qty * base_price * 100
            if notional > custom_order._position_size *1.5:
                 logging.error(
                    "[ORDER_BUILD] "
                    f"ts={time.time()*1000:.0f} order_id={custom_order.order_id} "
                    f"requested_qty={qty} final_qty=0 mutation=YES "
                    f"mutation_reason=RISK_CAP_MAX_QTY"
                )

                 return False
            custom_order.qty = qty

            # Debug info
            logging.info(
                f"[TWSService] Calculated qty={qty} for {custom_order.symbol} "
                f"premium={base_price}, position_size={getattr(custom_order, '_position_size', None)}"
            )

            # --- Build IB order ---
            closing = custom_order.action == "SELL"
            # NOTE: to_ib_order needs to be updated to support outside_rth for pre-market orders
            ib_order = custom_order.to_ib_order(
                order_type=custom_order.type,
                limit_price=custom_order.entry_price,
                transmit=True,
                closing=closing
            )
            ib_order.account = account

            ib_order_id = self.next_valid_order_id
            custom_order._ib_order_id = ib_order_id
            self._ib_to_order_id[ib_order_id] = custom_order.order_id
            # NEW: also map IB id -> custom UUID for orderStatus
            self._ib_to_custom_id[ib_order_id] = custom_order.order_id

            self._positions_by_order_id[custom_order.order_id] = {
                "qty": 0,
                "avg_price": 0.0,
                "symbol": custom_order.symbol,
                "expiry": custom_order.expiry,
                "strike": custom_order.strike,
                "right": custom_order.right,
            }
            logging.info(
                            "[ORDER_BUILD] "
                            f"ts={time.time()*1000:.0f} order_id={custom_order.order_id} "
                            f"requested_qty={qty} final_qty={custom_order.qty} "
                            f"mutation={'YES' if qty != custom_order.qty else 'NO'} "
                            f"mutation_reason={'NONE' if qty == custom_order.qty else 'UNKNOWN'}"
                        )
            self._pending_orders[custom_order.order_id] = custom_order
            

            self.placeOrder(ib_order_id, contract, ib_order)
            custom_order._placed_ts = time.time() * 1000
            logging.info(
            "[ORDER_SENT] "
            f"ts={custom_order._placed_ts:.0f} order_id={custom_order.order_id} "
            f"ib_order_id={ib_order_id} qty={custom_order.qty}"
        )


            logging.info(f"[TWSService] Sent order {custom_order.symbol} IBID={ib_order_id} "
                        f"at {custom_order._placed_ts:.0f} ms")
            logging.info(f"Placed custom order: {custom_order.order_id} -> IB ID: {ib_order_id}")

            # Increment order ID for next use
            self.next_valid_order_id += 1
            return True

        except Exception as e:
            logging.error(f"Failed to place custom order {custom_order.order_id}: {str(e)}")
            custom_order.mark_failed(reason=str(e))
            return False

    def cancel_custom_order(self, custom_order_id: str) -> bool:
        """Cancel a custom order"""
        logging.info(f"[TWSService] cancel_custom_order – {custom_order_id}")
        if custom_order_id in self._pending_orders:
            order = self._pending_orders[custom_order_id]
            if hasattr(order, '_ib_order_id'):
                self.cancelOrder(order._ib_order_id)
                order.mark_cancelled()
                logging.info(f"Cancelled order {custom_order_id}")
                return True
        logging.info(f"[TWSService] cancel_custom_order – no action taken for {custom_order_id}")
        return False

    def get_order_status(self, custom_order_id: str) -> Optional[Dict]:
        """
        Get the status of a custom order.
        """
        logging.info(f"[TWSService] get_order_status – {custom_order_id}")
        if custom_order_id in self._pending_orders:
            order = self._pending_orders[custom_order_id]
            return order.to_dict()
        return None
    def disconnect_gracefully(self):
        logging.info("Disconnecting from TWS...")
        self.connection_ready.clear()
        self.disconnect()
    
    def sell_custom_order(self, custom_order: Order, contract : Contract, account: str = "", ) -> bool:
        """
        Dedicated SELL method for option orders.
        - Uses the correct closing quantity if a position ID is provided (from the StopLoss Watcher).
        - Otherwise, dynamically recalculates quantity from live premium if position_size is set.
        """
        logging.info(f"[TWSService] sell_custom_order – {custom_order.order_id}")
        if not self.is_connected():
            logging.error("Cannot place SELL order: Not connected to TWS")
            return False

        try:
            custom_order.action = "SELL"
            ib_right = "C" if custom_order.right.upper() in ["C", "CALL"] else "P"
            
            # --- FIX START: Ensure Quantity for Closing Order is NOT Recalculated ---
            # A closing order should already have custom_order.qty set to the position size 
            # by sell_position_by_order_id, so we skip recalculation.
            is_closing_position = getattr(custom_order, "previous_id", None) and custom_order.qty > 0

            if not is_closing_position:
                # Original dynamic sizing logic for non-closing orders (if intentionally opening new short or complex position)
                premium = self.get_option_premium(custom_order.symbol, custom_order.expiry, custom_order.strike, ib_right)
                if not premium or premium <= 0:
                    raise RuntimeError(f"No live premium for {custom_order.symbol} {custom_order.expiry} {custom_order.strike}{ib_right}")

                if getattr(custom_order, "_position_size", None):
                    qty = custom_order.calc_contracts_from_premium(premium)
                    custom_order.qty = qty
                elif not getattr(custom_order, "qty", None):
                    raise RuntimeError("SELL order has neither qty nor position_size set")
            
            # --- FIX END ---


            # Build IB order
            ib_order = custom_order.to_ib_order(
                order_type=custom_order.type,
                limit_price=custom_order.entry_price,
                transmit=True
            )
            ib_order.account = account

            order_id = self.next_valid_order_id
            custom_order._ib_order_id = order_id
            self._pending_orders[custom_order.order_id] = custom_order
            
            # --- Link SELL IB ID to BUY UUID ---
            buy_order_uuid = custom_order.previous_id
            if buy_order_uuid and buy_order_uuid in self._positions_by_order_id:
                # Link the new SELL IB ID to the original BUY custom UUID
                self._ib_to_order_id[order_id] = buy_order_uuid
                logging.info(f"[TWSService] Linked SELL IBID {order_id} to BUY Position UUID {buy_order_uuid}")
            else:
                # Fallback: link to its own ID if position is not found
                logging.warning(f"[TWSService] No BUY position found for {buy_order_uuid}. Linking SELL to itself.")
                self._ib_to_order_id[order_id] = custom_order.order_id


            self.placeOrder(order_id, contract, ib_order)
            custom_order._placed_ts = time.time() * 1000
            logging.info(
                f"[TWSService] SELL placed: {custom_order.symbol} {custom_order.expiry} "
                f"{custom_order.strike}{ib_right} x{custom_order.qty} @ {custom_order.entry_price} "
                f"→ ID {order_id}"
            )

            self.next_valid_order_id += 1
            return True

        except Exception as e:
            logging.error(f"[TWSService] Failed to sell order {custom_order.order_id}: {e}")
            custom_order.mark_failed(reason=str(e))
            return False


    def get_position_by_order_id(self, order_id: str):
        logging.info(f"[TWSService] get_position_by_order_id – {order_id}")
        return self._positions_by_order_id.get(order_id)

    def has_position(self, order_id_or_symbol: str) -> bool:
        logging.info(f"[TWSService] has_position – query={order_id_or_symbol}")
        # check by UUID first
        pos = self._positions_by_order_id.get(order_id_or_symbol)
        if pos and pos["qty"] > 0:
            return True
        # fallback by symbol
        if hasattr(self, "_positions_by_symbol"):
            uuid = self._positions_by_symbol.get(order_id_or_symbol)
            if uuid:
                pos = self._positions_by_order_id.get(uuid)
                return bool(pos and pos["qty"] > 0)
        return False


    def sell_position_by_order_id(self, order_id: str, contract : Contract, qty: int | None = None,
                              limit_price: float | None = None, account: str = "", ex_order: Optional[Order] = None) -> bool:
        logging.info(f"[TWSService] sell_position_by_order_id – order_id={order_id} qty={qty}")
        pos = self._positions_by_order_id.get(order_id)
        if not pos or pos["qty"] <= 0:
            logging.warning(f"[TWSService] sell_position_by_order_id: no live position for {order_id}")
            logging.warning(f"The Position {pos}")
            logging.warning(f"the dict {self._positions_by_order_id}")
            return False

        sell_qty = qty or pos["qty"]

        ex_order.symbol = pos["symbol"]
        ex_order.expiry = pos["expiry"]
        ex_order.strike = pos["strike"]
        ex_order.right = pos["right"]
        ex_order.qty  = sell_qty
        
        # 💡 FIX: Set the previous_id on the exit order so sell_custom_order can link it
        ex_order.previous_id = order_id 
        
        # 💡 FIX: Ensure the exit order has the correct limit price if it's LMT (or None if MKT)
        if limit_price is not None:
             ex_order.entry_price = limit_price
        else:
             ex_order.entry_price = ex_order.entry_price # Keep existing for MKT reference

        ok = self.sell_custom_order(ex_order, contract, account=account)
        if ok:
            logging.info(f"[TWSService] SELL order submitted for {order_id}, waiting for fill confirmation.")
            # 🔧 Do NOT modify qty here; handled in orderStatus/execDetails

        return ok


    def get_option_premium(self, symbol: str, expiry: str, strike: float, right: str, timeout: int = 3) -> Optional[float]:
        """
        Live premium for a *single* option contract.
        Uses ONLY IBKR TWS - no Polygon fallbacks.
        Returns None if TWS is unavailable or market is closed/pre-market.
        """
        logging.info(f"[TWSService] get_option_premium – {symbol} {expiry} {strike}{right}")
        
        # --- Only use IBKR TWS ---
        if not self.is_connected():
            logging.warning("[TWSService] Not connected to TWS; cannot fetch option premium.")
            return None

        # --- IBKR Request Logic ---
        contract = self.create_option_contract(symbol, expiry, strike, right)
        conid = self.resolve_conid(contract)
        if not conid:
            logging.error(f"[TWSService] Failed to resolve conId for {symbol} {expiry} {strike}{right}")
            return None

        contract.conId = conid
        req_id = self._get_next_req_id()
        tick_snapshot = {"bid": None, "ask": None}
        event = threading.Event()

        def tickPrice_callback(reqId, tickType, price, attrib):
            if reqId != req_id or price <= 0:
                return
            if tickType == 1:
                tick_snapshot["bid"] = price
            elif tickType == 2:
                tick_snapshot["ask"] = price
            if tick_snapshot["bid"] is not None and tick_snapshot["ask"] is not None:
                event.set()

        # ✅ Use temporary callback dict instead of overriding tickPrice
        self._temporary_tick_callbacks[req_id] = tickPrice_callback

        try:
            self.reqMktData(req_id, contract, "", True, False, [])
            event.wait(timeout)
            bid = tick_snapshot["bid"]
            ask = tick_snapshot["ask"]
            mid = (bid + ask) / 2 if (bid and ask) else bid or ask
            if mid:
                logging.info(f"[TWSService] Premium snapshot for {symbol} {expiry} {strike}{right}: bid={bid}, ask={ask}, mid={mid}")
                return mid

            # --- No fallback - return None if TWS fails ---
            logging.warning(f"[TWSService] No IBKR premium for {symbol} {expiry} {strike}{right}")
            return None
        finally:
            try:
                self.cancelMktData(req_id)
            except Exception:
                pass
            # ✅ Cleanup temporary callback
            self._temporary_tick_callbacks.pop(req_id, None)

service = TWSService()
logging.info("[TWSService] module-level service instance created")


def create_tws_service() -> TWSService:
    logging.info("[TWSService] create_tws_service() called – returning singleton")
    return service




# Test the service
if __name__ == "__main__":
    print("Testing TWSService with Helpers.Order integration")
    
    # Import YOUR Order class
    from Helpers.Order import Order
    
    service = create_tws_service()
    
    if service.connect_and_start(port=7497):
        print("✓ Connected to TWS")
        
        try:
            # Test data retrieval
            print("Testing option chain data...")
            maturities = service.get_maturities("SPY")
            if maturities:
                print(f"✓ Got {len(maturities['expirations'])} expirations")
            
            # Test with YOUR Order class
            print("Testing with Helpers.Order...")
            my_order = Order(
                symbol="SPY",
                expiry="20241220",
                strike=450.0,
                right="CALL",
                qty=1,
                entry_price=2.50,
                tp_price=5.00,
                sl_price=1.00
            )
            
            print(f"✓ Created Helpers.Order: {my_order.order_id}")
            print(f"  {my_order.symbol} {my_order.expiry} {my_order.strike}{my_order.right}")
            
            # Order placement ready (commented for safety)
            print("✓ Order system integrated and ready")
            print("Uncomment to test order placement:")
            print("# service.place_custom_order(my_order)")
            
        except Exception as e:
            print(f"Error: {e}")
        
        service.disconnect_gracefully()
        print("✓ Disconnected")