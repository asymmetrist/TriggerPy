"""
Order Pipeline State Pattern Implementation

Manages the order finalization pipeline as a state machine:
1. Pending → PremiumFetching → Calculating → Placing → WaitingForFill → Finalized
"""

import time
import logging
from typing import Optional
from abc import ABC, abstractmethod
from enum import Enum
from Helpers.Order import Order, OrderState
from Services.order_manager import order_manager
from Services.watcher_info import (
    ThreadInfo, watcher_info,
    STATUS_PENDING, STATUS_RUNNING, STATUS_FINALIZED, STATUS_FAILED
)
from Services.nasdaq_info import is_market_closed_or_pre_market


class PipelineState(Enum):
    """Pipeline execution states"""
    PENDING = "pending"
    PREMIUM_FETCHING = "premium_fetching"
    CALCULATING = "calculating"
    PLACING = "placing"
    WAITING_FOR_FILL = "waiting_for_fill"
    FINALIZED = "finalized"
    FAILED = "failed"


class OrderPipelineState(ABC):
    """Base class for order pipeline states"""
    
    def __init__(self, order: Order, context: 'OrderPipelineContext'):
        self.order = order
        self.context = context
        self.order_id = order.order_id
    
    @abstractmethod
    def execute(self) -> Optional['OrderPipelineState']:
        """
        Execute the state's logic.
        Returns: Next state to transition to, or None if staying in current state
        """
        pass
    
    @abstractmethod
    def get_state_name(self) -> str:
        """Return human-readable state name"""
        pass
    
    def log(self, message: str, level: str = "info"):
        """Helper for logging with order context"""
        log_func = getattr(logging, level.lower(), logging.info)
        log_func(f"[Pipeline:{self.get_state_name()}] {message} | order_id={self.order_id}")


class PendingState(OrderPipelineState):
    """Initial state - order is pending finalization"""
    
    def execute(self) -> Optional[OrderPipelineState]:
        self.log("Order pending finalization")
        
        # Check if we're in RTH
        if is_market_closed_or_pre_market():
            error_msg = "Cannot finalize order in premarket"
            self.log(error_msg, "error")
            self.order.mark_failed(error_msg)
            return FailedState(self.order, self.context, error_msg)
        
        # Check if entry_price is already set
        if self.order.entry_price and self.order.entry_price > 0:
            self.log("Entry price already set, skipping to placing")
            return PlacingState(self.order, self.context)
        
        # Need to fetch premium and calculate
        return PremiumFetchingState(self.order, self.context)
    
    def get_state_name(self) -> str:
        return "PENDING"


class PremiumFetchingState(OrderPipelineState):
    """Fetch premium from stream or cache"""
    
    def __init__(self, order: Order, context: 'OrderPipelineContext', retry_count: int = 0):
        super().__init__(order, context)
        self.retry_count = retry_count
        self.max_retries = 3
        self.max_wait = 1.0  # seconds
        self.wait_interval = 0.05
    
    def execute(self) -> Optional[OrderPipelineState]:
        self.log(f"Fetching premium (attempt {self.retry_count + 1}/{self.max_retries})")
        
        # Try to get premium from order (set by OrderFixerService)
        premium = getattr(self.order, "premium", None)
        
        # If no premium, try stream
        if not premium or premium <= 0:
            premium = self.context.wait_service.get_streamed_premium(self.order)
        
        # If still no premium, wait for stream
        if not premium or premium <= 0:
            self.log("Premium not available from stream, waiting...")
            waited = 0
            
            while waited < self.max_wait:
                premium = self.context.wait_service.get_streamed_premium(self.order)
                if premium and premium > 0:
                    self.log(f"✅ Premium received from stream after {waited:.2f}s | premium={premium}")
                    break
                time.sleep(self.wait_interval)
                waited += self.wait_interval
            
            if not premium or premium <= 0:
                if self.retry_count < self.max_retries - 1:
                    self.log(f"Premium not available, retrying... (retry {self.retry_count + 1}/{self.max_retries})")
                    return PremiumFetchingState(self.order, self.context, self.retry_count + 1)
                else:
                    error_msg = f"Streamed premium not available after {self.max_wait}s wait and {self.max_retries} retries"
                    self.log(error_msg, "error")
                    self.order.mark_failed(error_msg)
                    return FailedState(self.order, self.context, error_msg)
        
        # Store premium for next state
        self.context.premium = premium
        return CalculatingState(self.order, self.context)
    
    def get_state_name(self) -> str:
        return "PREMIUM_FETCHING"


class CalculatingState(OrderPipelineState):
    """Calculate entry_price, qty, SL/TP from premium"""
    
    def execute(self) -> Optional[OrderPipelineState]:
        self.log("Calculating order parameters from premium")
        
        premium = self.context.premium
        if not premium or premium <= 0:
            error_msg = "Premium not available for calculation"
            self.log(error_msg, "error")
            self.order.mark_failed(error_msg)
            return FailedState(self.order, self.context, error_msg)
        
        # Get model and args
        model = getattr(self.order, "_model", None)
        _args = getattr(self.order, "_args", {})
        arcTick = _args.get("arcTick", 0.01)
        position_size = getattr(self.order, "_position_size", _args.get("position", 2000))
        
        # Recalculate SL/TP based on actual premium (if they were placeholders)
        if self.order.sl_price == 0.5 or self.order.tp_price == 1.2:  # Placeholder values
            self.order.sl_price = round(premium * 0.8, 2)
            self.order.tp_price = round(premium * 1.2, 2)
            self.log(f"Recalculated SL/TP: SL={self.order.sl_price} TP={self.order.tp_price} from premium={premium}")
        
        # Calculate entry_price from premium + arcTick
        mid = premium + arcTick
        if mid < 3:
            tick = 0.01
        elif mid >= 5:
            tick = 0.15
        else:
            tick = 0.05
        
        entry_price = round(int(mid / tick) * tick, 2)
        
        # Update order with calculated values
        self.order.entry_price = entry_price
        
        # Update qty if not set
        if not self.order.qty or self.order.qty <= 0:
            qty = int(position_size // premium) if premium > 0 else 1
            if qty <= 0:
                qty = 1
            self.order.qty = qty
            self.log(f"Calculated qty={qty} from premium={premium}")
        
        # Set SL/TP if not already set (fallback to model defaults)
        if not self.order.sl_price:
            if model and hasattr(model, "_stop_loss") and model._stop_loss:
                self.order.sl_price = model._stop_loss
            else:
                self.order.sl_price = round(entry_price * 0.8, 2)
        
        if not self.order.tp_price:
            if model and hasattr(model, "_take_profit") and model._take_profit:
                self.order.tp_price = model._take_profit
            else:
                self.order.tp_price = round(entry_price * 1.2, 2)
        
        self.log(
            f"Order prepared | premium={premium} | entry_price={entry_price} | "
            f"qty={self.order.qty} | SL={self.order.sl_price} | TP={self.order.tp_price}"
        )
        
        return PlacingState(self.order, self.context)
    
    def get_state_name(self) -> str:
        return "CALCULATING"


class PlacingState(OrderPipelineState):
    """Place order with TWS"""
    
    def execute(self) -> Optional[OrderPipelineState]:
        # Final safety check
        if not self.order.entry_price or self.order.entry_price <= 0:
            error_msg = f"Entry price is not set ({self.order.entry_price}). Cannot place order."
            self.log(error_msg, "error")
            self.order.mark_failed(error_msg)
            return FailedState(self.order, self.context, error_msg)
        
        self.log(
            f"Placing order | entry_price={self.order.entry_price} | qty={self.order.qty} | "
            f"symbol={self.order.symbol} {self.order.right}{self.order.strike}"
        )
        
        try:
            start_ts = time.time() * 1000
            logging.info(
                f"[TWS-LATENCY] {self.order.symbol} Trigger hit → sending ENTRY order "
                f"({self.order.right}{self.order.strike}) at {start_ts:.0f} ms | "
                f"entry_price={self.order.entry_price} | qty={self.order.qty}"
            )
            
            success = self.context.tws.place_custom_order(self.order)
            
            if success:
                end_ts = time.time() * 1000
                latency = end_ts - start_ts
                logging.info(
                    f"[TWS-LATENCY] {self.order.symbol} Order sent in {latency:.1f} ms "
                    f"(start {start_ts:.0f} → end {end_ts:.0f})"
                )
                
                self.order.mark_active(result=f"IB Order ID: {self.order._ib_order_id}")
                
                # Update UI callback
                if getattr(self.order, "_status_callback", None):
                    try:
                        self.order._status_callback(f"Finalized: {self.order.symbol} {self.order.order_id}", "green")
                    except Exception as e:
                        self.log(f"UI callback failed: {e}", "error")
                
                return WaitingForFillState(self.order, self.context)
            else:
                error_msg = "Failed to place order with TWS"
                self.log(error_msg, "error")
                self.order.mark_failed(error_msg)
                return FailedState(self.order, self.context, error_msg)
                
        except Exception as e:
            error_msg = f"Exception during order placement: {e}"
            self.log(error_msg, "error")
            self.order.mark_failed(error_msg)
            return FailedState(self.order, self.context, error_msg)
    
    def get_state_name(self) -> str:
        return "PLACING"


class WaitingForFillState(OrderPipelineState):
    """Wait for order fill confirmation"""
    
    def __init__(self, order: Order, context: 'OrderPipelineContext', timeout: float = 60.0):
        super().__init__(order, context)
        self.timeout = timeout
    
    def execute(self) -> Optional[OrderPipelineState]:
        self.log(f"Waiting for order fill (timeout={self.timeout}s)")
        
        if not hasattr(self.order, "_fill_event") or not self.order._fill_event:
            self.log("No fill event available, marking as finalized", "warning")
            return FinalizedState(self.order, self.context)
        
        filled = self.order._fill_event.wait(timeout=self.timeout)
        
        if filled and self.order.state == OrderState.FINALIZED:
            order_manager.add_finalized_order(self.order_id, self.order)
            self.log(f"Order filled and finalized → IB ID: {self.order._ib_order_id}")
            
            # Update watcher info
            watcher_info.update_watcher(self.order_id, STATUS_FINALIZED)
            if self.context.tinfo:
                self.context.tinfo.update_status(STATUS_FINALIZED)
            
            return FinalizedState(self.order, self.context)
        else:
            error_msg = f"Order not filled within {self.timeout}s timeout"
            self.log(error_msg, "warning")
            # Even if not filled, we mark the watcher as finalized if the order was sent
            if self.context.tinfo:
                self.context.tinfo.update_status(STATUS_FAILED, info={"error": "Fill event timed out"})
            return FinalizedState(self.order, self.context)  # Still finalize, order was sent
    
    def get_state_name(self) -> str:
        return "WAITING_FOR_FILL"


class FinalizedState(OrderPipelineState):
    """Order is finalized - start stop-loss watcher if needed"""
    
    def execute(self) -> Optional[OrderPipelineState]:
        self.log("Order finalized")
        
        # Start stop-loss watcher if configured
        if self.order.trigger or (self.order.sl_price and self.order.state == OrderState.FINALIZED):
            self._start_stop_loss_watcher()
        
        return None  # Terminal state
    
    def _start_stop_loss_watcher(self):
        """Start monitoring stop-loss level"""
        stop_loss_level = (
            self.order.trigger - self.order.sl_price 
            if self.order.right in ['C', 'CALL'] 
            else self.order.trigger + self.order.sl_price
        )
        
        exit_order = Order(
            symbol=self.order.symbol,
            expiry=self.order.expiry,
            strike=self.order.strike,
            right=self.order.right,
            qty=self.order.qty,
            entry_price=self.order.entry_price,  # keeps breakeven reference
            tp_price=None,
            sl_price=self.order.sl_price,
            action="SELL",
            type="MKT",  # Use MKT for guaranteed stop-loss exit
            trigger=None
        )
        
        exit_order.set_position_size(self.order._position_size)
        exit_order.previous_id = self.order.order_id
        exit_order.mark_active()
        
        self.log(f"Spawned EXIT watcher {exit_order.order_id} | stop={stop_loss_level} ({self.order.right})")
        
        self.context.wait_service.start_stop_loss_watcher(exit_order, stop_loss_level, mode="poll")
    
    def get_state_name(self) -> str:
        return "FINALIZED"


class FailedState(OrderPipelineState):
    """Order finalization failed"""
    
    def __init__(self, order: Order, context: 'OrderPipelineContext', error_msg: str):
        super().__init__(order, context)
        self.error_msg = error_msg
    
    def execute(self) -> Optional[OrderPipelineState]:
        self.log(f"Order failed: {self.error_msg}", "error")
        
        # Update watcher info
        watcher_info.update_watcher(self.order_id, STATUS_FAILED)
        if self.context.tinfo:
            self.context.tinfo.update_status(STATUS_FAILED, info={"error": self.error_msg})
        
        return None  # Terminal state
    
    def get_state_name(self) -> str:
        return "FAILED"


class OrderPipelineContext:
    """Context object that holds shared state and services for the pipeline"""
    
    def __init__(self, wait_service, tws, tinfo: Optional[ThreadInfo] = None):
        self.wait_service = wait_service
        self.tws = tws
        self.tinfo = tinfo
        self.premium: Optional[float] = None  # Cached premium value


class OrderPipeline:
    """Main pipeline orchestrator using State pattern"""
    
    def __init__(self, order: Order, wait_service, tws, tinfo: Optional[ThreadInfo] = None):
        self.order = order
        self.context = OrderPipelineContext(wait_service, tws, tinfo)
        self.current_state: Optional[OrderPipelineState] = PendingState(order, self.context)
        self.executed = False
    
    def execute(self) -> bool:
        """
        Execute the pipeline state machine.
        Returns: True if successful, False if failed
        """
        if self.executed:
            return self.current_state is None or isinstance(self.current_state, FinalizedState)
        
        self.executed = True
        
        while self.current_state is not None:
            try:
                next_state = self.current_state.execute()
                
                if next_state is None:
                    # Terminal state reached
                    break
                
                self.current_state = next_state
                
            except Exception as e:
                logging.exception(f"[Pipeline] Error in state {self.current_state.get_state_name()}: {e}")
                self.current_state = FailedState(self.order, self.context, str(e))
                self.current_state.execute()
                break
        
        # Check final state
        if isinstance(self.current_state, FinalizedState):
            return True
        elif isinstance(self.current_state, FailedState):
            return False
        else:
            # Shouldn't happen, but handle gracefully
            logging.warning(f"[Pipeline] Unexpected final state: {self.current_state}")
            return False
