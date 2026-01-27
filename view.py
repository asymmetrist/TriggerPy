import tkinter as tk
from tkinter import ttk
import logging
import threading
from typing import Optional, Callable

from model import general_app, get_model, AppModel
from Services import nasdaq_info
from Services.order_manager import order_manager
from Helpers.Order import OrderState
from Services.nasdaq_info import is_market_closed_or_pre_market
from Services.order_wait_service import wait_service
from Services.amo_service import amo, LOSS
# ---------------- Banner ----------------
class Banner(tk.Canvas):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, height=60, bg="black", highlightthickness=0, **kwargs)
        self.create_text(20, 30, anchor="w", text="ARCTRIGGER",
                         font=("Arial Black", 24, "bold"), fill="#A020F0")
        # Version number
        self.create_text(220, 30, anchor="w", text="MV 1.1",
                         font=("Arial", 12, "bold"), fill="#00FF00")

        self.connection_status = self.create_text(
            400, 30, anchor="w", text="🔴 DISCONNECTED", font=("Arial", 10), fill="red"
        )

        self.market_info = self.create_text(
            600, 30, anchor="w", text="Market info unavailable", font=("Arial", 10), fill="white"
        )

        self.update_market_info()

    def update_connection_status(self, connected: bool):
        status = "🟢 CONNECTED" if connected else "🔴 DISCONNECTED"
        color = "green" if connected else "red"
        self.itemconfig(self.connection_status, text=status, fill=color)

    def update_market_info(self):
        try:
            msg = nasdaq_info.market_status_string()
            self.itemconfig(self.market_info, text=msg, fill="white")
        except Exception as e:
            logging.error(f"Banner market info error: {e}")
        finally:
            self.after(30000, self.update_market_info)


# ------------------------------------------------------------------
#  SymbolSelector  –  NASDAQ / NYSE only  +  Shift-↑/Tab auto-pick
# ------------------------------------------------------------------
class SymbolSelector(ttk.Frame):
    """Combobox that searches symbols on a worker thread to avoid UI lag."""
    def __init__(self, parent, on_symbol_selected: Callable[[str], None], **kwargs):
        super().__init__(parent, **kwargs)
        self.on_symbol_selected_cb = on_symbol_selected

        ttk.Label(self, text="Stock").pack(side="left")
        self.combo_symbol = ttk.Combobox(self, width=20)
        self.combo_symbol.pack(side="left", padx=5)

        # NEW: accelerate keys
        self.combo_symbol.bind("<KeyRelease>", self._on_typed)
        self.combo_symbol.bind("<<ComboboxSelected>>", self._on_selected)
        self.combo_symbol.bind("<Shift-Up>", self._auto_select_first)
        self.combo_symbol.bind("<Tab>", self._auto_select_first)

        self.lbl_price = ttk.Label(self, text="Price: -")
        self.lbl_price.pack(side="left", padx=10)
        self.watcher = None

        self._search_req_id = 0
        self._search_lock = threading.Lock()

    # ----------------------------------------------------------
    #  1.  Filtered search worker  (NASDAQ / NYSE only)
    # ----------------------------------------------------------
    def _search_worker(self, query: str, req_id: int):
        try:
            raw = general_app.search_symbol(query) or []
            filtered = [
                r for r in raw
                if (r.get("primaryExchange") or "").upper() in {"NASDAQ", "NYSE"}
            ]
            values = [f"{r['symbol']} - {r['primaryExchange']}" for r in filtered]
        except Exception as e:
            logging.error(f"Symbol search error: {e}")
            values = []

        def apply():
            if req_id == self._search_req_id:
                self.combo_symbol["values"] = values
        self.after(0, apply)

    # ----------------------------------------------------------
    #  2.  Auto-select first hit on Shift-↑ or Tab
    # ----------------------------------------------------------
    def _auto_select_first(self, event=None):
        vals = self.combo_symbol["values"]
        if vals:
            self.combo_symbol.set(vals[0])
            self._on_selected()
        return "break"

    # ----------------------------------------------------------
    #  3.  Original typed search (unchanged logic)
    # ----------------------------------------------------------
    def _on_typed(self, event=None):
        query = self.combo_symbol.get().upper()
        if len(query) < 2:
            self.combo_symbol["values"] = ()
            return
        with self._search_lock:
            self._search_req_id += 1
            req_id = self._search_req_id
        threading.Thread(target=self._search_worker, args=(query, req_id), daemon=True).start()

    # ----------------------------------------------------------
    #  4.  Selection handler (unchanged)
    # ----------------------------------------------------------
    def _on_selected(self, event=None):
        selection = self.combo_symbol.get()
        if not selection:
            return
        symbol = selection.split(" - ")[0]
        logging.info(f"Symbol selected: {symbol}")
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self.watcher = general_app.watch_price(symbol, self._update_price)
        threading.Thread(target=self._price_worker, args=(symbol,), daemon=True).start()
        self.on_symbol_selected_cb(symbol)

    def _update_price(self, price):
        self.lbl_price.config(text=f"Price: {price}")

    def _price_worker(self, symbol: str):
        try:
            snap = general_app.get_snapshot(symbol)
            price_txt = "-"
            if snap and "last" in snap:
                current_price = float(snap["last"])
                price_txt = f"{current_price:.2f}"
        except Exception as e:
            logging.error(f"Price fetch error: {e}")
            price_txt = "-"
        self.after(0, lambda: self.lbl_price.config(text=f"Price: {price_txt}"))


# ---------------- Order Frame (threads for all blocking ops) ----------------
class OrderFrame(tk.Frame):
    def __init__(self, parent, order_id: int = 0, **kwargs):
        super().__init__(parent, relief="groove", borderwidth=2, padx=8, pady=8, **kwargs)
        self.order_id = order_id
        self.model: Optional[AppModel] = None # Explicitly type
        self.current_symbol: Optional[str] = None
        self._last_price = None   # cached last market price

        # request ids to prevent stale updates
        self._symbol_token = 0
        self._maturity_req_id = 0
        self._strike_req_id = 0
        self.is_finalized = False  # track if this frame's order finalized

        # --- Symbol selector ---
        symbol_frame = ttk.Frame(self)
        symbol_frame.grid(row=0, column=0, columnspan=9, sticky="ew", pady=5)
        self.symbol_selector = SymbolSelector(symbol_frame, self.on_symbol_selected)
        self.symbol_selector.pack(fill="x")

        # --- Trigger ---
        ttk.Label(self, text="Trigger").grid(row=1, column=0)
        self.entry_trigger = ttk.Entry(self, width=8)
        self.entry_trigger.grid(row=1, column=1, padx=5)
        self.entry_trigger.bind('<Up>',   lambda e: self._bump_trigger(+0.50))
        self.entry_trigger.bind('<Down>', lambda e: self._bump_trigger(-0.50))
        self.entry_trigger.bind('<Shift-Up>',   lambda e: self._bump_trigger(+0.10))
        self.entry_trigger.bind('<Shift-Down>', lambda e: self._bump_trigger(-0.10))

        # --- NEW TRIGGER BUTTONS ---
        self.btn_premarket = ttk.Button(self, text="P-Market", command=self._on_premarket_trigger, width=8, state="disabled")
        self.btn_premarket.grid(row=1, column=6, padx=2)
        self.btn_intraday = ttk.Button(self, text="Intraday", command=self._on_intraday_trigger, width=8, state="disabled")
        self.btn_intraday.grid(row=1, column=7, padx=2)
        # --- END NEW TRIGGER BUTTONS ---

        # --- Type + Order type ---
        self.var_type = tk.StringVar(value="CALL")
        ttk.Radiobutton(self, text="Call", variable=self.var_type, value="CALL").grid(row=1, column=2)
        ttk.Radiobutton(self, text="Put", variable=self.var_type, value="PUT").grid(row=1, column=3)
        # When CALL/PUT changes, repopulate strikes AND update model right
        self.var_type.trace_add("write", self._on_type_changed)

        # --- Order Type Toggle (LMT/MKT) ---
        ttk.Label(self, text="Order Type").grid(row=1, column=8)
        
        # Dynamic label that shows current selection
        self.var_is_lmt = tk.BooleanVar(value=False)  # Default to MKT (unchecked)
        self.lbl_order_type = ttk.Label(self, text="MKT", font=("Arial", 10, "bold"))
        self.lbl_order_type.grid(row=1, column=9, padx=(0, 2))
        
        # Checkbox to toggle between MKT (unchecked) and LMT (checked)
        self.chk_lmt_type = ttk.Checkbutton(
            self, 
            variable=self.var_is_lmt,
            command=self._update_order_type_label
        )
        self.chk_lmt_type.grid(row=1, column=10, padx=(0, 5), sticky="w")
        # --- END Order Type ---

        # --- Position Size ---
        ttk.Label(self, text="Position Size").grid(row=2, column=0)
        self.entry_pos_size = ttk.Entry(self, width=10)
        self.entry_pos_size.grid(row=2, column=1, padx=5)
        self.entry_pos_size.insert(0, "2000")

        # Bind events for manual typing
        self.entry_pos_size.bind("<KeyRelease>", lambda e: self.recalc_quantity())
        self.entry_pos_size.bind("<FocusOut>", lambda e: self.recalc_quantity())

        frame_pos_btns = ttk.Frame(self)
        frame_pos_btns.grid(row=3, column=0, columnspan=2, pady=2)
        for val in [ 100, 500, 1000, 2000]:
            ttk.Button(
                frame_pos_btns, text=str(val),
                command=lambda v=val: self._set_pos_and_recalc(v)
            ).pack(side="left", padx=2)

        # --- Maturity ---
        ttk.Label(self, text="Maturity").grid(row=2, column=2)
        self.combo_maturity = ttk.Combobox(self, width=10, state="readonly")
        self.combo_maturity.grid(row=2, column=3, padx=5)
        self.combo_maturity.bind("<<ComboboxSelected>>", self.on_maturity_selected)
        ttk.Label(self, text="Strike").grid(row=2, column=4)
        self.combo_strike = ttk.Combobox(self, width=8, state="disabled")
        self.combo_strike.grid(row=2, column=5, padx=5)

        # --- Qty + SL + TP ---
        ttk.Label(self, text="Qty").grid(row=3, column=2)
        self.entry_qty = ttk.Entry(self, width=8)
        self.entry_qty.grid(row=3, column=3, padx=5)
        self.entry_qty.insert(0, "1")

        # --- Stop Loss ---
        ttk.Label(self, text="Stop Loss").grid(row=3, column=4)
        self.entry_sl = ttk.Entry(self, width=8)
        self.entry_sl.grid(row=3, column=5, padx=5)

        frame_sl_btns = ttk.Frame(self)
        frame_sl_btns.grid(row=3, column=6, columnspan=2, padx=5)
        for val in [0.25, 0.50, 1.00, 2.00]:
            ttk.Button(
                frame_sl_btns, text=str(val),
                command=lambda v=val: self._set_stop_loss(v)
            ).pack(side="left", padx=2)

        ttk.Label(self, text="Take Profit").grid(row=3, column=8)
        self.entry_tp = ttk.Entry(self, width=8)
        self.entry_tp.grid(row=3, column=9, padx=5)

        # --- Controls ---
        frame_ctrl = ttk.Frame(self)
        frame_ctrl.grid(row=4, column=0, columnspan=9, pady=8)
        self.frame_actions = ttk.Frame(self)
        self.frame_actions.grid(row=6, column=0, columnspan=9, pady=5)
        #self.frame_actions.grid_remove()

        self.btn_be = ttk.Button(self.frame_actions, text="Breakeven",
                                command=self._on_breakeven )
        
        self.btn_be.pack(side="left", padx=3)

        self.btn_fo = ttk.Button(self.frame_actions, text="Flatten out",
                                command=self._on_breakeven)

        self.btn_fo.pack(side=tk.LEFT,padx=3)
        self.tp_buttons = []
        for pct in (10, 20, 50):
            btn = ttk.Button(self.frame_actions, text=f"TP {pct}%",
                            command=lambda p=pct: self._on_take_profit(p))
            btn.pack(side="left", padx=3)
            self.tp_buttons.append(btn)

        # --- Offset row  (was aggressive checkbox) -------------------------
        offset_frame = ttk.Frame(self)
        offset_frame.grid(row=5, column=0, columnspan=9, pady=8)

        ttk.Label(offset_frame, text="Offset").pack(side="left", padx=(0, 4))
        self.entry_offset = ttk.Entry(offset_frame, width=6)
        self.entry_offset.pack(side="left")
        self.entry_offset.insert(0, "0.01")          # sane default

        for val in (0.01, 0.05, 0.15):
            ttk.Button(
                offset_frame, text=f"{val:.2f}",
                command=lambda v=val: self._set_offset(v),
                width=4
            ).pack(side="left", padx=2)
        self.btn_save = ttk.Button(frame_ctrl, text="Place Order", command=self.place_order, state="disabled")
        self.btn_save.pack(side="left", padx=5)
        ttk.Button(frame_ctrl, text="Cancel Order", command=self.cancel_order).pack(side="left", padx=5)
        ttk.Button(frame_ctrl, text="Reset", command=self.reset).pack(side="left", padx=5)
        ttk.Button(frame_ctrl, text="REBASE TRIGGER(PRE MARKET ACTION)", command=self._on_pm_high_rebase).pack(side="left", padx=5)

        # --- Status ---
        self.lbl_status = ttk.Label(self, text="Select symbol to start", foreground="gray")
        self.lbl_status.grid(row=7, column=0, columnspan=9, pady=5)

    # ---------- helpers ----------

    def _update_model_right(self, *args):
        """Synchronize self.model._right with the selected radio button (CALL/PUT)."""
        if not self.model:
            return
        
        right = self.var_type.get()
        if right in ("CALL", "PUT"):
            self.model._right = right[0].upper() # Set to "C" or "P"
            logging.info(f"AppModel[{self.current_symbol}]: Option right updated to {self.model._right}")

    def _on_type_changed(self, *args):
        """Repopulate strikes when CALL/PUT toggled AND update model right."""
        # ✅ Update model right immediately (no network call)
        self._update_model_right()
        
        if not self.model:
            return
        
        # ✅ Run price fetch and strike population in worker thread (non-blocking)
        def worker():
            try:
                price = self.model.refresh_market_price()
                if price:
                    self._populate_strike_combo(price)
            except Exception as e:
                logging.error(f"Type change repopulate error: {e}")
        
        threading.Thread(target=worker, daemon=True).start()

    def _update_order_type_label(self):
        """Update the order type label when checkbox changes."""
        if self.var_is_lmt.get():
            self.lbl_order_type.config(text="LMT")
        else:
            self.lbl_order_type.config(text="MKT")

    def _populate_strike_combo(self, centre: float):
        """
        Populate strike combo with actual strikes from option chain.
        For CALLs: 5 strikes above trigger price
        For PUTs: 5 strikes below trigger price
        ✅ ASYNC: Runs in worker thread to avoid blocking UI
        """
        if not self.model:
            return
        
        # Get maturity/expiry
        maturity = self.combo_maturity.get()
        if not maturity:
            # No maturity selected yet - can't get strikes
            self._ui(lambda: self.combo_strike.config(state="disabled"))
            self._ui(lambda: setattr(self.combo_strike, "values", []))
            return
        
        # ✅ Run in worker thread to avoid blocking UI
        def worker():
            try:
                # ✅ Get actual strikes from option chain (blocking call)
                available_strikes = self.model.get_available_strikes(maturity)
                
                # Filter out invalid strikes (None, empty, non-numeric, or <= 0)
                if available_strikes:
                    available_strikes = [
                        s for s in available_strikes 
                        if s is not None and isinstance(s, (int, float)) and s > 0
                    ]
                
                def apply():
                    if not available_strikes:
                        # Chain not loaded yet or no valid strikes
                        self.combo_strike["values"] = []
                        self.combo_strike.config(state="disabled")
                        return
                    
                    # ✅ Check radio button selection (CALL or PUT)
                    is_call = self.var_type.get() == "CALL"
                    
                    if is_call:
                        # ✅ CALL: 5 strikes above trigger price
                        strikes = sorted([s for s in available_strikes if s >= centre])
                        strikes = strikes[:5]  # Take first 5 above trigger
                        if not strikes:
                            # If no strikes above, get closest ones
                            strikes = sorted(available_strikes, key=lambda s: abs(s - centre))[:5]
                    else:  # PUT
                        # ✅ PUT: 5 strikes below trigger price
                        strikes = sorted([s for s in available_strikes if s <= centre], reverse=True)
                        strikes = strikes[:5]  # Take first 5 below trigger (descending)
                        if not strikes:
                            # If no strikes below, get closest ones
                            strikes = sorted(available_strikes, key=lambda s: abs(s - centre))[:5]
                            strikes.reverse()
                    
                    if not strikes:
                        self.combo_strike["values"] = []
                        self.combo_strike.config(state="disabled")
                        return
                    
                    # Format strikes (handle both whole numbers and decimals)
                    fmt_strikes = []
                    for s in strikes:
                        if s == int(s):
                            fmt_strikes.append(f"{int(s)}")
                        else:
                            fmt_strikes.append(f"{s:.1f}")
                    
                    self.combo_strike["values"] = fmt_strikes
                    
                    # Default to closest to trigger price
                    best = min(strikes, key=lambda v: abs(v - centre))
                    if best == int(best):
                        self.combo_strike.set(f"{int(best)}")
                    else:
                        self.combo_strike.set(f"{best:.1f}")
                    
                    self.combo_strike.config(state="readonly")
                
                self._ui(apply)
            except Exception as e:
                logging.error(f"Error populating strikes: {e}")
                self._ui(lambda: self.combo_strike.config(state="disabled"))
        
        threading.Thread(target=worker, daemon=True).start()

# helper: snap to next valid tick (up or down)
    def _next_tick(self, price: float, step: float, up: bool) -> float:
        base = int(price / step) * step
        if up and base <= price:
            base += step
        elif not up and base >= price:
            base -= step
        return base
    def _set_offset(self, value: float):
        """Slam the offset entry with the pressed button's value."""
        self.entry_offset.delete(0, tk.END)
        self.entry_offset.insert(0, f"{value:.2f}")
    # ---------- helper ----------
    def _bump_trigger(self, delta: float):
        try:
            current = float(self.entry_trigger.get() or 0)
        except ValueError:
            current = 0.0

        new = current + delta

        # snap to nearest 0.50 only for big ($0.50) steps
        if abs(delta) >= 0.5:
            new = round(new * 2) / 2

        self.entry_trigger.delete(0, tk.END)
        self.entry_trigger.insert(0, f"{new:.2f}")
        return "break"
    def _set_stop_loss(self, value: float):
        """Set Stop Loss entry directly to offset value (no math here)."""
        self.entry_sl.delete(0, tk.END)
        self.entry_sl.insert(0, str(value))

    def _set_pos_and_recalc(self, value: float):
        """Set position size from quick button and recalc quantity."""
        self.entry_pos_size.delete(0, tk.END)
        self.entry_pos_size.insert(0, str(value))
        self.recalc_quantity()

    def recalc_quantity(self):
        """Recalculate Qty from current position size + last price.
        ✅ ASYNC: Runs in worker thread to avoid blocking UI.
        """
        if not self.model:
            return
        
        # ✅ Run in worker thread to avoid blocking UI
        def worker():
            try:
                # Get position size (read from UI in worker thread is safe)
                pos_size = float(self.entry_pos_size.get() or 2000)
                
                # ✅ Fetch price in background (blocking network call)
                price = self.model.refresh_market_price()
                
                # ✅ Update UI via thread-safe callback
                def apply():
                    if price and price > 0:
                        qty = self.model.calculate_quantity(pos_size, price)
                        self.entry_qty.delete(0, tk.END)
                        self.entry_qty.insert(0, str(qty))
                    else:
                        # Price fetch failed - keep existing qty or show error
                        logging.warning("Could not fetch price for quantity calculation")
                
                self._ui(apply)
            except Exception as e:
                logging.error(f"Quantity recalc error: {e}")
        
        threading.Thread(target=worker, daemon=True).start()

    def _ui(self, fn, *args, **kwargs):
        """Thread-safe UI update."""
        self.after(0, lambda: fn(*args, **kwargs))

    def _set_status(self, text: str, color: str = "gray"):
        self._ui(self.lbl_status.config, text=text, foreground=color)
        try:
            if self.model and self.model.order:
                order = self.model.order
                if order.state == OrderState.FINALIZED:
                    self._ui(self._on_order_finalized)
        except Exception as e:
            logging.error(f"[OrderFrame] Finalization check error: {e}")

    # ---------- handlers for new buttons ----------

    def _on_premarket_trigger(self):
        if not self.model or not self.current_symbol:
            self._set_status("Error: No symbol selected or model", "red")
            return
        
        self._set_status("Fetching pre-market data...", "orange")
        def worker():
            try:
                # 1. Price logic
                price = self.model.get_premarket_trigger_price() 
                
                # 2. UI update (thread-safe)
                def apply():
                    if price is not None and price > 0:
                        self.entry_trigger.delete(0, tk.END)
                        self.entry_trigger.insert(0, f"{price:.2f}")
                        self._set_status(f"Trigger set to P-Market {self.model._right} @ {price:.2f}", "blue")
                    else:
                        self._set_status("Error: Could not retrieve valid pre-market price.", "red")
                self._ui(apply)
            except Exception as e:
                logging.error(f"P-Market trigger error: {e}")
                self._ui(lambda: self._set_status(f"P-Market trigger failed: {e}", "red"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_intraday_trigger(self):
        if not self.model or not self.current_symbol:
            self._set_status("Error: No symbol selected or model", "red")
            return
            
        self._set_status("Fetching intraday data...", "orange")
        def worker():
            try:
                # 1. Price logic
                price = self.model.get_intraday_trigger_price() 
                
                # 2. UI update (thread-safe)
                def apply():
                    if price is not None and price > 0:
                        self.entry_trigger.delete(0, tk.END)
                        self.entry_trigger.insert(0, f"{price:.2f}")
                        self._set_status(f"Trigger set to Intraday {self.model._right} @ {price:.2f}", "blue")
                    else:
                        self._set_status("Error: Could not retrieve valid intraday price (HOD/LOD).", "red")
                self._ui(apply)
            except Exception as e:
                logging.error(f"Intraday trigger error: {e}")
                self._ui(lambda: self._set_status(f"Intraday trigger failed: {e}", "red"))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- events ----------
    def on_symbol_selected(self, symbol: str):
        self.current_symbol = symbol
        self.model = get_model(symbol)
        
        # 🎯 FIX: Call the centralized update function instead of setting _right directly
        self._update_model_right() 
        
        self._symbol_token += 1
        token = self._symbol_token
        self.model.set_status_callback(self._set_status)
        self._set_status(f"Ready: {symbol}", "blue")
        self.btn_save.config(state="normal")
        # Enable new trigger buttons
        self.btn_premarket.config(state="normal")
        self.btn_intraday.config(state="normal")


        # price + prefill trigger + quantity off-thread
        def price_worker():
            try:
                price = self.model.refresh_market_price()
            except Exception as e:
                logging.error(f"Auto-fill price error: {e}")
                price = None
            def apply():
                if token != self._symbol_token:
                    return
                
                if price:
                    self._populate_strike_combo(price) 
                    # trigger

                    trigger_price = price + 0.10 if self.var_type.get() == "CALL" else price - 0.10
                    self.entry_trigger.delete(0, tk.END)
                    self.entry_trigger.insert(0, f"{trigger_price:.2f}")
                    # quantity from position size
                    try:
                        pos_size = float(self.entry_pos_size.get() or 2000)
                        qty = self.model.calculate_quantity(pos_size, price)
                        self.entry_qty.delete(0, tk.END)
                        self.entry_qty.insert(0, str(qty))
                    except Exception as e:
                        logging.error(f"Quantity calc error: {e}")
            self._ui(apply)
        threading.Thread(target=price_worker, daemon=True).start()

        # maturities off-thread (model handles any internal waits)
        self.load_maturities_async(token)

    def on_maturity_selected(self, event=None):
        maturity = self.combo_maturity.get()
        if maturity and self.model:
            # ✅ Trigger strike population when maturity is selected
            def worker():
                try:
                    # Get current trigger price or underlying price
                    trigger_str = self.entry_trigger.get()
                    if trigger_str:
                        centre = float(trigger_str)
                    else:
                        # Fallback to underlying price
                        price = self.model.refresh_market_price()
                        if price:
                            centre = price
                        else:
                            return
                    self._populate_strike_combo(centre)
                except Exception as e:
                    logging.error(f"Maturity selection strike load error: {e}")
            
            threading.Thread(target=worker, daemon=True).start()

    # ---------- async loaders ----------
    def load_maturities_async(self, token: int):
        self._maturity_req_id += 1
        req_id = self._maturity_req_id

        def worker():
            try:
                all_maturities = self.model.get_available_maturities()   # raw strings YYYYMMDD
                # keep only the first 4 chronologically
                kept = sorted(all_maturities)[:4]
            except Exception as e:
                logging.error(f"Maturity load error: {e}")
                kept = []

            def apply():
                if token != self._symbol_token or req_id != self._maturity_req_id:
                    return  # stale
                self.combo_maturity["values"] = kept
                if kept:
                    self.combo_maturity.set(kept[0])
            self._ui(apply)

        threading.Thread(target=worker, daemon=True).start()

    def load_strikes_async(self, maturity: str):
        # ✅ Trigger strike population (called from on_maturity_selected)
        def worker():
            try:
                trigger_str = self.entry_trigger.get()
                if trigger_str:
                    centre = float(trigger_str)
                else:
                    price = self.model.refresh_market_price()
                    if price:
                        centre = price
                    else:
                        return
                self._populate_strike_combo(centre)
            except Exception as e:
                logging.error(f"Load strikes async error: {e}")
        
        threading.Thread(target=worker, daemon=True).start()

    # ---------- actions ----------
    def _on_pm_high_rebase(self):
        def worker():
            new_trigger = self.model.rebase_premarket_to_new_extreme()
            if not new_trigger:
                self._ui(lambda: self._set_status("PM rebase failed", "red"))
                return

            def apply():
                self.entry_trigger.delete(0, tk.END)
                self.entry_trigger.insert(0, f"{new_trigger:.2f}")
                self._populate_strike_combo(new_trigger)

            self._ui(apply)

        threading.Thread(target=worker, daemon=True).start()

    
    def place_order(self):
        if not self.model or not self.current_symbol:
            self._set_status("Error: No symbol selected", "red")
            return

        try:
            position_size = float(self.entry_pos_size.get() or 50000)
            maturity = self.combo_maturity.get()
            right = self.var_type.get()
            quantity = int(self.entry_qty.get() or 1)
            trigger_str = self.entry_trigger.get()
            self.type = "LMT" if self.var_is_lmt.get() else "MKT"
            logging.info(f"[UI] Order type selected: {self.type} (checkbox={self.var_is_lmt.get()})")
            trigger = float(trigger_str) if trigger_str else None
            sl = float(self.entry_sl.get()) if self.entry_sl.get() else None
            tp = float(self.entry_tp.get()) if self.entry_tp.get() else None
        except Exception as e:
            self._set_status(f"Order input error: {e}", "red")
            return

        self.btn_save.config(state="disabled")
        self._set_status("Placing order...", "orange")

        def worker():
            try:
                # Validate strike is selected
                strike_str = self.combo_strike.get().strip()
                if not strike_str:
                    raise ValueError("Please select a strike price")
                
                strike = float(strike_str)

                # ⚠️ STOP LOSS WARNING POPUP (blocking)
                if sl is None:
                    proceed_flag = tk.BooleanVar(value=False)

                    def show_popup():
                        popup = tk.Toplevel(self)
                        popup.title("⚠️ Warning")
                        popup.geometry("340x140")
                        popup.configure(bg="#222")
                        popup.grab_set()

                        ttk.Label(
                            popup,
                            text="Stop Loss not set!",
                            font=("Arial", 11, "bold")
                        ).pack(pady=(15, 5))

                        ttk.Label(
                            popup,
                            text="Proceeding without Stop Loss may be risky.",
                            font=("Arial", 9)
                        ).pack(pady=(0, 10))

                        frame_btn = ttk.Frame(popup)
                        frame_btn.pack(pady=5)

                        def proceed():
                            proceed_flag.set(True)
                            popup.destroy()

                        def cancel():
                            proceed_flag.set(False)
                            popup.destroy()

                        ttk.Button(frame_btn, text="Proceed Anyway", command=proceed).pack(side="left", padx=10)
                        ttk.Button(frame_btn, text="Cancel", command=cancel).pack(side="left", padx=10)

                    # Show popup synchronously in UI thread
                    self._ui(show_popup)
                    self.wait_variable(proceed_flag)
                    if not proceed_flag.get():
                        self._ui(lambda: self._set_status("Order cancelled (no SL).", "red"))
                        self.btn_save.config(state="normal")
                        return

                # Continue normally if OK
                # ✅ Validate maturity is not empty
                if not maturity or not maturity.strip():
                    self._ui(lambda: self._set_status("Error: Please select a maturity/expiry", "red"))
                    self.btn_save.config(state="normal")
                    return
                
                self.model.set_option_contract(maturity, strike, right)
                if sl is not None:
                    self.model._stop_loss = sl
                if tp is not None:
                    self.model._take_profit = tp

                try:
                    arcTick = float(self.entry_offset.get() or 1.06)
                except ValueError:
                    arcTick = 1.06

                order_data = self.model.place_option_order(
                    action="BUY",
                    quantity=quantity,
                    trigger_price=trigger,
                    status_callback=self._set_status,
                    position=position_size,
                    
                    arcTick=arcTick,
                    type=self.type
                )
                if is_market_closed_or_pre_market():
                    return
                state = order_data.get("state", "UNKNOWN")
                msg = f"Order {state}: {order_data.get('order_id')}"
                color = "green" if state == "ACTIVE" else "orange"

                def ok():
                    self._set_status(msg, color)
                    self.btn_save.config(state="normal")

                self._ui(ok)
                logging.info(f"Order placed: {order_data}")

            except Exception as e:
                self._e = e
                def err():
                    self._set_status(f"Order failed: {self._e}", "red")
                    self.btn_save.config(state="normal")
                self._ui(err)
                logging.error(f"Order failed: {e}")

        threading.Thread(target=worker, daemon=True).start()



    def cancel_order(self):
        if not self.model:
            return
        def worker():
            try:
                if is_market_closed_or_pre_market():
                    self.model.cancel_queued()
                pending = self.model.order
                
                if pending:  # Check if order exists
                    self.model.cancel_pending_order(pending.order_id)
                    self._ui(lambda: self._set_status("Pending orders cancelled", "orange"))
                else:
                    self._ui(lambda: self._set_status("No order to cancel", "gray"))
            except Exception as e:
                logging.error(f"Cancel error: {e}")
                self._ui(lambda: self._set_status(f"Cancel error: {e}", "red"))
            finally:
                # Always re-enable button after cancel (success or failure)
                self._ui(lambda: self.btn_save.config(state="normal"))
        threading.Thread(target=worker, daemon=True).start()

    def reset(self):
        self._symbol_token += 1
            # 🔪 KILL WATCHER ON RESET
        if self.symbol_selector.watcher:
            self.symbol_selector.watcher.stop()
            self.symbol_selector.watcher = None
        self.model = None
        self.current_symbol = None

        self.symbol_selector.combo_symbol.set('')
        self.symbol_selector.lbl_price.config(text="Price: -")
        self.entry_trigger.delete(0, tk.END)
        self.entry_pos_size.delete(0, tk.END)
        self.entry_pos_size.insert(0, "2000")
        self.combo_maturity.set('')
        self.entry_qty.delete(0, tk.END)
        self.entry_qty.insert(0, "1")
        self.entry_sl.delete(0, tk.END)
        self.entry_tp.delete(0, tk.END)
        self._set_status("Select symbol to start", "gray")
        self.btn_save.config(state="disabled")
        self.btn_premarket.config(state="disabled")
        self.btn_intraday.config(state="disabled")

    def _on_order_finalized(self):
        """Enable manual actions when backend finalizes order"""
        if not self.is_finalized:
            self.is_finalized = True
            self.frame_actions.grid()
            self.btn_be.config(state="normal")
            self.btn_fo.config(state="normal")
            for btn in self.tp_buttons:
                btn.config(state="normal")
            self._set_status("Order Finalized – controls enabled", "green")

    def _on_flatten_out(self):
        logging.info(f"[Flatten_out] starting")
        def worker():
            try:
                if self.model and self.model.order:
                    order = self.model.order
                    order_manager.breakeven(order.order_id)
                    self._ui(lambda: self._set_status("Flatten out triggered", "blue"))
                else:
                    self._ui(lambda: self._set_status("Error: No active order", "red"))
            except Exception as e:
                logging.error(f"FLATTEN_OUT error: {e}")
                self._ui(lambda: self._set_status(f"FLATTEN_OUT Error: {e}", "red"))

        threading.Thread(target=worker, daemon=True).start()
    def _on_breakeven(self):
        logging.info(f"[Breakeven] starting")
        def worker():
            try:
                if not self.model:
                    self._ui(lambda: self._set_status("Error: No model", "red"))
                    return
                
                # ✅ FIX: Find the most recent finalized BUY order for this symbol
                from Services.order_manager import order_manager
                finalized = None
                for order_id, order in order_manager.finalized_orders.items():
                    if order.symbol == self.model.symbol and order.action == "BUY":
                        finalized = order
                        break
                
                if not finalized:
                    self._ui(lambda: self._set_status("Breakeven Error: No active position", "red"))
                    logging.warning(f"[Breakeven] No finalized BUY order found for {self.model.symbol}")
                    return
                
                # ✅ Get trigger price (stock price level that triggered entry)
                if not finalized.trigger:
                    self._ui(lambda: self._set_status("Breakeven Error: No trigger price found", "red"))
                    return
                
                trigger_price = float(finalized.trigger)
                
                # ✅ Find the exit order (stop loss watcher) for this original order
                from model import general_app
                from Services.watcher_info import watcher_info
                
                wait_service = general_app.order_wait
                
                if not wait_service:
                    self._ui(lambda: self._set_status("Breakeven Error: Wait service not available", "red"))
                    logging.warning(f"[Breakeven] Wait service not available")
                    return
                
                # Search for exit order by previous_id in multiple places:
                # 1. Check active_stop_losses (WS mode)
                exit_order = None
                with wait_service.lock:
                    for order_id, order in wait_service.active_stop_losses.items():
                        if hasattr(order, 'previous_id') and order.previous_id == finalized.order_id:
                            exit_order = order
                            break
                
                # 2. If not found, check watcher_info (polling mode)
                if not exit_order:
                    # Get all watchers and check their orders
                    all_watchers = watcher_info.list_all()
                    for watcher_dict in all_watchers:
                        if watcher_dict.get('watcher_type') == 'stop_loss':
                            watcher_order_id = watcher_dict.get('order_id')
                            if watcher_order_id:
                                # Get the ThreadInfo object
                                thread_info = watcher_info.get_watcher(watcher_order_id)
                                if thread_info and thread_info.order:
                                    order = thread_info.order
                                    if hasattr(order, 'previous_id') and order.previous_id == finalized.order_id:
                                        exit_order = order
                                        break
                
                if not exit_order:
                    self._ui(lambda: self._set_status("Breakeven Error: Stop loss watcher not found", "red"))
                    logging.warning(f"[Breakeven] No exit order found for original order {finalized.order_id}")
                    return
                
                # ✅ Update stop loss to trigger price (breakeven)
                cb = amo.get(LOSS)
                cb(exit_order, trigger_price)
                
                logging.info(f"[Breakeven] Updated stop loss to trigger price {trigger_price} for order {finalized.order_id}")
                self._ui(lambda: self._set_status(f"Breakeven: Stop loss moved to trigger price {trigger_price:.2f}", "blue"))
            except Exception as e:
                logging.error(f"Breakeven error: {e}")
                self._ui(lambda: self._set_status(f"Breakeven Error: {e}", "red"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_take_profit(self, pct):
        logging.info(f"[Take Profit] starting")
        def worker():
            try:
                if not self.model:
                    self._ui(lambda: self._set_status("Error: No model", "red"))
                    return
                
                # ✅ FIX: Find the most recent finalized BUY order for this symbol
                from Services.order_manager import order_manager
                finalized = None
                for order_id, order in order_manager.finalized_orders.items():
                    if order.symbol == self.model.symbol and order.action == "BUY":
                        finalized = order
                        break
                
                if finalized:
                    order_manager.take_profit(finalized.order_id, pct / 100)
                    self._ui(lambda: self._set_status(f"Take Profit {pct}% triggered", "blue"))
                else:
                    logging.error(f"Take-Profit error: No finalized BUY order found for {self.model.symbol}")
                    self._ui(lambda: self._set_status("Error: No active position", "red"))
            except Exception as e:
                logging.error(f"Take-Profit error: {e}")
                self._ui(lambda: self._set_status(f"Error: {e}", "red"))

        threading.Thread(target=worker, daemon=True).start()

    
    def serialize(self) -> str:
        """
        Serialize this frame's UI metadata and its associated model.
        The frame never inspects or manipulates model.order directly.

        Layer delimiters:
            Frame   → '|'
            Model   → ':'
            Order   → '_'

        Output Example:
            <Frame>|id=1|state=ACTIVE|symbol=AAPL
            AppModel:18dc9329:AAPL:20251018:195.0:C:2.1:2.7:True
            Order:18dc9329_AAPL_20251018_195.0_C_1_2.35_2.5_2.0_BUY_195.1_PENDING_None
        """
        frame_id = getattr(self, "frame_id", id(self))
        state = getattr(self, "_view_state", "UNKNOWN")
        symbol = getattr(self.model, "symbol", "None") if hasattr(self, "model") else "None"

        header = f"<Frame>|id={frame_id}|state={state}|symbol={symbol}"

        model = getattr(self, "model", None)
        if model is not None:
            model_block = model.serialize()
            return f"{header}\n{model_block}"
        return header

    @classmethod
    def deserialize(cls, lines: list[str], parent) -> tuple["OrderFrame", int]:
        """
        Deserialize a frame block and its attached model.

        Expected input:
            <Frame>|id=<id>|state=<state>|symbol=<symbol>
            AppModel:...
            [Order:...]

        Returns:
            (OrderFrame instance, lines_consumed)
        """
        if not lines or not lines[0].startswith("<Frame>|"):
            raise ValueError("Expected a line starting with '<Frame>|'")

        # --- Parse frame header ---
        parts = lines[0].split("|")
        frame_id = None
        state = "UNKNOWN"
        symbol = "None"

        for p in parts[1:]:
            if p.startswith("id="):
                frame_id = p.split("=", 1)[1]
            elif p.startswith("state="):
                state = p.split("=", 1)[1]
            elif p.startswith("symbol="):
                symbol = p.split("=", 1)[1]

        # --- Instantiate frame ---
        frame = cls(parent)
        frame.frame_id = frame_id
        frame._view_state = state

        # --- Restore symbol correctly ---
        try:
            if symbol and symbol != "None":
                # Set symbol in combobox instead of non-existent symbol_var
                frame.symbol_selector.combo_symbol.set(symbol)
                # Optionally rebuild the model so buttons etc. get enabled
                frame.on_symbol_selected(symbol)
                logging.info(f"[OrderFrame.deserialize] Restored symbol {symbol}")
        except Exception as e:
            logging.error(f"[OrderFrame.deserialize] Symbol restore failed: {e}")

        consumed = 1

        # --- Parse attached model if present ---
        if len(lines) > 1 and lines[1].startswith("AppModel:"):
            try:
                model, used = AppModel.deserialize(lines[1:3])
                frame.model = model
                consumed += used
            except Exception as e:
                logging.error(f"[OrderFrame.deserialize] Model restore failed: {e}")

        return frame, consumed
    

class ScrollableFrame(ttk.Frame):
    """
    A scrollable Frame widget that uses a Canvas and Scrollbar.
    Mouse wheel scrolling is bound to the canvas.
    """
    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)

        # 1. Create the Canvas
        self.canvas = tk.Canvas(self, borderwidth=0, background="#ffffff")
        
        # 2. Create the Scrollbar
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        
        # 3. Create the interior frame to hold content
        self.scrollable_frame = ttk.Frame(self.canvas)

        # 4. Bind the interior frame's size to the canvas
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")
            )
        )

        # 5. Create the canvas window
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        
        # 6. Configure the canvas to use the scrollbar
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        # 7. Grid layout
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # 8. Bind mouse wheel scrolling
        self._bind_global_scroll()

    def _bind_global_scroll(self):
        """Bind mousewheel to the root window so it scrolls everywhere."""
        root = self.winfo_toplevel()  # main Tk instance
        root.bind_all("<MouseWheel>", self._on_mousewheel_windows)
        root.bind_all("<Button-4>", self._on_mousewheel_linux)
        root.bind_all("<Button-5>", self._on_mousewheel_linux)

    def _on_mousewheel_windows(self, event):
        """Scroll handler for Windows / macOS."""
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_mousewheel_linux(self, event):
        """Scroll handler for Linux (button 4/5)."""
        direction = -1 if event.num == 4 else 1
        self.canvas.yview_scroll(direction, "units")