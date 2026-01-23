"""
TWS Mock Service - Simulates TWS order placement for testing
Completely optional - only used when order_mode="simulate"
"""
import logging
import threading
from typing import Optional, Dict
from Helpers.Order import Order, OrderState


class TWSMockService:
    """
    Mock TWS service that simulates order placement without real TWS connection.
    Useful for testing order logic without risking real trades.
    """
    
    def __init__(self):
        self.mock_orders = {}  # order_id -> order
        self.next_order_id = 1000
        self._lock = threading.Lock()
    
    def place_order(self, order: Order) -> bool:
        """
        Simulate placing an order.
        
        Args:
            order: Order object to place
            
        Returns:
            True if order was "placed" successfully
        """
        with self._lock:
            order_id = self.next_order_id
            self.next_order_id += 1
            
            # Store mock order
            order._ib_order_id = order_id
            self.mock_orders[order.order_id] = {
                "order": order,
                "ib_order_id": order_id,
                "status": "Submitted",
                "filled": 0,
                "remaining": order.qty
            }
            
            logging.info(
                f"[TWSMock] 📝 Simulated order placement: "
                f"order_id={order.order_id} ib_order_id={order_id} "
                f"{order.symbol} {order.action} {order.qty} @ {order.entry_price}"
            )
            
            # Simulate immediate fill (for testing)
            # In real scenario, you might want to simulate delays
            if order.type == "MKT":
                # Market orders fill immediately
                self._simulate_fill(order, order.qty)
            elif order.type == "LMT":
                # Limit orders might fill if price is favorable
                # For now, we'll simulate a fill after a short delay
                threading.Timer(0.5, lambda: self._simulate_fill(order, order.qty)).start()
            
            return True
    
    def _simulate_fill(self, order: Order, qty: int):
        """Simulate order fill."""
        with self._lock:
            if order.order_id not in self.mock_orders:
                return
            
            mock_order = self.mock_orders[order.order_id]
            mock_order["filled"] = qty
            mock_order["remaining"] = 0
            mock_order["status"] = "Filled"
            
            order.mark_active(result=f"Mock IB Order ID: {mock_order['ib_order_id']}")
            order.mark_filled(filled_qty=qty, avg_price=order.entry_price)
            
            logging.info(
                f"[TWSMock] ✅ Simulated fill: order_id={order.order_id} "
                f"qty={qty} @ ${order.entry_price:.2f}"
            )
    
    def cancel_order(self, order_id: str) -> bool:
        """Simulate order cancellation."""
        with self._lock:
            if order_id in self.mock_orders:
                mock_order = self.mock_orders[order_id]
                mock_order["status"] = "Cancelled"
                logging.info(f"[TWSMock] 🚫 Simulated cancel: order_id={order_id}")
                return True
            return False
    
    def get_order_status(self, order_id: str) -> Optional[Dict]:
        """Get mock order status."""
        with self._lock:
            return self.mock_orders.get(order_id)
