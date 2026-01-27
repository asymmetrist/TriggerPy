"""
Validation helpers for ArcTrigger
Provides safe, defensive validation functions to prevent crashes
"""
import logging
from typing import Optional, Union


def safe_float(value: Union[str, int, float, None], default: Optional[float] = None) -> Optional[float]:
    """
    Safely convert value to float, returning None or default on failure.
    
    Args:
        value: Value to convert
        default: Default value if conversion fails (None if not provided)
    
    Returns:
        float value or default/None
    """
    if value is None:
        return default
    
    try:
        result = float(value)
        return result if result > 0 else default
    except (ValueError, TypeError) as e:
        logging.debug(f"safe_float conversion failed: {value} -> {e}")
        return default


def safe_int(value: Union[str, int, float, None], default: Optional[int] = None) -> Optional[int]:
    """
    Safely convert value to int, returning None or default on failure.
    """
    if value is None:
        return default
    
    try:
        result = int(float(value))  # Convert via float to handle "123.0"
        return result if result > 0 else default
    except (ValueError, TypeError) as e:
        logging.debug(f"safe_int conversion failed: {value} -> {e}")
        return default


def validate_strike(strike: Union[str, int, float, None]) -> tuple[bool, Optional[float], str]:
    """
    Validate strike price.
    
    Returns:
        (is_valid, strike_value, error_message)
    """
    if strike is None:
        return False, None, "Strike is None"
    
    if isinstance(strike, str):
        strike = strike.strip()
        if not strike:
            return False, None, "Strike is empty"
    
    strike_value = safe_float(strike)
    
    if strike_value is None:
        return False, None, f"Invalid strike format: {strike}"
    
    if strike_value <= 0:
        return False, None, f"Strike must be positive: {strike_value}"
    
    return True, strike_value, ""


def validate_trigger_price(trigger: Union[str, int, float, None]) -> tuple[bool, Optional[float], str]:
    """
    Validate trigger price.
    
    Returns:
        (is_valid, trigger_value, error_message)
    """
    if trigger is None:
        return False, None, "Trigger price is None"
    
    if isinstance(trigger, str):
        trigger = trigger.strip()
        if not trigger:
            return False, None, "Trigger price is empty"
    
    trigger_value = safe_float(trigger)
    
    if trigger_value is None:
        return False, None, f"Invalid trigger format: {trigger}"
    
    if trigger_value <= 0:
        return False, None, f"Trigger must be positive: {trigger_value}"
    
    return True, trigger_value, ""


def validate_quantity(qty: Union[str, int, float, None]) -> tuple[bool, Optional[int], str]:
    """
    Validate order quantity.
    
    Returns:
        (is_valid, quantity_value, error_message)
    """
    if qty is None:
        return False, None, "Quantity is None"
    
    if isinstance(qty, str):
        qty = qty.strip()
        if not qty:
            return False, None, "Quantity is empty"
    
    qty_value = safe_int(qty)
    
    if qty_value is None:
        return False, None, f"Invalid quantity format: {qty}"
    
    if qty_value <= 0:
        return False, None, f"Quantity must be positive: {qty_value}"
    
    return True, qty_value, ""


def validate_symbol(symbol: Optional[str]) -> tuple[bool, Optional[str], str]:
    """
    Validate trading symbol.
    
    Returns:
        (is_valid, symbol_value, error_message)
    """
    if not symbol:
        return False, None, "Symbol is empty or None"
    
    symbol = symbol.strip().upper()
    
    if not symbol:
        return False, None, "Symbol is empty after trimming"
    
    if len(symbol) < 1 or len(symbol) > 10:
        return False, None, f"Symbol length invalid: {len(symbol)}"
    
    # Basic validation - alphanumeric and some special chars
    if not all(c.isalnum() or c in ['.', '-'] for c in symbol):
        return False, None, f"Symbol contains invalid characters: {symbol}"
    
    return True, symbol, ""


def validate_option_right(right: Optional[str]) -> tuple[bool, Optional[str], str]:
    """
    Validate option right (CALL/PUT).
    
    Returns:
        (is_valid, right_value, error_message)
    """
    if not right:
        return False, None, "Option right is empty"
    
    right = right.strip().upper()
    
    if right in ["CALL", "C"]:
        return True, "C", ""
    elif right in ["PUT", "P"]:
        return True, "P", ""
    else:
        return False, None, f"Invalid option right: {right} (expected CALL/PUT or C/P)"


def validate_maturity(maturity: Optional[str]) -> tuple[bool, Optional[str], str]:
    """
    Validate option maturity/expiry date.
    
    Returns:
        (is_valid, maturity_value, error_message)
    """
    if not maturity:
        return False, None, "Maturity is empty"
    
    maturity = maturity.strip()
    
    # Basic format check (YYYYMMDD or similar)
    if len(maturity) < 6:
        return False, None, f"Maturity format too short: {maturity}"
    
    return True, maturity, ""
