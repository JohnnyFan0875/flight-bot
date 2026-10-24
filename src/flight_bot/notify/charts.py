"""每週摘要的熱力圖和趨勢圖。"""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

# Actions 裝的是 Noto Sans CJK；本機 Mac 退回 PingFang / Heiti
CJK_FONTS = ["Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans TC", "PingFang TC", "Heiti TC", "Arial Unicode MS"]


def _setup_font() -> None:
    installed = {f.name for f in font_manager.fontManager.ttflist}
    available = [f for f in CJK_FONTS if f in installed]
    plt.rcParams["font.sans-serif"] = available + plt.rcParams["font.sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def heatmap(rows: list[str], cols: list[str], values: list[list[int | None]], title: str) -> bytes:
    """values[i][j]：第 i 個目的地、第 j 個月份的最低價。顏色依同一列內的相對位置（綠 = 該目的地最便宜的月份）。"""
    _setup_font()
    fig, ax = plt.subplots(figsize=(max(6, 1.1 * len(cols) + 2), 0.7 * len(rows) + 1.5))
    shades = []
    for row in values:
        known = [v for v in row if v is not None]
        lo, hi = (min(known), max(known)) if known else (0, 0)
        shades.append([None if v is None else (0.5 if hi == lo else (v - lo) / (hi - lo)) for v in row])
    masked = [[float("nan") if s is None else s for s in row] for row in shades]
    ax.imshow(masked, cmap="RdYlGn_r", vmin=0, vmax=1, aspect="auto")
    for i, row in enumerate(values):
        for j, v in enumerate(row):
            s = shades[i][j]
            dark = s is not None and (s > 0.8 or s < 0.15)  # 深紅、深綠底色改用白字
            ax.text(j, i, "—" if v is None else f"{v:,}", ha="center", va="center", fontsize=9,
                    color="white" if dark else "black")
    ax.set_xticks(range(len(cols)), cols)
    ax.set_yticks(range(len(rows)), rows)
    ax.set_title(title)
    return _png(fig)


def trend(series: dict[str, list[tuple[str, int]]], title: str) -> bytes:
    """series：{目的地: [(週, 最低價), ...]}。"""
    _setup_font()
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for name, points in series.items():
        if points:
            ax.plot([p[0] for p in points], [p[1] for p in points], marker="o", label=name)
    ax.set_title(title)
    ax.set_ylabel("TWD")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.grid(alpha=0.3)
    ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate()
    return _png(fig)
