# work_symbols.py

from typing import Dict
from Services.persistent_conid_storage import storage, PersistentConidStorage
from Services.tws_service import create_tws_service
from ibapi.contract import Contract
import logging

class WorkSymbols:
    """
    Maintains an in-memory map of symbols -> bool
    True  = a conid exists in DB for this symbol
    False = no conid stored (or symbol known but unresolved)
    """

    def __init__(self, storage: PersistentConidStorage = storage):
        self.storage = storage
        self.symbols: Dict[str, bool] = {}
        # Load persisted symbols on initialization
        self._load_persisted_symbols()


    def refresh_all_conids(self) -> None:
        """
        Force-refresh UNDERLYING conids for all tracked option symbols.
        These conids are used as the base for OPT trading.
        """
        

        tws = create_tws_service()

        if not tws.is_connected():
            logging.error("[WorkSymbols] TWS not connected – cannot refresh underlying conids")
            return

        for symbol in list(self.symbols.keys()):
            try:
                logging.info(f"[WorkSymbols] Resolving underlying conid for {symbol}")

                # ⚠️ IMPORTANT:
                # This STK contract is ONLY used to resolve the UNDERLYING conId
                # Required by IB for option chains and OPT contracts
                underlying_contract = Contract()
                underlying_contract.symbol = symbol
                underlying_contract.secType = "STK"      # resolver-only
                underlying_contract.exchange = "SMART"
                underlying_contract.currency = "USD"

                conid = tws.resolve_conid(underlying_contract)

                if conid:
                    self.storage.store_conid(symbol, str(conid))
                    self.symbols[symbol] = True
                    logging.info(
                        f"[WorkSymbols] ✅ Underlying conid stored for {symbol}: {conid}"
                    )
                else:
                    self.symbols[symbol] = False
                    logging.warning(
                        f"[WorkSymbols] ❌ Failed to resolve underlying conid for {symbol}"
                    )

            except Exception as e:
                self.symbols[symbol] = False
                logging.exception(
                    f"[WorkSymbols] Exception while resolving underlying conid for {symbol}: {e}"
                )


    def add_symbol(self, symbol: str) -> None:
        """
        Add a symbol to tracking.
        Initial value is determined by DB state.
        Persists the symbol to database.
        """
        symbol = symbol.upper()
        self.symbols[symbol] = self.storage.get_conid(symbol) is not None
        # Persist to database
        self.storage.save_work_symbol(symbol)

    def remove_symbol(self, symbol: str) -> None:
        """
        Remove a symbol from tracking and from persisted list.
        """
        symbol = symbol.upper()
        self.symbols.pop(symbol, None)
        # Remove from persisted list
        self.storage.remove_work_symbol(symbol)

    def has_symbol(self, symbol: str) -> bool:
        """
        Check if symbol is tracked.
        """
        return symbol.upper() in self.symbols

    def check(self) -> None:
        """
        Re-check DB for all tracked symbols and update internal state.
        """
        for symbol in list(self.symbols.keys()):
            self.symbols[symbol] = self.storage.get_conid(symbol) is not None

    def get_ready_symbols(self) -> Dict[str, bool]:
        """
        Return a copy of the internal symbol map.
        """
        return dict(self.symbols)

    def unresolved_symbols(self) -> Dict[str, bool]:
        """
        Return symbols without conids.
        """
        return {s: v for s, v in self.symbols.items() if not v}

    def resolved_symbols(self) -> Dict[str, bool]:
        """
        Return symbols with conids.
        """
        return {s: v for s, v in self.symbols.items() if v}
    
    def _load_persisted_symbols(self) -> None:
        """
        Load persisted symbols from database on initialization.
        """
        persisted_symbols = self.storage.load_work_symbols()
        for symbol in persisted_symbols:
            self.symbols[symbol] = self.storage.get_conid(symbol) is not None
    
    def refresh_symbol_conid(self, symbol: str) -> bool:
        """
        Refresh conID for a single symbol.
        Returns True if conID was successfully resolved and stored.
        """
        symbol = symbol.upper()
        tws = create_tws_service()
        
        if not tws.is_connected():
            logging.error(f"[WorkSymbols] TWS not connected – cannot refresh conID for {symbol}")
            return False
        
        try:
            logging.info(f"[WorkSymbols] Resolving underlying conid for {symbol}")
            
            from ibapi.contract import Contract
            underlying_contract = Contract()
            underlying_contract.symbol = symbol
            underlying_contract.secType = "STK"
            underlying_contract.exchange = "SMART"
            underlying_contract.currency = "USD"
            
            conid = tws.resolve_conid(underlying_contract)
            
            if conid:
                self.storage.store_conid(symbol, str(conid))
                self.symbols[symbol] = True
                logging.info(f"[WorkSymbols] ✅ Underlying conid stored for {symbol}: {conid}")
                return True
            else:
                self.symbols[symbol] = False
                logging.warning(f"[WorkSymbols] ❌ Failed to resolve underlying conid for {symbol}")
                return False
        except Exception as e:
            self.symbols[symbol] = False
            logging.exception(f"[WorkSymbols] Exception while resolving underlying conid for {symbol}: {e}")
            return False

work_symbols = WorkSymbols()
# Example usage:
# storage = PersistentConidStorage()
# ws = WorkSymbols(storage)
# ws.add_symbol("AAPL")
# ws.add_symbol("TSLA")
# ws.check()
# print(ws.get_ready_symbols())
