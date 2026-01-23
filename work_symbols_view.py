# work_symbols_view.py

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import logging

from model import general_app
from Services.work_symbols import WorkSymbols, work_symbols
from Services.persistent_conid_storage import storage, PersistentConidStorage


class WorkSymbolsView(tk.Toplevel):
    def __init__(self, parent, storage: PersistentConidStorage = storage):
        super().__init__(parent)
        self.title("Work Symbols (Daily)")
        self.geometry("600x500")

        self.storage = storage
        self.work_symbols = work_symbols  # Use the global singleton instead of creating new instance
        self.entry_rows = []  # List of (entry_widget, status_label, delete_btn, symbol) tuples
        self._refresh_in_progress = False  # Guard to prevent multiple simultaneous refresh operations

        self._build_ui()
        self._load_existing_symbols()

    # -------------------------------------------------
    # UI
    # -------------------------------------------------
    def _build_ui(self):
        # Header
        header = ttk.Frame(self)
        header.pack(fill="x", padx=10, pady=5)
        ttk.Label(header, text="Work Symbols", font=("Arial", 12, "bold")).pack(side="left")
        
        # Scrollable frame for entry rows
        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.canvas = tk.Canvas(canvas_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Bind mousewheel to canvas
        def _on_mousewheel(event):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)
        
        # Column headers
        header_row = ttk.Frame(self.scrollable_frame)
        header_row.pack(fill="x", pady=(0, 5))
        ttk.Label(header_row, text="Symbol", font=("Arial", 9, "bold"), width=15).pack(side="left", padx=5)
        ttk.Label(header_row, text="Status", font=("Arial", 9, "bold"), width=10).pack(side="left", padx=5)
        ttk.Label(header_row, text="Action", font=("Arial", 9, "bold"), width=10).pack(side="left", padx=5)

        # Bottom buttons
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=10)
        self.refresh_btn = ttk.Button(bottom, text="Refresh All ConIDs", command=self._refresh_conids)
        self.refresh_btn.pack(side="left", padx=5)
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right", padx=5)

    # -------------------------------------------------
    # Logic
    # -------------------------------------------------
    def _create_entry_row(self, symbol="", is_new_row=False):
        """Create a new entry row. If is_new_row=True, it's an empty row for adding new symbols."""
        row_frame = ttk.Frame(self.scrollable_frame)
        row_frame.pack(fill="x", pady=2)
        
        entry = ttk.Entry(row_frame, width=15, font=("Arial", 10))
        entry.pack(side="left", padx=5)
        
        status_label = ttk.Label(row_frame, text="", width=10)
        status_label.pack(side="left", padx=5)
        
        delete_btn = ttk.Button(row_frame, text="✕", width=3, command=lambda: self._remove_row(row_frame))
        delete_btn.pack(side="left", padx=5)
        
        if symbol:
            entry.insert(0, symbol)
            entry.config(state="readonly")  # Make existing symbols read-only
            status = "✅" if self.work_symbols.get_ready_symbols().get(symbol, False) else "❌"
            status_label.config(text=status)
        
        if is_new_row:
            entry.focus()
            entry.bind("<Return>", lambda e: self._on_entry_enter(entry, row_frame))
            entry.bind("<FocusOut>", lambda e: self._on_entry_focus_out(e, entry, row_frame))
        
        self.entry_rows.append((entry, status_label, delete_btn, symbol if symbol else None, row_frame))
        return row_frame
    
    def _on_entry_enter(self, entry, row_frame):
        """Handle Enter key press in entry field"""
        symbol = entry.get().strip().upper()
        if not symbol:
            return
        
        # Add symbol to work_symbols
        self.work_symbols.add_symbol(symbol)
        
        # Update this row to show the symbol (make it read-only)
        entry.config(state="readonly")
        entry.unbind("<Return>")
        entry.unbind("<FocusOut>")
        status = "❌"  # New symbol, conID not ready yet
        idx = next(i for i, (e, _, _, _, _) in enumerate(self.entry_rows) if e == entry)
        self.entry_rows[idx] = (entry, self.entry_rows[idx][1], self.entry_rows[idx][2], symbol, row_frame)
        self.entry_rows[idx][1].config(text=status)
        
        # Create new empty row below and focus it
        new_row = self._create_entry_row(is_new_row=True)
        # Focus the new entry after a short delay to ensure it's created
        def focus_new_entry():
            for entry_widget, _, _, symbol, _ in self.entry_rows:
                if symbol is None:  # Empty row
                    entry_widget.focus()
                    break
        self.after(10, focus_new_entry)
    
    def _on_entry_focus_out(self, event, entry, row_frame):
        """Handle focus out - if empty and not the last empty row, remove it"""
        symbol = entry.get().strip().upper()
        if not symbol:
            # Count how many empty rows exist
            empty_count = sum(1 for _, _, _, s, _ in self.entry_rows if s is None)
            # Only remove if there's more than one empty row
            if empty_count > 1:
                self._remove_row(row_frame)
    
    def _remove_row(self, row_frame):
        """Remove a row and the symbol from work_symbols"""
        # Find the row in entry_rows
        for i, (entry, status_label, delete_btn, symbol, rf) in enumerate(self.entry_rows):
            if rf == row_frame:
                if symbol:
                    self.work_symbols.remove_symbol(symbol)
                row_frame.destroy()
                self.entry_rows.pop(i)
                break
    
    def _load_existing_symbols(self):
        """Load existing symbols from work_symbols and create rows"""
        symbols = list(self.work_symbols.get_ready_symbols().keys())
        for symbol in symbols:
            self._create_entry_row(symbol=symbol, is_new_row=False)
        # Add one empty row at the end for new entries
        self._create_entry_row(is_new_row=True)
    
    def _refresh_conids(self):
        # Guard: Prevent multiple simultaneous refresh operations
        if self._refresh_in_progress:
            messagebox.showwarning("Warning", "Refresh already in progress. Please wait for it to complete.")
            return
        
        if not general_app.is_connected:
            messagebox.showerror("Error", "TWS not connected")
            return

        symbols = list(self.work_symbols.get_ready_symbols().keys())
        if not symbols:
            messagebox.showwarning("Warning", "No symbols to refresh. Please add symbols first.")
            return

        # Show confirmation
        response = messagebox.askyesno(
            "Refresh All ConIDs",
            f"Refresh stock and option conIDs for {len(symbols)} symbol(s)?\n\n"
            f"This will:\n"
            f"1. Refresh stock conIDs\n"
            f"2. Cache option conIDs (Friday expiry, ±10 strikes, CALL & PUT)\n\n"
            f"Symbols: {', '.join(symbols[:5])}{'...' if len(symbols) > 5 else ''}\n\n"
            f"This may take a few minutes."
        )

        if not response:
            return

        # Disable button and set flag
        self._refresh_in_progress = True
        self.refresh_btn.config(state="disabled", text="Refreshing...")

        def worker():
            try:
                from Services.option_conid_cache_service import cache_option_conids_for_all_symbols
                from Services.polygon_service import polygon_service
                from Services.tws_service import create_tws_service
                
                tws_service = create_tws_service()
                
                # Step 1: Refresh stock conIDs
                logging.info("[WorkSymbolsView] Step 1: Refreshing stock conIDs...")
                self.work_symbols.refresh_all_conids()
                
                # Step 2: Cache option conIDs
                logging.info("[WorkSymbolsView] Step 2: Caching option conIDs...")
                results = cache_option_conids_for_all_symbols(
                    tws_service, polygon_service, self.storage, num_strikes=10
                )
                
                # Calculate totals
                total_cached = sum(cached for cached, _ in results.values())
                total_attempted = sum(attempted for _, attempted in results.values())
                
                # Update UI and show results
                def update_ui():
                    self._update_status_labels()
                    self._refresh_in_progress = False
                    self.refresh_btn.config(state="normal", text="Refresh All ConIDs")
                    messagebox.showinfo(
                        "Refresh Complete",
                        f"✅ Stock conIDs refreshed\n"
                        f"✅ Option conIDs cached: {total_cached}/{total_attempted}\n\n"
                        f"Symbols processed: {len(symbols)}\n"
                        f"Success rate: {(total_cached/total_attempted*100) if total_attempted > 0 else 0:.1f}%"
                    )
                
                self.after(0, update_ui)
                logging.info(f"[WorkSymbolsView] Refresh complete: {total_cached}/{total_attempted} option conIDs cached")
            except Exception as e:
                logging.error(f"[WorkSymbolsView] Error refreshing conIDs: {e}")
                def show_error():
                    self._update_status_labels()
                    self._refresh_in_progress = False
                    self.refresh_btn.config(state="normal", text="Refresh All ConIDs")
                    messagebox.showerror("Error", f"Failed to refresh conIDs:\n{str(e)}")
                self.after(0, show_error)

        threading.Thread(target=worker, daemon=True).start()
    
    def _update_status_labels(self):
        """Update status labels for all rows based on current work_symbols state"""
        ready_symbols = self.work_symbols.get_ready_symbols()
        for entry, status_label, delete_btn, symbol, row_frame in self.entry_rows:
            if symbol:
                status = "✅" if ready_symbols.get(symbol, False) else "❌"
                status_label.config(text=status)
