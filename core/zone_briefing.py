"""
Pre-market Zone Briefing — runs at 9:00 AM IST.

Identifies the 3-5 highest-conviction price zones for the trading day
using weekly and daily NIFTY bars. These zones are stored in IPC and
used by the strategy to:
  1. Enter immediately when price touches a demand zone (buy CE)
  2. Enter immediately when price touches a supply zone (buy PE)
  3. Skip entries when price is in "open air" between zones

Zone types:
  demand_zone : weekly/daily support — buy CE when price enters
  supply_zone : weekly/daily resistance — buy PE when price enters

This replaces the "chase the indicator" approach with a "wait at the zone"
approach. Entry is defined by WHERE price is, not by indicator confirmation.
"""

import logging
from typing import Optional
import time

import pandas as pd

logger = logging.getLogger(__name__)

_TODAY_ZONES: list = []
_ZONES_TS: float = 0.0
_ZONES_DATE: str = ""


def compute_daily_zones(df_5m: pd.DataFrame, df_daily: Optional[pd.DataFrame] = None) -> list:
    """
    Compute today's watch zones from 5m intraday bars + daily bars.

    Returns list of zone dicts:
      {type, top, bottom, mid, strength, source, direction}
    """
    zones = []
    if df_5m is None or len(df_5m) < 10:
        return zones

    df_5m = df_5m.copy()
    for col in ["High", "Low", "Close", "Open"]:
        if col not in df_5m.columns and col.lower() in df_5m.columns:
            df_5m[col] = df_5m[col.lower()]

    current_price = float(df_5m["Close"].iloc[-1])

    # ── Previous day high / low (strongest intraday levels) ──────────────────
    df_5m.index = pd.to_datetime(df_5m.index)
    df_5m["_date"] = df_5m.index.date
    dates = sorted(df_5m["_date"].unique())

    if len(dates) >= 2:
        prev_day = df_5m[df_5m["_date"] == dates[-2]]
        pdh = float(prev_day["High"].max())
        pdl = float(prev_day["Low"].min())
        pdo = float(prev_day["Open"].iloc[0])
        pdc = float(prev_day["Close"].iloc[-1])
        body_top = max(pdo, pdc)
        body_bot = min(pdo, pdc)

        zones.append({
            "source":    "PDH",
            "type":      "supply" if pdh > current_price else "demand",
            "direction": "PE" if pdh > current_price else "CE",
            "top":       round(pdh + 10, 0),
            "bottom":    round(pdh - 15, 0),
            "mid":       round(pdh, 0),
            "strength":  4,
            "note":      f"Previous Day High ₹{pdh:.0f}",
        })
        zones.append({
            "source":    "PDL",
            "type":      "demand" if pdl < current_price else "supply",
            "direction": "CE" if pdl < current_price else "PE",
            "top":       round(pdl + 15, 0),
            "bottom":    round(pdl - 10, 0),
            "mid":       round(pdl, 0),
            "strength":  4,
            "note":      f"Previous Day Low ₹{pdl:.0f}",
        })
        # Previous day body zone (open-close range) — high conviction flip zone
        zones.append({
            "source":    "PD_BODY",
            "type":      "demand" if body_bot < current_price else "supply",
            "direction": "CE" if body_bot < current_price else "PE",
            "top":       round(body_top, 0),
            "bottom":    round(body_bot, 0),
            "mid":       round((body_top + body_bot) / 2, 0),
            "strength":  3,
            "note":      f"Prev day body ₹{body_bot:.0f}–₹{body_top:.0f}",
        })

    # ── Weekly swing zones from 5m bars (last 5 days) ────────────────────────
    if len(dates) >= 5:
        week_data = df_5m[df_5m["_date"].isin(dates[-5:])]
        week_high = float(week_data["High"].max())
        week_low  = float(week_data["Low"].min())

        zones.append({
            "source":    "WEEKLY_HIGH",
            "type":      "supply",
            "direction": "PE",
            "top":       round(week_high + 20, 0),
            "bottom":    round(week_high - 30, 0),
            "mid":       round(week_high, 0),
            "strength":  5,
            "note":      f"Weekly High ₹{week_high:.0f} — HPS Supply",
        })
        zones.append({
            "source":    "WEEKLY_LOW",
            "type":      "demand",
            "direction": "CE",
            "top":       round(week_low + 30, 0),
            "bottom":    round(week_low - 20, 0),
            "mid":       round(week_low, 0),
            "strength":  5,
            "note":      f"Weekly Low ₹{week_low:.0f} — HPS Demand",
        })

    # ── Swing zones from current day (ORB + VWAP) ────────────────────────────
    if len(dates) >= 1:
        today = df_5m[df_5m["_date"] == dates[-1]]
        if len(today) >= 3:
            orb = today.iloc[:3]
            orb_high = float(orb["High"].max())
            orb_low  = float(orb["Low"].min())

            zones.append({
                "source":    "ORB_HIGH",
                "type":      "supply" if orb_high > current_price else "demand",
                "direction": "PE" if orb_high > current_price else "CE",
                "top":       round(orb_high + 10, 0),
                "bottom":    round(orb_high - 10, 0),
                "mid":       round(orb_high, 0),
                "strength":  3,
                "note":      f"Opening Range High ₹{orb_high:.0f}",
            })
            zones.append({
                "source":    "ORB_LOW",
                "type":      "demand" if orb_low < current_price else "supply",
                "direction": "CE" if orb_low < current_price else "PE",
                "top":       round(orb_low + 10, 0),
                "bottom":    round(orb_low - 10, 0),
                "mid":       round(orb_low, 0),
                "strength":  3,
                "note":      f"Opening Range Low ₹{orb_low:.0f}",
            })

    # Sort by strength, remove zones price is currently inside
    zones = [z for z in zones if not (z["bottom"] <= current_price <= z["top"])]
    zones.sort(key=lambda z: z["strength"], reverse=True)
    return zones[:8]   # top 8 zones


def get_active_zone(price: float, zones: list, proximity_pts: float = 30.0) -> Optional[dict]:
    """
    Check if price is entering or inside any watch zone.
    Returns the highest-strength zone within proximity_pts of price, else None.
    """
    triggered = []
    for z in zones:
        # Price within proximity of zone mid, or inside the zone itself
        if abs(price - z["mid"]) <= proximity_pts or z["bottom"] <= price <= z["top"]:
            triggered.append(z)
    if not triggered:
        return None
    return max(triggered, key=lambda z: z["strength"])


def zone_signal(price: float, zones: list, proximity_pts: float = 25.0) -> Optional[str]:
    """
    Returns 'BUY' (enter CE) or 'SELL' (enter PE) if price is at a zone,
    else None.
    """
    zone = get_active_zone(price, zones, proximity_pts)
    if not zone:
        return None
    return "BUY" if zone["direction"] == "CE" else "SELL"


def today_zones_summary(zones: list) -> str:
    """One-line summary for logs/dashboard."""
    if not zones:
        return "No watch zones computed"
    lines = [f"  {z['source']:12} {z['direction']} zone ₹{z['bottom']:.0f}–₹{z['top']:.0f}  [{z['note']}]"
             for z in zones[:5]]
    return "Today's watch zones:\n" + "\n".join(lines)
