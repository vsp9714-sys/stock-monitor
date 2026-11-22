"""
Chart generator — creates candlestick charts with indicator overlays.

Generates publication-quality charts using mplfinance:
  - Candlestick OHLCV (dark theme)
  - EMA lines (9, 21, 50, 200 where applicable)
  - RSI subplot
  - MACD subplot
  - Volume bars with spike highlighting
  - Signal annotation (entry arrow + label)

Charts are saved as PNG to the charts/ directory and attached to alerts.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for server use

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import mplfinance as mpf
import numpy as np
import pandas as pd

from src.signals.ema_cross import compute_ema
from src.signals.macd import compute_macd
from src.signals.rsi import compute_rsi

logger = logging.getLogger(__name__)

CHARTS_DIR = Path("charts/output")
CHARTS_DIR.mkdir(parents=True, exist_ok=True)

# Dark theme colors
COLORS = {
    "bg": "#0d1117",
    "fg": "#e6edf3",
    "grid": "#21262d",
    "up": "#3fb950",
    "down": "#f85149",
    "ema9": "#58a6ff",
    "ema21": "#ffa657",
    "ema50": "#d2a8ff",
    "ema200": "#ff7b72",
    "volume_normal": "#388bfd50",
    "volume_spike": "#ffa65780",
    "rsi_line": "#58a6ff",
    "rsi_zone": "#21262d",
    "macd_line": "#58a6ff",
    "signal_line": "#ffa657",
    "macd_bull": "#3fb950",
    "macd_bear": "#f85149",
    "buy_arrow": "#3fb950",
    "sell_arrow": "#f85149",
}

STYLE = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=mpf.make_marketcolors(
        up=COLORS["up"],
        down=COLORS["down"],
        edge={"up": COLORS["up"], "down": COLORS["down"]},
        wick={"up": COLORS["up"], "down": COLORS["down"]},
        volume={"up": COLORS["up"], "down": COLORS["down"]},
    ),
    facecolor=COLORS["bg"],
    figcolor=COLORS["bg"],
    gridcolor=COLORS["grid"],
    gridstyle="--",
    gridaxis="both",
    rc={
        "axes.labelcolor": COLORS["fg"],
        "axes.edgecolor": COLORS["grid"],
        "xtick.color": COLORS["fg"],
        "ytick.color": COLORS["fg"],
        "text.color": COLORS["fg"],
        "font.family": "DejaVu Sans",
    },
)


def generate_signal_chart(
    symbol: str,
    company_name: str,
    df: pd.DataFrame,
    signal_type: str,
    direction: str,
    indicator_value: Optional[float] = None,
    lookback_candles: int = 60,
) -> Optional[str]:
    """
    Generate a candlestick chart with indicator overlays for a signal.

    Args:
        symbol: Stock symbol
        company_name: Full company name
        df: OHLCV DataFrame with 'ts','open','high','low','close','volume'
        signal_type: Signal identifier (e.g., "rsi_oversold")
        direction: "BUY" or "SELL"
        indicator_value: Primary indicator value (e.g., RSI=28.5)
        lookback_candles: How many candles to display on chart

    Returns:
        Path to saved chart PNG, or None if generation failed.
    """
    try:
        full_df = df.copy().reset_index(drop=True)
        if len(full_df) < 10:
            return None

        closes = full_df["close"].astype(float)

        # Compute EMAs on full history before slicing
        ema9 = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        ema50 = compute_ema(closes, 50) if len(full_df) >= 60 else None
        ema200 = compute_ema(closes, 200) if len(full_df) >= 210 else None

        # Compute RSI on full history before slicing
        rsi_series = compute_rsi(closes, 14) if len(full_df) >= 24 else None

        # Compute MACD on full history before slicing
        macd_line, sig_line, histogram = compute_macd(closes) if len(full_df) >= 40 else (None, None, None)

        # Slice to lookback_candles for display
        display_df = full_df.tail(lookback_candles).reset_index(drop=True)
        if len(display_df) < 10:
            return None

        # Prepare mplfinance-compatible DataFrame
        plot_df = display_df.set_index("ts")[["open", "high", "low", "close", "volume"]]
        plot_df.index = pd.DatetimeIndex(plot_df.index)
        plot_df.columns = ["Open", "High", "Low", "Close", "Volume"]

        n = len(plot_df)
        ema9 = ema9.tail(n)
        ema21 = ema21.tail(n)
        if ema50 is not None:
            ema50 = ema50.tail(n)
        if ema200 is not None:
            ema200 = ema200.tail(n)
        if rsi_series is not None:
            rsi_series = rsi_series.tail(n)
        if macd_line is not None:
            macd_line = macd_line.tail(n)
            sig_line = sig_line.tail(n)
            histogram = histogram.tail(n)

        # ── Build addplot list ────────────────────────────────────────────
        addplots = []
        ema_df = plot_df.copy()
        ema_df["ema9"] = ema9.values
        ema_df["ema21"] = ema21.values
        if ema50 is not None:
            ema_df["ema50"] = ema50.values
        if ema200 is not None:
            ema_df["ema200"] = ema200.values

        addplots.append(mpf.make_addplot(ema_df["ema9"], color=COLORS["ema9"], width=1.2, label="EMA 9"))
        addplots.append(mpf.make_addplot(ema_df["ema21"], color=COLORS["ema21"], width=1.2, label="EMA 21"))
        if ema50 is not None:
            addplots.append(mpf.make_addplot(ema_df["ema50"], color=COLORS["ema50"], width=1.5, label="EMA 50"))
        if ema200 is not None:
            addplots.append(mpf.make_addplot(ema_df["ema200"], color=COLORS["ema200"], width=2.0, label="EMA 200"))

        panel_count = 1  # Main chart

        # RSI panel
        if rsi_series is not None:
            rsi_df = pd.Series(rsi_series.values, index=plot_df.index)
            addplots.append(mpf.make_addplot(
                rsi_df, panel=1, color=COLORS["rsi_line"], width=1.5,
                ylabel="RSI", ylim=(0, 100)
            ))
            # Overbought/Oversold lines
            ob = pd.Series([70] * len(rsi_df), index=plot_df.index)
            os_ = pd.Series([30] * len(rsi_df), index=plot_df.index)
            addplots.append(mpf.make_addplot(ob, panel=1, color="#f8514950", linestyle="--", width=0.8))
            addplots.append(mpf.make_addplot(os_, panel=1, color="#3fb95050", linestyle="--", width=0.8))
            panel_count += 1

        # MACD panel
        if macd_line is not None:
            macd_s = pd.Series(macd_line.values, index=plot_df.index)
            sig_s = pd.Series(sig_line.values, index=plot_df.index)
            hist_s = pd.Series(histogram.values, index=plot_df.index)
            hist_colors = [COLORS["macd_bull"] if v >= 0 else COLORS["macd_bear"] for v in hist_s]

            addplots.append(mpf.make_addplot(macd_s, panel=panel_count, color=COLORS["macd_line"], width=1.2, ylabel="MACD"))
            addplots.append(mpf.make_addplot(sig_s, panel=panel_count, color=COLORS["signal_line"], width=1.2))
            addplots.append(mpf.make_addplot(hist_s, panel=panel_count, type="bar", color=hist_colors, alpha=0.6))

        # ── Figure sizing ─────────────────────────────────────────────────
        num_panels = 1 + (1 if rsi_series is not None else 0) + (1 if macd_line is not None else 0)
        panel_ratios = [4] + [1.5] * (num_panels - 1)

        title_dir_color = COLORS["buy_arrow"] if direction == "BUY" else COLORS["sell_arrow"]
        signal_label = signal_type.upper().replace("_", " ")

        fig, axes = mpf.plot(
            plot_df,
            type="candle",
            style=STYLE,
            addplot=addplots,
            volume=True,
            panel_ratios=panel_ratios,
            figsize=(14, 10),
            returnfig=True,
            tight_layout=True,
        )

        # Title
        fig.suptitle(
            f"[{direction}] {symbol} — {company_name}\n"
            f"Signal: {signal_label}"
            + (f" ({indicator_value:.2f})" if indicator_value else ""),
            color=COLORS["fg"],
            fontsize=13,
            fontweight="bold",
            y=0.98,
        )

        # Legend on main axis
        legend_patches = [
            mpatches.Patch(color=COLORS["ema9"], label="EMA 9"),
            mpatches.Patch(color=COLORS["ema21"], label="EMA 21"),
        ]
        if ema50 is not None:
            legend_patches.append(mpatches.Patch(color=COLORS["ema50"], label="EMA 50"))
        if ema200 is not None:
            legend_patches.append(mpatches.Patch(color=COLORS["ema200"], label="EMA 200"))
        axes[0].legend(handles=legend_patches, loc="upper left", fontsize=8, framealpha=0.4)

        # Save
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = CHARTS_DIR / f"{symbol}_{signal_type}_{timestamp}.png"
        fig.savefig(fname, dpi=120, bbox_inches="tight", facecolor=COLORS["bg"])
        plt.close(fig)

        logger.info(f"Chart saved: {fname}")
        return str(fname)

    except Exception as e:
        logger.error(f"Chart generation failed for {symbol}: {e}", exc_info=True)
        return None
