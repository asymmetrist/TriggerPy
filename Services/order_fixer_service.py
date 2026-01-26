import threading
import queue
import time
import logging
from typing import Set

from Helpers.Order import Order, OrderState
#from model import general_app
from Services.polygon_service import polygon_service
from Services.tws_service import create_tws_service, TWSService
from Services.order_wait_service import wait_service

class OrderFixerService:
    """
    Background service that incrementally fixes / completes Order requirements.

    Design goals:
    - Async, non-blocking
    - Idempotent (safe to call many times)
    - Single source of truth for order readiness
    - Can be invoked from anywhere in runtime
    """

    def __init__(self, tws: TWSService = create_tws_service()):
        self._queue: queue.Queue[Order] = queue.Queue()
        self._active: Set[str] = set()   # order_id currently being fixed
        self._lock = threading.Lock()
        self._running = True
        self.tws = tws
        self._worker = threading.Thread(
            target=self._run,
            name="OrderFixerService",
            daemon=True
        )
        self._worker.start()

        logging.info("[OrderFixerService] Initialized and worker started")

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------

    def fix_async(self, order: Order):
        """
        Schedule an order to be fixed in background.
        Safe to call multiple times.
        """
        if not order:
            return

        if order.state in (OrderState.FINALIZED, OrderState.CANCELLED):
            return

        with self._lock:
            if order.order_id in self._active:
                return
            self._active.add(order.order_id)

        logging.debug(f"[OrderFixerService] Queued order {order.order_id} for fixing")
        self._queue.put(order)

    def ensure(self, order: Order, timeout: float = 5.0) -> bool:
        """
        Blocking helper: wait until order becomes FIXED or timeout expires.
        """
        self.fix_async(order)
        start = time.time()
        while time.time() - start < timeout:
            if self.is_ready(order):
                return True
            time.sleep(0.05)
        return False

    def is_ready(self, order: Order) -> bool:
        """
        Returns True if order is fully fixed and ready for execution.
        """
        return bool(getattr(order, "_order_ready", False))

    def stop(self):
        self._running = False
        logging.info("[OrderFixerService] Stopping service")

    # ------------------------------------------------------------------
    # WORKER LOOP
    # ------------------------------------------------------------------

    def _run(self):
        while self._running:
            try:
                order: Order = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self._fix_order(order)
            except Exception as e:
                logging.error(
                    f"[OrderFixerService] Fatal error while fixing order "
                    f"{getattr(order, 'order_id', '?')}: {e}"
                )
            finally:
                with self._lock:
                    self._active.discard(order.order_id)

    # ------------------------------------------------------------------
    # CORE LOGIC
    # ------------------------------------------------------------------

    def _fix_order(self, order: Order):
        """
        Incrementally fix missing order requirements.
        Never submits orders. Never mutates strategy intent.
        """

        if order.state in (OrderState.FINALIZED, OrderState.CANCELLED):
            return

        # If already ready, nothing to do
        if getattr(order, "_order_ready", False):
            return

        changed = False

        # ----------------------------
        # 1. Underlying price (optional, cached)
        # ----------------------------
        if getattr(order, "underlying_price", None) is None:
            try:
                
                price = polygon_service.get_last_trade(order.symbol)
                if price and price > 0:
                    order.underlying_price = price
                    changed = True
            except Exception:
                pass

        # ----------------------------
        # 2. Option premium (stream only - no snapshot fallback)
        # ----------------------------
        if getattr(order, "premium", None) is None:
            try:
                # ✅ Only use streamed premium (0ms latency)
                # If stream not ready yet, skip - pipeline will handle it in _finalize_order()
                streamed_premium = wait_service.get_streamed_premium(order)
                if streamed_premium and streamed_premium > 0:
                    order.premium = streamed_premium
                    changed = True
                    logging.debug(
                        f"[OrderFixerService] ✅ Using streamed premium for {order.symbol} "
                        f"{order.expiry} {order.strike}{order.right}: {streamed_premium}"
                    )
                else:
                    # Stream not ready yet - that's OK, pipeline will fetch it at trigger time
                    logging.debug(
                        f"[OrderFixerService] Stream not ready yet for {order.symbol} "
                        f"{order.expiry} {order.strike}{order.right} - pipeline will handle"
                    )
            except Exception as e:
                logging.warning(f"[OrderFixerService] Error checking streamed premium: {e}")
                pass

        # ----------------------------
        # 3. Quantity calculation
        # ----------------------------
        if not getattr(order, "qty", None):
            try:
                if getattr(order, "_position_size", None) and order.premium:
                    qty = int(order._position_size // order.premium)
                    if qty > 0:
                        order.qty = qty
                        changed = True
            except Exception:
                pass

        # ----------------------------
        # 4. ConId resolution (cached)
        # ----------------------------
        if not getattr(order, "_pre_conid", None):
            try:
                ok = self.tws.pre_conid(order)
                if ok:
                    changed = True
            except Exception:
                pass

        # ----------------------------
        # 5. Final readiness check
        # ----------------------------
        ready = all([
            getattr(order, "expiry", None),
            getattr(order, "strike", None),
            getattr(order, "right", None),
            getattr(order, "qty", None),
            getattr(order, "_pre_conid", None),
        ])

        if ready:
            order._order_ready = True
            order.state = OrderState.PENDING
            logging.info(
                f"[OrderFixerService] Order {order.order_id} FIXED "
                f"(qty={order.qty}, premium={getattr(order, 'premium', None)})"
            )
        else:
            # Not ready yet → requeue gently
            if changed:
                time.sleep(0.05)
            self.fix_async(order)


# ----------------------------------------------------------------------
# SINGLETON EXPORT
# ----------------------------------------------------------------------

order_fixer = OrderFixerService()
