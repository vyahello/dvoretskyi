"""Chart rendering for the stats surface — tables, trends, stacks, small multiples.

Every renderer here is **synchronous and CPU-bound** (matplotlib). Callers must run
them off the event loop (`asyncio.to_thread`) — a 2-core VPS shared with other bots
takes ~300-600ms per render, which would otherwise freeze every other Telegram turn.

Two deliberate choices:

* **The object-oriented API (`Figure`), never `pyplot`.** pyplot keeps a global figure
  manager that is not thread-safe; with renders on worker threads it would corrupt
  state or leak figures. `Figure` + `FigureCanvasAgg` owns nothing global.
* **Colour follows the entity, never its rank.** `provider_color()` assigns a slot from
  the provider's stable display order (gas, water, electricity, housing, internet,
  mobile), so re-sorting a table or filtering a household never repaints a service.
  The palette order was validated for colour-vision deficiency separation (worst
  adjacent ΔE 9.2 protan/deutan, 24.0 normal-vision) — the previous rank-cycled
  palette put #f4a261 next to #8ab17d, a pair 3.7 apart under protanopia, i.e.
  indistinguishable. Do not reorder these without re-validating.
"""

from __future__ import annotations

import tempfile
from decimal import ROUND_HALF_UP, Decimal

# --- palette -----------------------------------------------------------------
# Categorical slots in stable display order. Validated (light surface #ffffff):
# lightness band PASS · chroma floor PASS · CVD separation PASS · normal-vision PASS.
# The sub-3:1 contrast slots (yellow, magenta, aqua) are relieved by the visible text
# labels / legend every chart here carries — never colour alone.
_SLOTS = (
    "#eb6834",  # orange  — gas (постачання)
    "#1baf7a",  # aqua    — gas (доставлення)
    "#2a78d6",  # blue    — water
    "#eda100",  # yellow  — electricity
    "#008300",  # green   — housing
    "#4a3aa7",  # violet  — internet
    "#e87ba4",  # magenta — mobile
)
_OTHER = "#898781"  # "Інші" fold — a neutral, never a generated 8th hue
SLOT_COUNT = len(_SLOTS)  # callers fold the tail past this into the neutral

# One hue for magnitude (the table's share bars encode size, not identity).
_ACCENT = "#2a78d6"

# Ink + chrome tokens (light surface — the PNG carries its own background, so the
# viewer's Telegram theme never applies and a single committed look is correct).
_SURFACE = "#ffffff"
_INK = "#0b0b0b"  # primary text
_SECONDARY = "#52514e"  # secondary text
_MUTED = "#898781"  # axis / column headers
_GRID = "#e1e0d9"  # hairline gridline
_BASELINE = "#c3c2b7"  # axis line
_STRIPE = "#f7f7f5"  # zebra row background
_TRACK = "#e4e7eb"  # empty share-bar track
_GOOD = "#006300"  # delta ↓ (spent less)
_BAD = "#d03b3b"  # delta ↑ (spent more)

_FONT = "DejaVu Sans"  # matplotlib's bundled default — covers Cyrillic


def provider_color(slot: int) -> str:
    """Colour for the `slot`-th provider in stable display order. Past the palette we
    fold to a neutral rather than generate a hue no CVD reader could separate."""
    return _SLOTS[slot] if 0 <= slot < len(_SLOTS) else _OTHER


def _new_figure(width: float, height: float):
    """A canvas-backed Figure — no pyplot, no global state, safe on a worker thread."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(width, height), dpi=150, facecolor=_SURFACE)
    FigureCanvasAgg(fig)
    return fig


def _save(fig) -> str:
    tmp = tempfile.NamedTemporaryFile(
        prefix="dvoretskyi_stats_", suffix=".png", delete=False
    )
    fig.savefig(tmp.name, facecolor=_SURFACE)
    tmp.close()
    return tmp.name


def fmt_uah(amount: Decimal) -> str:
    """'2391.39' → '2 391.39' (space-grouped thousands, always 2 decimals)."""
    cents = int(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole:,}".replace(",", " ") + f".{frac:02d}"


def _fit_text(fig, ax, text: str, fontsize: float, max_width: float) -> str:
    """Trim `text` until it renders no wider than `max_width` (in x-axis units).

    matplotlib does not clip text to a column, so an over-long label (a by-household row
    is labelled with an ADDRESS) draws straight through the share bar and the amount.
    Character counts can't decide this — Cyrillic, digits and spaces have very different
    widths in DejaVu Sans — so we MEASURE the rendered extent and trim to fit.
    """
    renderer = fig.canvas.get_renderer()
    ax_px = ax.get_window_extent(renderer=renderer).width
    if ax_px <= 0:
        return text
    limit_px = max_width * ax_px

    def width_px(s: str) -> float:
        probe = ax.text(0, 0, s, fontsize=fontsize, fontname=_FONT, alpha=0)
        w = probe.get_window_extent(renderer=renderer).width
        probe.remove()
        return w

    if width_px(text) <= limit_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # longest prefix that still fits, with the ellipsis included
        mid = (lo + hi + 1) // 2
        if width_px(text[:mid].rstrip() + "…") <= limit_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…" if lo else "…"


def _compact(amount: float) -> str:
    """Axis-tick money: 4812.5 → '4.8к', 480 → '480'. Keeps the y-axis narrow."""
    if abs(amount) >= 1000:
        return f"{amount / 1000:.1f}к".replace(".0к", "к")
    return f"{amount:.0f}"


def _style_axes(ax, *, ygrid: bool = True) -> None:
    """Recessive chrome: hairline y-grid, no box, muted ticks."""
    ax.set_facecolor(_SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(_BASELINE)
    ax.spines["bottom"].set_linewidth(1.0)
    if ygrid:
        ax.grid(axis="y", color=_GRID, linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)
    ax.tick_params(axis="both", colors=_MUTED, labelsize=9, length=0)


# --- the month table ---------------------------------------------------------


def render_table(
    rows: list[tuple[str, Decimal, float, int | None]],
    title: str,
    total: Decimal,
    delta_note: str | None = None,
) -> str:
    """The one-month breakdown as a clean data table (PNG).

    `rows`: (label, amount, share, slot) sorted biggest-first — `slot` is the entity's
    stable display index, so its colour chip survives re-sorting. `slot=None` draws no
    chip: a by-month or by-household row has no provider identity for a colour to carry,
    and a chip there would imply one. `delta_note` is the month-over-month line
    ("▲ +8% до травня") shown under the grand total; None when there's no previous
    month to compare against.

    The share bar encodes MAGNITUDE, so it takes one hue — a categorical colour here
    would imply identity the bar doesn't carry. The per-row colour chip carries
    identity instead, and matches the same provider's colour in every trend chart.
    """
    n = len(rows)
    title_h, header_h, row_h, pad = 1.6, 0.8, 1.0, 0.4
    # `pad` twice: once above the title, once BELOW the last row. With a single pad the
    # bottom row's zebra stripe ran flush into the image edge and read as clipped.
    units = title_h + header_h + n * row_h + pad * 2
    fig = _new_figure(8.6, 0.52 * units + 0.4)
    ax = fig.add_subplot(111)
    # Set the final geometry BEFORE anything is drawn: `_fit_text` measures the axes to
    # decide where to trim, so adjusting afterwards made it measure matplotlib's default
    # margins (0.775 of the figure) instead of the real 0.98 — trimming every label ~21%
    # shorter than it needed to be.
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, units)
    ax.axis("off")

    x_chip = 0.028
    x_name = 0.058
    x_bar0, x_bar1 = 0.45, 0.70
    x_amount = 0.875
    x_share = 0.965

    top = units - pad
    ax.text(
        x_chip,
        top - 0.55,
        "Комуналка",
        fontsize=20,
        fontweight="bold",
        color=_INK,
        fontname=_FONT,
    )
    ax.text(x_chip, top - 1.12, title, fontsize=13, color=_MUTED, fontname=_FONT)
    ax.text(
        x_share,
        top - 0.62,
        f"{fmt_uah(total)} ₴",
        fontsize=20,
        fontweight="bold",
        color=_INK,
        ha="right",
        fontname=_FONT,
    )
    if delta_note:
        colour = _BAD if delta_note.startswith("▲") else _GOOD
        ax.text(
            x_share,
            top - 1.16,
            delta_note,
            fontsize=11.5,
            color=colour,
            ha="right",
            fontname=_FONT,
        )
    else:
        ax.text(
            x_share,
            top - 1.16,
            "разом",
            fontsize=12,
            color=_MUTED,
            ha="right",
            fontname=_FONT,
        )

    hy = top - title_h - header_h / 2
    ax.text(
        x_name, hy, "ПОСЛУГА", fontsize=10.5, color=_MUTED, va="center", fontname=_FONT
    )
    ax.text(
        x_amount,
        hy,
        "СУМА",
        fontsize=10.5,
        color=_MUTED,
        va="center",
        ha="right",
        fontname=_FONT,
    )
    ax.text(
        x_share,
        hy,
        "ЧАСТКА",
        fontsize=10.5,
        color=_MUTED,
        va="center",
        ha="right",
        fontname=_FONT,
    )

    from matplotlib.patches import Rectangle

    max_share = max((s for _, _, s, _ in rows), default=0.0) or 1.0
    body_top = top - title_h - header_h
    for i, (label, amount, share, slot) in enumerate(rows):
        ry = body_top - i * row_h
        cy = ry - row_h / 2
        if i % 2 == 0:
            ax.add_patch(
                Rectangle(
                    (0.02, ry - row_h),
                    0.96,
                    row_h,
                    facecolor=_STRIPE,
                    edgecolor="none",
                    zorder=0,
                )
            )
        # Identity chip — the same colour this provider wears in every trend chart.
        # No slot (a by-month/by-household row) → no chip; there is no identity here.
        if slot is not None:
            ax.add_patch(
                Rectangle(
                    (x_chip - 0.012, cy - 0.16),
                    0.008,
                    0.32,
                    facecolor=provider_color(slot),
                    edgecolor="none",
                    zorder=2,
                )
            )
        # Trim to the column so a long label can never overdraw the bar/amount. The gap
        # keeps it from touching the bar.
        fitted = _fit_text(fig, ax, label, 13.5, (x_bar0 - x_name) - 0.02)
        ax.text(
            x_name,
            cy,
            fitted,
            fontsize=13.5,
            color=_INK,
            va="center",
            zorder=2,
            fontname=_FONT,
        )
        bar_h = row_h * 0.24
        ax.add_patch(
            Rectangle(
                (x_bar0, cy - bar_h / 2),
                x_bar1 - x_bar0,
                bar_h,
                facecolor=_TRACK,
                edgecolor="none",
                zorder=1,
            )
        )
        fill_w = (x_bar1 - x_bar0) * (share / max_share)
        if fill_w > 0.004:
            ax.add_patch(
                Rectangle(
                    (x_bar0, cy - bar_h / 2),
                    fill_w,
                    bar_h,
                    facecolor=_ACCENT,
                    edgecolor="none",
                    zorder=2,
                )
            )
        ax.text(
            x_amount,
            cy,
            fmt_uah(amount),
            fontsize=13.5,
            fontweight="bold",
            color=_INK,
            va="center",
            ha="right",
            zorder=2,
            fontname=_FONT,
        )
        ax.text(
            x_share,
            cy,
            f"{share:.0%}",
            fontsize=12.5,
            color=_MUTED,
            va="center",
            ha="right",
            zorder=2,
            fontname=_FONT,
        )

    return _save(fig)  # geometry was set before drawing — see the note above


# --- money trend (one series over months) ------------------------------------


def render_trend(
    points: list[tuple[str, Decimal]],
    title: str,
    *,
    unit: str = "₴",
) -> str:
    """Money (or any single measure) per month — columns + an average reference line.

    One series → one hue, no legend (the title names it). Labels are SELECTIVE: the
    peak and the latest month only. A number over every column would be noise, and the
    exact figures live in the table view a tap away.
    """
    labels = [lbl for lbl, _ in points]
    values = [float(v) for _, v in points]
    n = len(values)

    fig = _new_figure(max(7.0, min(10.0, 1.0 + 0.62 * n)), 4.2)
    ax = fig.add_subplot(111)
    _style_axes(ax)

    ax.bar(range(n), values, width=0.62, color=_ACCENT, edgecolor="none", zorder=2)

    peak = max(values) if values else 0.0
    avg = (sum(values) / n) if n else 0.0
    if avg > 0 and n > 1:
        ax.axhline(avg, color=_SECONDARY, linewidth=1.2, linestyle="--", zorder=3)
        # Label INSIDE the axes, above the line at the left. Anchoring it past the last
        # bar put it in the margin, where tight_layout is free to clip it.
        ax.text(
            -0.4,
            avg,
            f"сер. {_compact(avg)}",
            fontsize=9,
            color=_SECONDARY,
            va="bottom",
            ha="left",
            fontname=_FONT,
            zorder=4,
        )

    # Selective direct labels: the peak and the newest month carry the story.
    mark = set()
    if values:
        mark.add(int(max(range(n), key=lambda i: values[i])))
        mark.add(n - 1)
    for i in mark:
        if values[i] <= 0:
            continue
        ax.text(
            i,
            values[i] + peak * 0.03,
            _compact(values[i]),
            fontsize=10,
            fontweight="bold",
            color=_INK,
            ha="center",
            va="bottom",
            fontname=_FONT,
        )

    ax.set_xticks(range(n))
    ax.set_xticklabels(labels, fontsize=9, color=_MUTED, fontname=_FONT)
    ax.set_ylim(0, max(peak * 1.18, 1))
    ax.yaxis.set_major_formatter(lambda v, _: _compact(v))
    # The unit belongs in the title, not floating beside the axis where it collided with
    # it. One line names the series, so no legend is needed for a single series.
    ax.set_title(
        f"{title}, {unit}" if unit else title,
        fontsize=13,
        color=_INK,
        loc="left",
        pad=14,
        fontname=_FONT,
    )
    fig.tight_layout()
    return _save(fig)


# --- per-provider stack ------------------------------------------------------


def render_stacked(
    labels: list[str],
    series: list[tuple[str, list[Decimal], int]],
    title: str,
) -> str:
    """Composition per month — a stacked column per month, one segment per provider.

    `series`: (name, values-per-month, slot). A legend is always present (≥2 series →
    identity is never colour-alone), and segments are separated by a 2px surface gap so
    adjacent fills stay countable.
    """
    n = len(labels)
    fig = _new_figure(max(7.0, min(10.0, 1.4 + 0.68 * n)), 4.8)
    ax = fig.add_subplot(111)
    _style_axes(ax)

    bottoms = [0.0] * n
    for name, values, slot in series:
        vals = [float(v) for v in values]
        ax.bar(
            range(n),
            vals,
            bottom=bottoms,
            width=0.62,
            label=name,
            color=provider_color(slot),
            edgecolor=_SURFACE,
            linewidth=1.4,
            zorder=2,
        )
        bottoms = [b + v for b, v in zip(bottoms, vals, strict=False)]

    totals = bottoms
    peak = max(totals) if totals else 0.0
    for i, tot in enumerate(totals):
        if tot > 0:
            ax.text(
                i,
                tot + peak * 0.03,
                _compact(tot),
                fontsize=9.5,
                fontweight="bold",
                color=_INK,
                ha="center",
                va="bottom",
                fontname=_FONT,
            )

    ax.set_xticks(range(n))
    ax.set_xticklabels(labels, fontsize=9, color=_MUTED, fontname=_FONT)
    ax.set_ylim(0, max(peak * 1.2, 1))
    ax.yaxis.set_major_formatter(lambda v, _: _compact(v))
    ax.set_title(title, fontsize=13, color=_INK, loc="left", pad=14, fontname=_FONT)
    legend = ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.10),
        ncol=min(3, len(series)),
        frameon=False,
        fontsize=9.5,
        handlelength=1.1,
        handleheight=1.1,
        columnspacing=1.4,
    )
    for text in legend.get_texts():
        text.set_color(_SECONDARY)
        text.set_fontname(_FONT)
    fig.tight_layout()
    return _save(fig)


# --- consumption small multiples ---------------------------------------------


def render_volume(
    panels: list[tuple[str, list[str], list[Decimal], int]],
    title: str,
) -> str:
    """Consumption per meter — SMALL MULTIPLES, one panel per meter, each with its own
    y-scale.

    `panels`: (meter name, month labels, values, slot). Gas runs ~40 m³/month and water
    ~3 m³/month; sharing one axis would flatten water into the baseline, and a second
    y-axis is never the answer — so each meter gets its own panel and its own scale.
    """
    rows = len(panels)
    fig = _new_figure(8.0, 1.0 + 2.3 * rows)
    for idx, (name, labels, values, slot) in enumerate(panels):
        ax = fig.add_subplot(rows, 1, idx + 1)
        _style_axes(ax)
        vals = [float(v) for v in values]
        colour = provider_color(slot)
        ax.bar(
            range(len(vals)), vals, width=0.6, color=colour, edgecolor="none", zorder=2
        )
        peak = max(vals) if vals else 0.0
        for i, v in enumerate(vals):
            if v > 0:
                ax.text(
                    i,
                    v + peak * 0.04,
                    f"{v:g}",
                    fontsize=9,
                    color=_INK,
                    ha="center",
                    va="bottom",
                    fontname=_FONT,
                )
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=9, color=_MUTED, fontname=_FONT)
        ax.set_ylim(0, max(peak * 1.25, 1))
        ax.set_title(
            f"{name}, м³", fontsize=11.5, color=_INK, loc="left", pad=8, fontname=_FONT
        )
    fig.suptitle(title, fontsize=13, color=_INK, x=0.02, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return _save(fig)
