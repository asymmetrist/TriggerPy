import tkinter as tk
from tkinter import ttk
import logging
import time
from datetime import datetime
from pathlib import Path
from Helpers.debugger import DebugFrame, TkinterHandler
from Services.order_manager import order_manager
from model import general_app
# --- MODIFIED IMPORT ---
from view import Banner, OrderFrame, ScrollableFrame
from Services.watcher_info import watcher_info
import os, sys
import threading
from datetime import datetime, timedelta
from Services.runtime_manager import runtime_man
from work_symbols_view import WorkSymbolsView

AUTO_SAVE_INTERVAL_MIN = 5

# Global reference to app instance for popup dialogs
_app_instance = None

def get_app_instance():
    """Get the global ArcTriggerApp instance."""
    return _app_instance



def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def setup_logging():
    log_dir = Path("logs"); log_dir.mkdir(exist_ok=True)
    log_file = log_dir / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log")

    root = logging.getLogger()
    if root.hasHandlers():
        root.handlers.clear()

    handler_file = logging.FileHandler(log_file, encoding='utf-8')
    handler_console = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for h in (handler_file, handler_console):
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(logging.INFO)
    logging.info("Logging initialised → %s", log_file)


class ArcTriggerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ArcTriggerPy")
        self.configure(bg="black")
        # ✅ Make icon loading optional (won't crash if icon missing)
        try:
            icon_path = resource_path("icon.ico")
            if os.path.exists(icon_path):
                self.iconbitmap(icon_path)
        except Exception as e:
            logging.debug(f"Could not load icon: {e}")
        # Set global app instance immediately when app is created
        global _app_instance
        _app_instance = self
        logging.info(f"[ArcTriggerApp.__init__] Global app instance set: {_app_instance} (type: {type(_app_instance)})")
        self.geometry("1400x800")  # width x height

        # ---------- Banner ----------
        self.banner = Banner(self)
        self.banner.pack(fill="x")

        # ---------- Top control bar ----------
        top_frame = ttk.Frame(self)
        top_frame.pack(fill="x", pady=10)

        ttk.Button(top_frame, text="Connect", command=self.connect_services).pack(side="left", padx=5)
        ttk.Button(top_frame, text="Disconnect", command=self.disconnect_services).pack(side="left", padx=5)

        ttk.Label(top_frame, text="Order Count:", background="black", foreground="white").pack(side="left", padx=5)
        self.spin_count = tk.Spinbox(top_frame, from_=1, to=10, width=5)
        self.spin_count.pack(side="left", padx=5)
        self.spin_count.delete(0, tk.END)
        self.spin_count.insert(0, "1")

        tk.Button(top_frame, text="Start Trigger", bg="red", fg="white", command=self.build_order_frames).pack(side="left", padx=10)
        tk.Button(top_frame, text="Show Debug", command=self.toggle_debug).pack(side="left", padx=10)
        ttk.Button(top_frame, text="Watchers", command=self.show_watchers).pack(side="left", padx=5)
        ttk.Button(top_frame, text="Positions", command=self.show_positions).pack(side="left", padx=5)
        ttk.Button(
            top_frame,
            text="Work Symbols",
            command=lambda: WorkSymbolsView(self)
        ).pack(side="left", padx=5)
        
        # Replay mode toggle
        self.replay_mode = False
        self.replay_service = None
        ttk.Button(top_frame, text="🎬 Replay", command=self.toggle_replay_mode).pack(side="left", padx=5)
        self.replay_status_label = ttk.Label(top_frame, text="", foreground="gray", background="black")
        self.replay_status_label.pack(side="left", padx=5)


        # Removed: ttk.Button(top_frame, text="Finalized Orders", ...)

        # --- MODIFIED: Order container ---
        # Replaced ttk.Frame with the new ScrollableFrame
        self.order_container = ScrollableFrame(self)
        self.order_container.pack(fill="both", expand=True)

        self.order_frames = []
        self.debug_frame = None
        self.disconnect_services()
        self.connect_services()
        self.start_conn_monitor()

        self._running = runtime_man.is_run()
        self.start_auto_save_thread()
        self.protocol("WM_DELETE_WINDOW", self.on_exit)
        


# Attempt auto-restore on startup
        restored = self.load_session(auto=True)
        if restored:
            logging.info("[ArcTriggerApp] Previous session auto-restored.")
        else:
            logging.info("[ArcTriggerApp] No recent session to restore.")
        
        # Auto-refresh work symbols if in premarket
        self.after(2000, self._auto_refresh_work_symbols_if_premarket)  # Wait 2s for connection to establish

    
    def show_positions(self):
        from opmng_ui import open_positions_window
        open_positions_window(self)

    def _auto_refresh_work_symbols_if_premarket(self):
        """Auto-refresh work symbols conIDs if in premarket."""
        from Services.nasdaq_info import is_market_closed_or_pre_market
        from Services.work_symbols import work_symbols
        from Services.tws_service import create_tws_service
        
        # Check if premarket
        if not is_market_closed_or_pre_market():
            logging.info("[ArcTriggerApp] Not in premarket - skipping auto-refresh")
            return
        
        # Check TWS connection
        tws_service = create_tws_service()
        if not tws_service.is_connected():
            logging.info("[ArcTriggerApp] TWS not connected - skipping auto-refresh")
            return
        
        # Check if there are symbols to refresh
        symbols = list(work_symbols.get_ready_symbols().keys())
        if not symbols:
            logging.info("[ArcTriggerApp] No symbols in Work Symbols - skipping auto-refresh")
            return
        
        logging.info(f"[ArcTriggerApp] Premarket detected - auto-refreshing conIDs for {len(symbols)} symbol(s)")
        
        # Run refresh in background thread
        def worker():
            try:
                from Services.option_conid_cache_service import cache_option_conids_for_all_symbols
                from Services.polygon_service import polygon_service
                from Services.persistent_conid_storage import storage
                
                # Step 1: Refresh stock conIDs
                logging.info("[ArcTriggerApp] Auto-refresh: Refreshing stock conIDs...")
                work_symbols.refresh_all_conids()
                
                # Step 2: Cache option conIDs
                logging.info("[ArcTriggerApp] Auto-refresh: Caching option conIDs...")
                results = cache_option_conids_for_all_symbols(
                    tws_service, polygon_service, storage, num_strikes=10
                )
                
                total_cached = sum(cached for cached, _ in results.values())
                total_attempted = sum(attempted for _, attempted in results.values())
                
                logging.info(
                    f"[ArcTriggerApp] Auto-refresh complete: "
                    f"Stock conIDs refreshed, {total_cached}/{total_attempted} option conIDs cached"
                )
            except Exception as e:
                logging.error(f"[ArcTriggerApp] Error in auto-refresh: {e}")
        
        threading.Thread(target=worker, daemon=True).start()

    def save_session(self, filename: str = "arctrigger.dat", background: bool = False):
        """
        Robust save: writes to temp file then atomically replaces.
        Includes header marker + frame count.
        """
        try:
            header = ["#ARCTRIGGER_SESSION_V1"]
            header.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            frame_count = len(self.order_frames)
            header.append(str(frame_count))
            logging.info(f"[ArcTriggerApp.save_session] Saving {frame_count} frames...")

            lines = []
            for frame in self.order_frames:
                try:
                    serialized = frame.serialize().strip()
                    if serialized:
                        lines.append(serialized)
                except Exception as e:
                    logging.error(f"[ArcTriggerApp.save_session] Failed to serialize frame: {e}")

            tmpfile = filename + ".tmp"
            with open(tmpfile, "w", encoding="utf-8") as f:
                f.write("\n".join(header + lines))

            os.replace(tmpfile, filename)
            logging.info(f"[ArcTriggerApp.save_session] ✓ Saved {frame_count} frames → {filename}")

        except Exception as e:
            logging.error(f"[ArcTriggerApp.save_session] ❌ Save failed: {e}")

    
    def load_session(self, filename: str = "arctrigger.dat", auto=False):
        """
        Robust load with version + timestamp check.
        Restores partial frames safely if one fails.
        """
        if not os.path.exists(filename):
            logging.info("[ArcTriggerApp.load_session] No session file found.")
            return False

        try:
            with open(filename, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
        except Exception as e:
            logging.error(f"[ArcTriggerApp.load_session] Failed to read {filename}: {e}")
            return False

        if len(lines) < 3 or not lines[0].startswith("#ARCTRIGGER_SESSION_"):
            logging.warning(f"[ArcTriggerApp.load_session] File incomplete or invalid header ({len(lines)} lines).")
            return False

        try:
            timestamp = datetime.strptime(lines[1], "%Y-%m-%d %H:%M:%S")
            count = int(lines[2])
        except Exception as e:
            logging.error(f"[ArcTriggerApp.load_session] Invalid header: {e}")
            return False

        if auto and datetime.now() - timestamp > timedelta(minutes=15):
            logging.info("[ArcTriggerApp.load_session] Last session too old, skipping auto-restore.")
            return False

        # wipe current frames
        for frame in self.order_frames:
            frame.destroy()
        self.order_frames.clear()

        restored = 0
        for i, block in enumerate(lines[3:], start=1):
            if not block.startswith("<Frame>|"):
                continue
            try:
                # --- MODIFIED: Parent is now the inner frame of the scrollable container ---
                frame, _ = OrderFrame.deserialize([block], parent=self.order_container.scrollable_frame)
                frame.pack(fill="x", pady=10, padx=10)
                self.order_frames.append(frame)
                restored += 1
            except Exception as e:
                logging.error(f"[ArcTriggerApp.load_session] Frame {i} failed: {e}")

        logging.info(f"[ArcTriggerApp.load_session] Restored {restored}/{count} frames.")
        return restored > 0


    def start_auto_save_thread(self):
        """
        Background thread that saves the session every 15 minutes.
        Terminates gracefully when _running becomes False.
        """
        def _loop():
            while self._running:
                try:
                    self.save_session(background=True)
                except Exception as e:
                    logging.error(f"[ArcTriggerApp.auto_save] Error: {e}")
                threading.Event().wait(AUTO_SAVE_INTERVAL_MIN * 60)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        self._autosave_thread = t
        logging.info(f"[ArcTriggerApp] Auto-save thread started ({AUTO_SAVE_INTERVAL_MIN} min interval).")

    def on_exit(self):
        """
        Triggered when the user closes the app window.
        Performs a final autosave, stops background threads,
        and safely destroys the Tkinter root.
        """
        # Stop replay if active
        if self.replay_mode:
            self._stop_replay_mode()
        
        try:
            logging.info("[ArcTriggerApp.on_exit] Application exiting, performing final save...")
            runtime_man.stop()
            self._running = runtime_man.is_run()  # stop autosave loop

            self.save_session("arctrigger.dat")
            logging.info("[ArcTriggerApp.on_exit] Final session autosaved.")
        except Exception as e:
            logging.error(f"[ArcTriggerApp.on_exit] Error during final save: {e}")
        finally:
            self.destroy()
            logging.info("[ArcTriggerApp.on_exit] Tkinter window destroyed.")


    # ------------------------------------------------------------------
    #  CONNECTION MONITOR THREAD
    # ------------------------------------------------------------------
    def start_conn_monitor(self, interval: int = 5):
        import threading
        from Services.tws_service import create_tws_service

        def monitor():
            service = create_tws_service()
            last_state = None
            while True:
                try:
                    current_state = service.conn_status()
                    if current_state != last_state:
                        self.after(0, lambda s=current_state: self.banner.update_connection_status(s))
                        last_state = current_state
                except Exception as e:
                    logging.error(f"[ConnMonitor] Error checking TWS status: {e}")
                finally:
                    time.sleep(interval)

        t = threading.Thread(target=monitor, name="ConnMonitorThread", daemon=True)
        t.start()
        logging.info("Connection monitor thread started.")

    # ------------------------------------------------------------------
    #  WATCHERS WINDOW
    # ------------------------------------------------------------------
    def show_watchers(self):
        import tkinter as tk
        from tkinter import ttk, messagebox

        win = tk.Toplevel(self)
        win.title("Active Watchers")
        win.geometry("900x350")

        cols = ("Order ID", "Symbol", "Type", "Mode", "Status", "StopLoss", "LastPrice", "StartTime", "Action")
        tree = ttk.Treeview(win, columns=cols, show="headings")

        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=100, anchor="center")

        tree.pack(fill="both", expand=True)

        # Scrollbars
        yscroll = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side="right", fill="y")

        def on_cancel(order_id):
            try:
                watcher_info.cancel(order_id)
                messagebox.showinfo("Cancelled", f"Watcher {order_id} cancelled.")
            except Exception as e:
                logging.info(f"Failed to cancel watcher {order_id}: {e}")
                messagebox.showerror("Error", f"Failed to cancel watcher {order_id}: {e}")

        def refresh():
            # Clear and re-populate
            for i in tree.get_children():
                tree.delete(i)
            for w in watcher_info.list_all():
                order_id = w["order_id"]
                # Insert row
                tree.insert("", "end", values=(
                    order_id, w["symbol"], w["watcher_type"], w["mode"],
                    w["status_label"], w["stop_loss"], w["last_price"],
                    w["start_time"][:19], "Cancel"
                ))
            win.after(2000, refresh)

        def on_tree_click(event):
            item = tree.identify_row(event.y)
            col = tree.identify_column(event.x)
            if col == f"#{len(cols)}" and item:  # 'Action' column
                order_id = tree.item(item)["values"][0]
                on_cancel(order_id)

        tree.bind("<Button-1>", on_tree_click)
        refresh()
    
    # ------------------------------------------------------------------
    #  REPLAY MODE
    # ------------------------------------------------------------------
    def toggle_replay_mode(self):
        """Toggle replay mode on/off."""
        if not self.replay_mode:
            self._start_replay_mode()
        else:
            self._stop_replay_mode()
    
    def _start_replay_mode(self):
        """Start replay mode - show dialog to configure replay."""
        from tkinter import simpledialog, messagebox
        
        try:
            # Get date
            date = simpledialog.askstring(
                "Replay Mode",
                "Enter date to replay (YYYY-MM-DD):\n\nExample: 2026-01-22",
                initialvalue="2026-01-22"
            )
            if not date:
                return
            
            # Validate date format
            from datetime import datetime
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                messagebox.showerror("Invalid Date", "Date must be in YYYY-MM-DD format")
                return
            
            # Get symbol
            symbol = simpledialog.askstring(
                "Replay Mode",
                "Enter symbol to replay:\n\nExample: TSLA",
                initialvalue="TSLA"
            )
            if not symbol:
                return
            
            # Get speed
            speed_str = simpledialog.askstring(
                "Replay Mode",
                "Replay speed:\n1.0 = real-time\n2.0 = 2x speed\n10.0 = 10x speed",
                initialvalue="1.0"
            )
            try:
                speed = float(speed_str) if speed_str else 1.0
                if speed <= 0:
                    speed = 1.0
            except:
                speed = 1.0
            
            # Confirm
            confirm = messagebox.askyesno(
                "Start Replay?",
                f"Start replay mode?\n\n"
                f"Date: {date}\n"
                f"Symbol: {symbol.upper()}\n"
                f"Speed: {speed}x\n\n"
                f"⚠️ Orders will be placed to your paper account!"
            )
            if not confirm:
                return
            
            # Initialize replay service
            from Replay.replay_service import ReplayService
            
            self.replay_service = ReplayService()
            self.replay_mode = True
            
            # Start replay in background thread
            def start_replay():
                try:
                    self.replay_service.start_replay(
                        date=date,
                        symbols=[symbol.upper()],
                        speed=speed,
                        order_mode="real"  # Use real TWS (paper account)
                    )
                except Exception as e:
                    logging.error(f"[UI] Replay start error: {e}", exc_info=True)
                    self.after(0, lambda: messagebox.showerror("Replay Error", f"Failed to start replay:\n{e}"))
                    self.after(0, self._stop_replay_mode)
            
            threading.Thread(target=start_replay, daemon=True, name="ReplayThread").start()
            
            # Update UI
            self.replay_status_label.config(
                text=f"🎬 {symbol.upper()} on {date} ({speed}x)",
                foreground="green"
            )
            logging.info(f"[UI] Replay mode started: {symbol.upper()} on {date} at {speed}x speed")
            
        except Exception as e:
            logging.error(f"[UI] Replay setup error: {e}", exc_info=True)
            messagebox.showerror("Replay Error", f"Failed to setup replay:\n{e}")
    
    def _stop_replay_mode(self):
        """Stop replay mode."""
        if self.replay_service:
            try:
                self.replay_service.stop_replay()
            except Exception as e:
                logging.error(f"[UI] Error stopping replay: {e}")
            self.replay_service = None
        
        self.replay_mode = False
        self.replay_status_label.config(text="", foreground="gray")
        logging.info("[UI] Replay mode stopped")


    # ------------------------------------------------------------------
    #  CONNECTIONS
    # ------------------------------------------------------------------
    def connect_services(self):
        if general_app.connect():
            self.banner.update_connection_status(True)
            logging.info("Services connected successfully")
        else:
            self.banner.update_connection_status(False)
            logging.error("Failed to connect services")

    def disconnect_services(self):
        general_app.disconnect()
        self.banner.update_connection_status(False)
        logging.info("Services disconnected")

    # ------------------------------------------------------------------
    #  ORDER FRAMES
    # ------------------------------------------------------------------
    def build_order_frames(self):
        """Create order frames based on spinbox value."""
        
        ex_count = len(self.order_frames)
        try:
            count = int(self.spin_count.get())
        except ValueError:
            count = 1

        if count > ex_count:
            diff = count - ex_count
            for i in range(diff):
                # --- MODIFIED: Parent is now the inner frame of the scrollable container ---
                frame = OrderFrame(self.order_container.scrollable_frame, order_id=i + 1)
                frame.pack(fill="x", pady=10, padx=10)
                self.order_frames.append(frame)
        else:
            diff = ex_count - count
            for i in range(diff):
                frame = self.order_frames.pop()
                frame.destroy()


    # ------------------------------------------------------------------
    #  DEBUG CONSOLE
    # ------------------------------------------------------------------
    def toggle_debug(self):
        if self.debug_frame and self.debug_frame.winfo_exists():
            self.debug_frame.destroy()
            self.debug_frame = None
            for h in logging.handlers[:]:
                if isinstance(h, TkinterHandler):
                    logging.removeHandler(h)
        else:
            self.debug_frame = DebugFrame(self)
            self.debug_frame.pack(fill="both", expand=True, padx=10, pady=10)
            handler = TkinterHandler(self.debug_frame)
            handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
            logging.addHandler(handler)
            logging.setLevel(logging.INFO)


# ---------- ENTRY ----------
if __name__ == "__main__":
    import atexit
    import traceback
    atexit.register(lambda: os.path.exists("arctrigger.dat") or None)

    try:
        setup_logging()
        logging.info("Starting ArcTrigger application...")
        app = ArcTriggerApp()
        # _app_instance is already set in ArcTriggerApp.__init__()
        logging.info("Application initialized successfully")
        app.mainloop()
    except Exception as e:
        error_msg = f"Fatal error during startup: {e}\n\n{traceback.format_exc()}"
        logging.critical(error_msg)
        print("\n" + "="*60)
        print("FATAL ERROR - Application failed to start")
        print("="*60)
        print(error_msg)
        print("="*60)
        input("\nPress Enter to exit...")
        raise
