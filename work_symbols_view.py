# work_symbols_view.py

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import logging

from model import general_app
from Services.work_symbols import WorkSymbols
from Services.persistent_conid_storage import storage, PersistentConidStorage


class WorkSymbolsView(tk.Toplevel):
    def __init__(self, parent, storage: PersistentConidStorage = storage):
        super().__init__(parent)
        self.title("Work Symbols (Daily)")
        self.geometry("700x500")

        self.storage = storage
        self.work_symbols = WorkSymbols(self.storage)

        self._build_ui()
        self._refresh_list()

    # -------------------------------------------------
    # UI
    # -------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=10)

        ttk.Label(top, text="Search Symbol").pack(side="left")
        self.entry_search = ttk.Entry(top, width=20)
        self.entry_search.pack(side="left", padx=5)
        self.entry_search.bind("<KeyRelease>", self._on_search)

        self.combo_results = ttk.Combobox(top, width=25)
        self.combo_results.pack(side="left", padx=5)

        ttk.Button(top, text="Add", command=self._add_symbol).pack(side="left", padx=5)

        # -------------------------------------------------
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=10, pady=10)

        cols = ("Symbol", "ConID Ready")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings")
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, anchor="center")

        self.tree.pack(fill="both", expand=True)

        # -------------------------------------------------
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=10)

        ttk.Button(bottom, text="Remove Selected", command=self._remove_selected).pack(side="left")
        ttk.Button(bottom, text="Refresh All ConIDs", command=self._refresh_conids).pack(side="left", padx=10)
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right")

    # -------------------------------------------------
    # Logic
    # -------------------------------------------------
    def _on_search(self, event=None):
        query = self.entry_search.get().upper()
        if len(query) < 2:
            self.combo_results["values"] = ()
            return

        def worker():
            try:
                results = general_app.search_symbol(query) or []
                values = [
                    r["symbol"]
                    for r in results
                    if (r.get("primaryExchange") or "").upper() in {"NASDAQ", "NYSE"}
                ]
            except Exception as e:
                logging.error(f"Search error: {e}")
                values = []

            self.after(0, lambda: self.combo_results.configure(values=values))

        threading.Thread(target=worker, daemon=True).start()

    def _add_symbol(self):
        symbol = self.combo_results.get().strip().upper()
        if not symbol:
            return
        self.work_symbols.add_symbol(symbol)
        self._refresh_list()

    def _remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        symbol = self.tree.item(sel[0])["values"][0]
        self.work_symbols.remove_symbol(symbol)
        self._refresh_list()

    def _refresh_conids(self):
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
                    self._refresh_list()
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
                    messagebox.showerror("Error", f"Failed to refresh conIDs:\n{str(e)}")
                    self._refresh_list()  # Still refresh the list even if there was an error
                self.after(0, show_error)

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        for symbol, ready in self.work_symbols.get_ready_symbols().items():
            self.tree.insert("", "end", values=(symbol, "✅" if ready else "❌"))
