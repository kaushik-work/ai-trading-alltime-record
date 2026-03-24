"""
Indian NSE F&O (Index Options) Trading Charges Calculator
All rates as of FY 2024-25.

Applicable to NIFTY and BANKNIFTY weekly/monthly options.

Charge breakdown per round-trip trade:
┌─────────────────────────────┬──────────────────────────────────────────┐
│ Charge                      │ Rate / Basis                             │
├─────────────────────────────┼──────────────────────────────────────────┤
│ Brokerage                   │ ₹20 flat per order (discount broker)     │
│ STT (Sell side only)        │ 0.0625% of sell-leg premium turnover     │
│ NSE Exchange Transaction    │ 0.053% of total premium turnover         │
│ SEBI Turnover Fee           │ ₹10 per crore = 0.000001 of turnover     │
│ Stamp Duty (Buy side only)  │ 0.003% of buy-leg premium turnover       │
│ GST                         │ 18% on (Brokerage + Exchange + SEBI)     │
└─────────────────────────────┴──────────────────────────────────────────┘

Notes:
- STT: charged only on the SELL leg (when you square off / sell the option).
  For options bought and squared off intraday, STT = 0.0625% of sell premium.
  For exercised ITM contracts at expiry, STT = 0.125% of intrinsic value
  (settlement price × lot_size) — much higher. This module covers the
  intraday/square-off case only.
- Stamp Duty: charged only on BUY orders (as per 2020 amendment, collected
  at exchange level, uniform rate 0.003%).
- GST (18%) applies to Brokerage + Exchange charges + SEBI fees.
  NOT on STT or Stamp Duty.
- DP charges: not applicable for F&O.
- All rupee values are per round-trip (one buy + one sell).
"""

# ── Charge rates ─────────────────────────────────────────────────────────────

BROKERAGE_PER_ORDER  = 20.0       # ₹ flat per order — typical discount broker
STT_SELL_RATE        = 0.000625   # 0.0625 % on sell-leg premium value
NSE_EXCHANGE_RATE    = 0.00053    # 0.053 % on total (buy+sell) premium turnover
SEBI_RATE            = 0.000001   # ₹10 per crore = 1e-6 of turnover
STAMP_BUY_RATE       = 0.00003    # 0.003 % on buy-leg premium value only
GST_RATE             = 0.18       # 18 % on brokerage + exchange + SEBI


# ── ATM premium estimator ─────────────────────────────────────────────────────

def estimate_atm_premium(atr: float, dte: float = 5.0) -> float:
    """
    Rough ATM option premium estimate for charges calculation.

    ATM premium scales with √DTE (Black-Scholes approximation):
        premium ≈ ATR × 0.5 × √DTE

    Examples at typical NIFTY ATR=50:
        DTE=4  → ₹50   (current week, 4 days left)
        DTE=9  → ₹75   (next week, standard switch threshold)
        DTE=11 → ₹83   (next week, Friday entry)

    Only used for charges computation — real premiums vary with IV skew.
    """
    return max(1.0, atr * 0.5 * (dte ** 0.5))


# ── Per-trade charge calculator ───────────────────────────────────────────────

def compute_charges(
    entry_premium_per_unit: float,   # estimated ATM premium at entry (pts)
    exit_premium_per_unit: float,    # estimated ATM premium at exit (pts)
    lot_size: int,                   # NSE lot size  (NIFTY=75, BANKNIFTY=15)
    num_lots: int,                   # number of lots traded
) -> dict:
    """
    Compute itemised charges for one round-trip NSE index options trade.

    Returns a dict with each charge component and the total in ₹.
    """
    exit_premium_per_unit = max(0.05, exit_premium_per_unit)  # premium can't be < 0.05

    buy_turnover  = entry_premium_per_unit * lot_size * num_lots
    sell_turnover = exit_premium_per_unit  * lot_size * num_lots
    total_turnover = buy_turnover + sell_turnover

    brokerage = BROKERAGE_PER_ORDER * 2               # one buy + one sell order
    stt       = sell_turnover  * STT_SELL_RATE        # STT only on sell leg
    exchange  = total_turnover * NSE_EXCHANGE_RATE    # NSE fee on both legs
    sebi      = total_turnover * SEBI_RATE            # SEBI turnover fee
    stamp     = buy_turnover   * STAMP_BUY_RATE       # Stamp only on buy leg
    gst       = (brokerage + exchange + sebi) * GST_RATE  # GST on service charges

    total = brokerage + stt + exchange + sebi + stamp + gst

    return {
        "brokerage": round(brokerage, 2),
        "stt":       round(stt,       2),
        "exchange":  round(exchange,  2),
        "sebi":      round(sebi,      2),
        "stamp":     round(stamp,     2),
        "gst":       round(gst,       2),
        "total":     round(total,     2),
    }


# ── Convenience: charges for a trade given gross P&L ─────────────────────────

def charges_for_trade(
    entry_atr:    float,    # ATR at entry bar (used to estimate entry premium)
    gross_pnl:    float,    # gross P&L in ₹ (before charges)
    lot_size:     int,
    num_lots:     int,
    opt_delta:    float = 0.45,
    dte:          float = 5.0,   # calendar days to expiry at entry
) -> dict:
    """
    Compute charges when you have gross P&L but not explicit exit premium.

    exit_premium is back-derived from gross_pnl:
        premium_change_per_unit = gross_pnl / (opt_delta × lot_size × num_lots)
        exit_premium = entry_premium + premium_change_per_unit
    """
    entry_premium = estimate_atm_premium(entry_atr, dte)
    units         = lot_size * num_lots
    if units > 0 and opt_delta > 0:
        premium_change = gross_pnl / (opt_delta * units)
    else:
        premium_change = 0.0
    exit_premium = entry_premium + premium_change
    return compute_charges(entry_premium, exit_premium, lot_size, num_lots)
