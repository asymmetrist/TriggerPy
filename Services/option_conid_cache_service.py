# option_conid_cache_service.py

import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from Services.persistent_conid_storage import PersistentConidStorage
from Services.tws_service import TWSService
from Services.polygon_service import PolygonService
from Services.work_symbols import work_symbols


def get_this_week_friday_expiry(symbol: str, tws_service: TWSService) -> Optional[str]:
    """
    Get this week's Friday expiry from TWS expirations.
    If Friday is a holiday (not in TWS expirations), use Thursday instead.
    
    Returns: YYYYMMDD string or None if not found
    """
    # Get actual expirations from TWS
    maturities = tws_service.get_maturities(symbol)
    if not maturities or not maturities.get('expirations'):
        logging.warning(f"[OptionConIDCache] No expirations available for {symbol}")
        return None
    
    expirations = set(maturities['expirations'])
    today = datetime.now()
    
    # Calculate this week's Friday
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0:
        # Today is Friday
        friday_date = today
    else:
        friday_date = today + timedelta(days=days_until_friday)
    
    friday_str = friday_date.strftime("%Y%m%d")
    
    # 1. Try Friday first
    if friday_str in expirations:
        logging.info(f"[OptionConIDCache] Using Friday expiry for {symbol}: {friday_str}")
        return friday_str
    
    # 2. Friday is holiday → Try Thursday
    thursday_date = friday_date - timedelta(days=1)
    thursday_str = thursday_date.strftime("%Y%m%d")
    
    if thursday_str in expirations:
        logging.info(f"[OptionConIDCache] Friday is holiday, using Thursday expiry for {symbol}: {thursday_str}")
        return thursday_str
    
    # 3. Both Friday and Thursday not available → Find nearest available expiry before Friday
    nearest_expiry = None
    nearest_date = None
    
    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str, "%Y%m%d")
            # Must be before or equal to Friday, and in the future (or today)
            if exp_date <= friday_date and exp_date >= today:
                if nearest_date is None or exp_date > nearest_date:
                    nearest_date = exp_date
                    nearest_expiry = exp_str
        except ValueError:
            continue
    
    if nearest_expiry:
        logging.info(f"[OptionConIDCache] Friday/Thursday not available, using nearest expiry for {symbol}: {nearest_expiry}")
        return nearest_expiry
    
    # 4. Fallback: Use most recent available expiry (shouldn't happen in practice)
    all_dates = []
    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str, "%Y%m%d")
            if exp_date >= today:
                all_dates.append((exp_date, exp_str))
        except ValueError:
            continue
    
    if all_dates:
        all_dates.sort(key=lambda x: x[0])
        nearest_expiry = all_dates[0][1]
        logging.warning(f"[OptionConIDCache] Using fallback expiry for {symbol}: {nearest_expiry}")
        return nearest_expiry
    
    logging.warning(f"[OptionConIDCache] No suitable expiry found for {symbol}")
    return None


def cache_option_conids_for_symbol(
    symbol: str,
    tws_service: TWSService,
    polygon_service: PolygonService,
    storage: PersistentConidStorage,
    num_strikes: int = 10
) -> Tuple[int, int]:
    """
    Cache option conIDs for this week's Friday expiry, ±num_strikes from current price.
    
    Args:
        symbol: Stock symbol
        tws_service: TWS service instance
        polygon_service: Polygon service instance
        storage: Persistent storage instance
        num_strikes: Number of strikes above and below current price (default: 10)
    
    Returns:
        Tuple of (cached_count, total_attempted)
    """
    logging.info(f"[OptionConIDCache] Starting cache for {symbol} (±{num_strikes} strikes)")
    
    # 1. Get current stock price
    current_price = polygon_service.get_last_trade(symbol)
    if not current_price or current_price <= 0:
        logging.warning(f"[OptionConIDCache] Cannot get price for {symbol}")
        return (0, 0)
    
    logging.info(f"[OptionConIDCache] Current price for {symbol}: {current_price}")
    
    # 2. Get this week's Friday expiry from TWS (handles holidays automatically)
    expiry = get_this_week_friday_expiry(symbol, tws_service)
    if not expiry:
        logging.warning(f"[OptionConIDCache] No Friday expiry found for {symbol}")
        return (0, 0)
    
    # 3. Get valid strikes from option chain
    maturities = tws_service.get_maturities(symbol)
    if not maturities or not maturities.get('strikes'):
        logging.warning(f"[OptionConIDCache] No strikes available for {symbol}")
        return (0, 0)
    
    valid_strikes = sorted(maturities['strikes'])
    
    # 4. Filter to ±num_strikes from current price
    strikes_to_cache = [
        s for s in valid_strikes 
        if current_price - num_strikes <= s <= current_price + num_strikes
    ]
    
    if not strikes_to_cache:
        logging.warning(f"[OptionConIDCache] No strikes in range for {symbol} (price={current_price}, range=[{current_price - num_strikes}, {current_price + num_strikes}])")
        return (0, 0)
    
    logging.info(f"[OptionConIDCache] Caching {len(strikes_to_cache)} strikes for {symbol} {expiry}")
    
    # 5. Cache conIDs for each strike (CALL and PUT)
    cached_count = 0
    total_attempted = 0
    
    for strike in strikes_to_cache:
        for right in ["C", "P"]:  # CALL and PUT
            total_attempted += 1
            try:
                contract = tws_service.create_option_contract(symbol, expiry, strike, right)
                conid = tws_service.resolve_conid(contract, timeout=15)
                if conid:
                    storage.store_option_conid(symbol, expiry, strike, right, str(conid))
                    cached_count += 1
                    logging.info(f"[OptionConIDCache] ✅ Cached {symbol} {expiry} {strike}{right} → {conid}")
                else:
                    logging.warning(f"[OptionConIDCache] ❌ Failed to resolve conID for {symbol} {expiry} {strike}{right}")
            except Exception as e:
                logging.error(f"[OptionConIDCache] Error caching {symbol} {expiry} {strike}{right}: {e}")
    
    logging.info(f"[OptionConIDCache] Completed: Cached {cached_count}/{total_attempted} option conIDs for {symbol} (expiry={expiry})")
    return (cached_count, total_attempted)


def cache_option_conids_for_all_symbols(
    tws_service: TWSService,
    polygon_service: PolygonService,
    storage: PersistentConidStorage,
    num_strikes: int = 10
) -> dict:
    """
    Cache option conIDs for all symbols in Work Symbols.
    
    Returns:
        Dict with summary: {symbol: (cached_count, total_attempted), ...}
    """
    if not tws_service.is_connected():
        logging.error("[OptionConIDCache] TWS not connected - cannot cache option conIDs")
        return {}
    
    symbols = list(work_symbols.get_ready_symbols().keys())
    if not symbols:
        logging.warning("[OptionConIDCache] No symbols in Work Symbols to cache")
        return {}
    
    logging.info(f"[OptionConIDCache] Starting cache for {len(symbols)} symbols")
    
    results = {}
    total_cached = 0
    total_attempted = 0
    
    for symbol in symbols:
        try:
            cached, attempted = cache_option_conids_for_symbol(
                symbol, tws_service, polygon_service, storage, num_strikes
            )
            results[symbol] = (cached, attempted)
            total_cached += cached
            total_attempted += attempted
        except Exception as e:
            logging.error(f"[OptionConIDCache] Error caching {symbol}: {e}")
            results[symbol] = (0, 0)
    
    logging.info(f"[OptionConIDCache] Overall: Cached {total_cached}/{total_attempted} option conIDs across {len(symbols)} symbols")
    return results
