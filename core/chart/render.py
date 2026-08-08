"""Draw the chart as it looked at a past moment, so a vision model can be tested.

THE PROBLEM THIS SOLVES

A bot that reads charts visually sounds untestable: there are no historical
screenshots, so there is nothing to replay against, and an untestable strategy
is one this repo has learned not to deploy.

But screenshots are not the only way to get a picture. A chart is a rendering
of OHLC, and we hold 115,200 bars of it per crypto symbol. Rendering bar i with
its preceding window produces exactly the image a trader would have been
looking at, and the outcome is already known from the data. So the vision lens
CAN be measured on the same footing as every numeric one — same splits, same
mix-matched baseline, same hold-out.

    render(df, i)  ->  PNG of the chart at bar i, NOTHING after it

THE ONE RULE

`df.iloc[:i+1]`, never `df`. A single future candle in frame and the model is
being asked to predict something it can see. This is the same failure as the
resample-label bug that turned -Rs 6,110 into +Rs 377,749, except the leak
would be visual and therefore much harder to notice in review.

The renderer draws only what a trader would have on screen: candles, the swing
levels, and optionally the proposed stop and target. No indicator computed from
future bars, no annotation of the outcome.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

# Bars visible in the frame. Enough context to see structure, few enough that
# individual candles stay legible after downscaling for the vision model.
DEFAULT_WINDOW = 120

# Rendered size. Larger costs more tokens and buys nothing once candles are
# already distinguishable.
FIG_W, FIG_H, DPI = 12.0, 7.0, 90


@dataclass
class RenderedChart:
    png: bytes
    idx: int
    n_bars: int
    first_ts: object
    last_ts: object
    last_close: float

    @property
    def b64(self) -> str:
        return base64.standard_b64encode(self.png).decode()

    def save(self, path) -> None:
        with open(path, "wb") as fh:
            fh.write(self.png)


def render(df: pd.DataFrame, idx: Optional[int] = None, *,
           window: int = DEFAULT_WINDOW,
           levels: Optional[Sequence[float]] = None,
           stop: Optional[float] = None,
           target: Optional[float] = None,
           title: str = "") -> Optional[RenderedChart]:
    """PNG of the chart as of bar `idx`, inclusive. None if there is too little.

    `idx` defaults to the last bar. Everything after it is DROPPED before
    drawing — the slice happens here rather than at the call site so a caller
    cannot forget.
    """
    import matplotlib
    matplotlib.use("Agg")                     # headless; no display required
    import matplotlib.pyplot as plt
    import mplfinance as mpf

    need = {"open", "high", "low", "close"}
    if df is None or not need <= set(df.columns):
        return None
    if idx is None:
        idx = len(df) - 1
    if idx < 20 or idx >= len(df):
        return None

    # THE cut. Nothing beyond the decision bar reaches the canvas.
    lo = max(0, idx + 1 - window)
    view = df.iloc[lo:idx + 1].copy()
    if len(view) < 20:
        return None

    if "datetime" in view.columns:
        view["datetime"] = pd.to_datetime(view["datetime"], utc=True, errors="coerce")
        view = view.dropna(subset=["datetime"]).set_index("datetime")
    view = view[["open", "high", "low", "close"] +
                (["volume"] if "volume" in view.columns else [])]
    view.columns = [c.capitalize() for c in view.columns]

    hlines = [x for x in (list(levels or [])) if x and x > 0]
    colors = ["#888888"] * len(hlines)
    if stop:
        hlines.append(stop); colors.append("#d03b3b")
    if target:
        hlines.append(target); colors.append("#0ca30c")

    style = mpf.make_mpf_style(base_mpf_style="charles", gridstyle=":",
                               facecolor="white", edgecolor="#333333")
    kwargs = dict(type="candle", style=style, figsize=(FIG_W, FIG_H),
                  volume="Volume" in view.columns, returnfig=True,
                  tight_layout=True, xrotation=0, warn_too_much_data=10_000)
    if hlines:
        kwargs["hlines"] = dict(hlines=hlines, colors=colors,
                                linestyle="--", linewidths=1.0)
    if title:
        kwargs["title"] = title

    buf = io.BytesIO()
    try:
        fig, _axes = mpf.plot(view, **kwargs)
        fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight")
        plt.close(fig)
    except Exception as e:
        logger.error("render failed at idx %d: %s", idx, e)
        try:
            plt.close("all")
        except Exception:
            pass
        return None

    return RenderedChart(
        png=buf.getvalue(), idx=idx, n_bars=len(view),
        first_ts=view.index[0] if len(view.index) else None,
        last_ts=view.index[-1] if len(view.index) else None,
        last_close=float(view["Close"].iloc[-1]),
    )


def render_setup(df: pd.DataFrame, setup, *, window: int = DEFAULT_WINDOW,
                 title: str = "") -> Optional[RenderedChart]:
    """Render the bar a Setup fired on, with its stop and target drawn.

    Note the deliberate omission: the entry marker is drawn but the OUTCOME is
    not. Showing where price went would turn the picture into the answer.
    """
    return render(df, getattr(setup, "sweep", None).idx if getattr(setup, "sweep", None)
                  else None,
                  window=window, stop=setup.stop, target=setup.target,
                  title=title or f"{'LONG' if setup.direction > 0 else 'SHORT'} "
                                 f"R:R {setup.rr:.2f}")
