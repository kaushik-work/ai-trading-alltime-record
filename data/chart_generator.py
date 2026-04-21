"""
chart_generator.py — generates NIFTY candlestick chart images from live Angel One data.

Returns PNG bytes in memory (no disk I/O) ready to send to Claude Vision API.
"""

import io
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def generate_chart(symbol: str = "NIFTY", interval: str = "5m", bars: int = 100) -> Optional[bytes]:
    """
    Fetch live bars from Angel One and render a candlestick chart.
    Returns PNG bytes or None on failure.

    interval: "5m" or "15m"
    bars: number of recent bars to show
    """
    try:
        import pandas as pd
        import mplfinance as mpf
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend

        from data.angel_fetcher import AngelFetcher
        days = 3 if interval == "5m" else 5
        df = AngelFetcher.get().fetch_historical_df(symbol, interval, days=days)

        if df is None or len(df) < 20:
            logger.warning("chart_generator: insufficient data for %s %s", symbol, interval)
            return None

        # Take most recent `bars` candles
        df = df.tail(bars).copy()
        df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
        df.index = pd.to_datetime(df.index)
        df.index.name = "Date"

        # Add session open and previous day high/low reference lines
        addplots = _build_reference_lines(df)

        # TradingView-style dark theme
        style = mpf.make_mpf_style(
            base_mpf_style="nightclouds",
            marketcolors=mpf.make_marketcolors(
                up="#26a69a", down="#ef5350",
                edge="inherit", wick="inherit",
                volume={"up": "#26a69a55", "down": "#ef535055"},
            ),
            gridstyle="--", gridcolor="#2a2a3c",
            facecolor="#131722", figcolor="#131722",
            rc={"axes.labelcolor": "#d1d4dc", "xtick.color": "#d1d4dc", "ytick.color": "#d1d4dc"},
        )

        buf = io.BytesIO()
        fig, _ = mpf.plot(
            df,
            type="candle",
            style=style,
            volume=True,
            addplot=addplots or [],
            figsize=(14, 7),
            title=dict(
                title=f"\nNIFTY 50  ·  {interval.upper()}  ·  {len(df)} bars  ·  {datetime.now().strftime('%d %b %Y %H:%M')} IST",
                color="#d1d4dc", fontsize=11,
            ),
            tight_layout=True,
            returnfig=True,
            warn_too_much_data=9999,
        )
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                    facecolor="#131722", edgecolor="none")
        fig.clf()
        import matplotlib.pyplot as plt
        plt.close("all")
        buf.seek(0)
        png_bytes = buf.read()
        logger.info("chart_generator: generated %s %s chart (%d bytes, %d bars)",
                    symbol, interval, len(png_bytes), len(df))
        return png_bytes

    except Exception as e:
        logger.error("chart_generator.generate_chart %s %s: %s", symbol, interval, e)
        return None


def _build_reference_lines(df) -> list:
    """Add previous-day high/low and today's session open as horizontal lines."""
    try:
        import mplfinance as mpf
        import numpy as np

        lines = []
        today = df.index[-1].date()

        # Split today vs previous days
        today_mask = df.index.date == today
        prev_mask  = df.index.date < today

        if prev_mask.any() and today_mask.any():
            prev_df = df[prev_mask]
            pdh = float(prev_df["High"].max())
            pdl = float(prev_df["Low"].min())

            pdh_line = [pdh if today_mask[i] else float("nan") for i in range(len(df))]
            pdl_line = [pdl if today_mask[i] else float("nan") for i in range(len(df))]

            lines.append(mpf.make_addplot(pdh_line, color="#f0b429", linestyle="--",
                                           linewidth=0.8, secondary_y=False))
            lines.append(mpf.make_addplot(pdl_line, color="#f0b429", linestyle="--",
                                           linewidth=0.8, secondary_y=False))

        # Session open price (first bar of today)
        if today_mask.any():
            open_price = float(df[today_mask]["Open"].iloc[0])
            open_line  = [open_price if today_mask[i] else float("nan") for i in range(len(df))]
            lines.append(mpf.make_addplot(open_line, color="#7c83fd", linestyle=":",
                                           linewidth=0.8, secondary_y=False))

        return lines
    except Exception:
        return []
