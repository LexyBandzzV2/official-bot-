"""Position Sizer — calculates correct lot/unit size for 1 % account risk.

Formula
-------
    dollar_risk  = account_balance * risk_pct          (e.g. $10 000 × 0.01 = $100)
    price_risk   = entry_price - stop_loss_price        (absolute distance)
    position_size = dollar_risk / price_risk            (units of the asset)

The result is the number of units (shares / coins / lots) to trade so that
hitting the stop loss costs exactly risk_pct of the account.
"""

from __future__ import annotations


def calculate_position_size(
    account_balance: float,
    entry_price: float,
    stop_loss_price: float,
    risk_pct: float = 0.01,
) -> float:
    """Return the position size that limits loss to risk_pct of account.

    Parameters
    ----------
    account_balance : Current account equity.
    entry_price     : Price at which the trade opens (HA close of entry candle).
    stop_loss_price : Hard stop level (2 % below entry for buys).
    risk_pct        : Fraction of account to risk  (default 0.01 = 1 %).

    Returns
    -------
    float : Number of units/shares/coins.  Returns 0 if inputs are invalid.
    """
    if account_balance <= 0 or entry_price <= 0:
        return 0.0

    price_risk = abs(entry_price - stop_loss_price)
    if price_risk == 0:
        return 0.0

    dollar_risk   = account_balance * risk_pct
    position_size = dollar_risk / price_risk
    return round(position_size, 6)


def calculate_dollar_risk(
    account_balance: float,
    risk_pct: float = 0.01,
) -> float:
    """Return the maximum dollar amount to risk on one trade."""
    return round(account_balance * risk_pct, 2)


def pct_to_price(entry: float, pct: float, direction: str = "buy") -> float:
    """Convert a percentage offset to an absolute price level.

    direction='buy'  → entry * (1 - pct)   (stop below entry)
    direction='sell' → entry * (1 + pct)   (stop above entry)
    """
    if direction == "buy":
        return round(entry * (1.0 - pct), 6)
    return round(entry * (1.0 + pct), 6)
