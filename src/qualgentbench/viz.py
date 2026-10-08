"""Inline-SVG chart primitives for the static view (QUA-2916).

Deterministic string builders: the same input gives the same bytes, so a page built
from them stays byte-stable across `view --index-from` rebuilds. Stdlib only, no
script, no CDN, and no `xmlns` attribute (inline SVG in HTML5 does not need one, and
the view's pages must not contain `http`).

Statistics come from journey.summary rows, never recomputed here. A rate is read as
`{prefix}_rate`, `{prefix}_ci`, `{prefix}_k`, `{prefix}_n` (the `rates.Rate.as_fields`
shape) and drawn as it is; a None rate draws "n/a", never 0%.

Colour only through CSS classes bound to the tokens in `view.CSS`: `--s1` for every
rate mark (public filled, held-out hollow), `--s2` for arm B only, `--st-*` for status.
The CVD rule: status marks never carry meaning by colour alone. Every status cell
carries a glyph and a `<title>`, and the strip legend names each glyph. Every rate mark
carries a `<title>` with `k/n p% [lo-hi]`, and the caller renders the table twin.

Public API (all return str):

  esc(s)                                   escaped text or attribute value
  svg(w, h, body, label=None)              the root element, class "chart"
  text(x, y, s, cls=None, anchor=None)     one escaped `<text>`
  pct_axis(x0, w, y, top=None)             0-100% ticks under a panel, gridlines from top
  dots_ci(rows, panels, href=None, ...)    rates with Wilson whiskers, one panel per metric
  strip(cells, lanes, href_fn, ...)        one 10x10 status cell per episode
  drift(rows_recorded, rows_now, metrics)  recorded (hollow) -> now (filled) per metric
  forest(groups, series_labels)            arm A and arm B per row, grouped, pooled rows
  bars(rows, prefix, title)                one series of 0-100% bars, k/n at each tip
  row_label(row)                           the default row label (model · agent · condition)
  STATUS                                   the strip's status vocabulary: class, glyph, label

Layout is fixed-width (`width`/`height` attributes plus a viewBox; the stylesheet caps
it at the container width). Callers wrap a chart in `.tablewrap` when it may be wider
than a phone.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .rates import fmt_pct_ci

Row = Mapping[str, Any]

#: status -> (css class, glyph, legend label). Lanes: seeded episodes use the first
#: six, clean episodes `clean` / `false_report`. `excluded` is hatched, not coloured.
STATUS: dict[str, tuple[str, str, str]] = {
    "caught": ("st-good", "✓", "caught"),
    "artifact": ("st-serious", "✗", "missed, unmatched grounded report (artifact?)"),
    "silent": ("st-crit", "✗", "missed, no report"),
    "unreached": ("st-warn", "○", "never reached"),
    "truncated": ("st-warn", "✂", "truncated"),
    "excluded": ("st-ex", "⊘", "excluded"),
    "clean": ("st-good", "✓", "no false report"),
    "false_report": ("st-crit", "!", "false report"),
}

ROW_H = 20
CELL = 10
CELL_GAP = 2
GROUP_GAP = 8
CHAR_W = 6.2      # average glyph width at 11px, for label fitting and legend layout


def esc(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def _n(x: float) -> str:
    """A coordinate as text: one decimal, no trailing `.0`, no `-0`."""
    s = f"{round(float(x), 1):.1f}".removesuffix(".0")
    return "0" if s == "-0" else s


def svg(w: float, h: float, body: str, label: str | None = None) -> str:
    aria = f' role="img" aria-label="{esc(label)}"' if label else ""
    return (f'<svg class="chart" viewBox="0 0 {_n(w)} {_n(h)}" width="{_n(w)}" '
            f'height="{_n(h)}"{aria}>{body}</svg>')


def text(x: float, y: float, s: Any, cls: str | None = None, anchor: str | None = None) -> str:
    c = f' class="{cls}"' if cls else ""
    a = f' text-anchor="{anchor}"' if anchor else ""
    return f'<text x="{_n(x)}" y="{_n(y)}"{c}{a}>{esc(s)}</text>'


def _line(x1: float, y1: float, x2: float, y2: float, cls: str) -> str:
    return f'<line class="{cls}" x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}"/>'


def _fit(s: str, width: float) -> str:
    """Shorten a label to fit `width` px; the full text stays in the mark titles."""
    cap = max(4, int(width / CHAR_W))
    return s if len(s) <= cap else s[:cap - 1] + "…"


def pct_axis(x0: float, w: float, y: float, top: float | None = None,
             ticks: Sequence[int] = (0, 25, 50, 75, 100)) -> str:
    """Tick labels for a 0-100% panel spanning `x0..x0+w`; hairline gridlines from `top`."""
    out = [_line(x0, y, x0 + w, y, "axis")]
    for t in ticks:
        x = x0 + w * t / 100
        if top is not None:
            out.append(_line(x, top, x, y, "grid"))
        out.append(text(x, y + 13, f"{t}%", "tick", "middle"))
    return "".join(out)


def _legend(items: Sequence[tuple[str, str]], x: float, y: float) -> tuple[str, float]:
    """`(key class, label)` pairs as circle keys on one line; returns (svg, width)."""
    out, cx = [], x
    for cls, label in items:
        out.append(f'<circle class="key {cls}" cx="{_n(cx + 5)}" cy="{_n(y - 4)}" r="4"/>')
        out.append(text(cx + 13, y, label))
        cx += 13 + len(label) * CHAR_W + 14
    return f'<g class="legend">{"".join(out)}</g>', cx - x


def _rate(row: Row, prefix: str) -> tuple[float | None, Sequence[float] | None, int, int]:
    return (row.get(f"{prefix}_rate"), row.get(f"{prefix}_ci"),
            int(row.get(f"{prefix}_k") or 0), int(row.get(f"{prefix}_n") or 0))


def _title(row: Row, prefix: str, lead: str = "") -> str:
    p, ci, k, n = _rate(row, prefix)
    return f"<title>{esc(lead + fmt_pct_ci(p, ci, k, n))}</title>"


def row_label(row: Row) -> str:
    """Default row label: `label` if the caller set one, else model · agent · condition
    (· app for a `journey.summary(by_app=True)` row)."""
    if row.get("label"):
        return str(row["label"])
    parts = [row.get("model"), row.get("agent"), row.get("condition"), row.get("app")]
    return " · ".join(str(p) for p in parts if p)


def _mark(x: float, y: float, cls: str, ci: Sequence[float] | None, sx: Callable,
          title: str, dy: float = 0) -> str:
    whisker = _line(sx(ci[0]), y + dy, sx(ci[1]), y + dy, "whisker") if ci else ""
    return (f'<g class="{cls}">{title}{whisker}'
            f'<circle class="hit" cx="{_n(x)}" cy="{_n(y)}" r="12"/>'
            f'<circle class="dot" cx="{_n(x)}" cy="{_n(y)}" r="5"/></g>')


def _panels(rows: list[Row], panels: Sequence[tuple[str, str]], legend: list[tuple[str, str]],
            draw: Callable[[Row, str, float, Callable], str], label: Callable[[Row], str],
            held_key: str | None, href: Callable[[Row], str | None] | None,
            label_w: float, panel_w: float, note: Callable[[], str | None],
            aria: str) -> str:
    """The shared frame: label column + one 0-100% panel per metric, rows sharing the
    row axis; public rows first, then a "held-out" block."""
    gap = 24
    w = label_w + len(panels) * (panel_w + gap) - gap + 16     # room for the "100%" tick
    out, y = [], 6.0
    if legend:
        g, lw = _legend(legend, label_w, y + 10)
        out.append(g)
        w, y = max(w, label_w + lw), y + 20
    for j, (_, title) in enumerate(panels):
        out.append(text(label_w + j * (panel_w + gap), y + 10, title, "ptitle"))
    y += 18
    top = y
    public = [r for r in rows if not (held_key and r.get(held_key))]
    held = [r for r in rows if held_key and r.get(held_key)]
    body = []
    for block, block_rows in (("", public), ("held-out", held)):
        if not block_rows:
            continue
        if block:
            body.append(text(0, y + 13, block, "dim"))
            y += 18
        for r in block_rows:
            cy = y + ROW_H / 2
            marks = [text(0, cy + 4, _fit(label(r), label_w - 8))]
            for j, (prefix, _) in enumerate(panels):
                x0 = label_w + j * (panel_w + gap) + 6
                pw = panel_w - 12

                def sx(p: float, x0: float = x0, pw: float = pw) -> float:
                    return x0 + pw * max(0.0, min(1.0, float(p)))
                marks.append(draw(r, prefix, cy, sx))
            g = f'<g class="row">{"".join(marks)}</g>'
            link = href(r) if href else None
            body.append(f'<a href="{esc(link)}">{g}</a>' if link else g)
            y += ROW_H
    y += 4
    for j in range(len(panels)):
        out.append(pct_axis(label_w + j * (panel_w + gap) + 6, panel_w - 12, y, top))
    out.extend(body)
    y += 22
    msg = note()
    if msg:
        out.append(text(0, y + 6, msg, "dim"))
        y += 16
    return svg(w, y, "".join(out), aria)


def dots_ci(rows: Iterable[Row], panels: Sequence[tuple[str, str]],
            href: Callable[[Row], str | None] | None = None, held_key: str = "heldout",
            dim_below_n: int = 5, label: Callable[[Row], str] = row_label,
            series_labels: Mapping[str, str] | None = None,
            label_w: float = 220, panel_w: float = 200) -> str:
    """Rates with intervals: one dot-with-whisker per row per panel, panels side by
    side on a shared row axis, 0-100% each (e.g. `[("false_alarm", "false alarm / clean
    case"), ("catch", "catch / seeded defect")]`).

    Rows draw in the order given, public first, then held-out (`row[held_key]`) with
    hollow marks. A mark whose `{prefix}_n` is below `dim_below_n`, or any mark of a row
    with `row["pending"]` (a row still owed results), is faded (`lown`) and a note says
    so. A None rate draws `row["na_label"]` (default "n/a"). `row["series"] == "s2"` draws in the arm B colour; `series_labels`
    (`{"s1": ..., "s2": ...}`) names the two series in the legend. The highest rate in
    each panel gets the one direct label; every other value is in its `<title>`."""
    rows = list(rows)
    tops: dict[str, int] = {}
    for prefix, _ in panels:
        best = None
        for i, r in enumerate(rows):
            p = r.get(f"{prefix}_rate")
            if p is not None and (best is None or p > rows[best][f"{prefix}_rate"]):
                best = i
        if best is not None:
            tops[prefix] = id(rows[best])
    faded: list[bool] = []

    def draw(r: Row, prefix: str, cy: float, sx: Callable) -> str:
        p, ci, _, n = _rate(r, prefix)
        if p is None:
            return text(sx(0), cy + 4, r.get("na_label") or "n/a", "dim")
        cls = "pt" + (" ho" if r.get(held_key) else "") + (
            " s2" if r.get("series") == "s2" else "")
        if n < dim_below_n or r.get("pending"):
            cls += " lown"
            faded.append(bool(r.get("pending")))
        x = sx(p)
        out = _mark(x, cy, cls, ci, sx, _title(r, prefix, label(r) + ": "))
        if tops.get(prefix) == id(r):
            right = p > 0.8
            out += text(x - 9 if right else x + 9, cy - 6, f"{p * 100:.0f}%", "val",
                        "end" if right else None)
        return out

    legend: list[tuple[str, str]] = []
    if any(r.get("series") == "s2" for r in rows):
        names = series_labels or {}
        legend += [("", names.get("s1", "arm A")), ("s2", names.get("s2", "arm B"))]
    if any(r.get(held_key) for r in rows):
        legend += [("", "public"), ("ho", "held-out")]
    return _panels(rows, panels, legend, draw, label, held_key, href, label_w, panel_w,
                   lambda: (f"faded: fewer than {dim_below_n} in the denominator"
                            + (", or the row is pending" if any(faded) else ""))
                   if faded else None,
                   "rates with 95% intervals: " + ", ".join(t for _, t in panels))


def row_id(row: Row) -> tuple:
    """A board row's identity: (agent, model, condition, held-out, app — None on a
    whole-lane row). `journey.row_key` gives the same tuple for a result in that row."""
    return (row.get("agent"), row.get("model"), row.get("condition"),
            bool(row.get("heldout")), row.get("app"))


def drift(rows_recorded: Iterable[Row], rows_now: Iterable[Row],
          metrics: Sequence[tuple[str, str]], label: Callable[[Row], str] = row_label,
          label_w: float = 220, panel_w: float = 200) -> str:
    """Recorded -> rescored per metric: the recorded rate hollow, the current one filled,
    a connector between them and both whiskers (recorded above, current below). Rows
    pair on (agent, model, condition, heldout, app), in `rows_now` order; a row on one
    side only draws its one mark. A pair that moved gets a direct `+N pp` label."""
    rec = {row_id(r): r for r in rows_recorded}
    now = {row_id(r): r for r in rows_now}
    keys = list(now) + [k for k in rec if k not in now]
    pairs = [{"_rec": rec.get(k), "_now": now.get(k), "heldout": k[3]} for k in keys]

    def lab(pair: Row) -> str:
        return label(pair["_now"] or pair["_rec"])

    def draw(pair: Row, prefix: str, cy: float, sx: Callable) -> str:
        r0, r1 = pair["_rec"], pair["_now"]
        p0 = r0.get(f"{prefix}_rate") if r0 else None
        p1 = r1.get(f"{prefix}_rate") if r1 else None
        if p0 is None and p1 is None:
            return text(sx(0), cy + 4, "n/a", "dim")
        out = _line(sx(p0), cy, sx(p1), cy, "conn") if p0 is not None and p1 is not None else ""
        if p0 is not None:
            out += _mark(sx(p0), cy, "pt rec", r0.get(f"{prefix}_ci"), sx,
                         _title(r0, prefix, "recorded "), -3)
        if p1 is not None:
            out += _mark(sx(p1), cy, "pt", r1.get(f"{prefix}_ci"), sx,
                         _title(r1, prefix, "now "), 3)
        if p0 is not None and p1 is not None and round((p1 - p0) * 100) != 0:
            d = round((p1 - p0) * 100)
            out += text((sx(p0) + sx(p1)) / 2, cy - 6, f"{'+' if d > 0 else '−'}{abs(d)} pp",
                        "val", "middle")
        return out

    return _panels(pairs, metrics, [("rec", "recorded"), ("", "now")], draw, lab,
                   "heldout", None, label_w, panel_w, lambda: None,
                   "recorded vs rescored: " + ", ".join(t for _, t in metrics))


FOREST_PITCH = 24
FLAG = "▼"


def forest(groups: Sequence[Mapping[str, Any]], series_labels: Mapping[str, str] | None = None,
           title: str = "power", label_w: float = 220, panel_w: float = 280) -> str:
    """Two arms per row on one 0-100% axis (QUA-2922): arm A (`a_*` rate fields) above
    in `--s1`, arm B (`b_*`) below in `--s2`, each with its Wilson whisker and a `<title>`.

    `groups` draw in order, each `{"title", "rows", "pooled", "note"}`: a heading, its
    rows (`{"label", "flag", a_rate/a_ci/a_k/a_n, b_rate/...}`; `flag` puts `FLAG` before
    the label), then an optional pooled row (bold label) and an optional dim note under
    it (a test's p-value, say). A None rate draws no mark; the row says `n/a` at the
    panel's right. `series_labels` (`{"s1": ..., "s2": ...}`) names the arms in the
    legend; when any row is flagged the legend says what the flag means."""
    names = {"s1": "arm A", "s2": "arm B", **(series_labels or {})}
    groups = list(groups)
    x0, pw = label_w + 6, panel_w - 12

    def sx(p: float) -> float:
        return x0 + pw * max(0.0, min(1.0, float(p)))

    out, y = [], 6.0
    w = label_w + panel_w + 40
    for cls, name in (("", names["s1"]), ("s2", names["s2"])):     # one arm per line:
        legend, lw = _legend([(cls, name)], label_w, y + 10)        # pins make them long
        out.append(legend)
        w, y = max(w, label_w + lw), y + 16
    flagged = any(r.get("flag") for g in groups for r in g.get("rows") or [])
    y += 4
    if flagged:
        out.append(text(label_w, y + 6, f"{FLAG} = arm B below arm A", "dim"))
        y += 16
    out.append(text(label_w, y + 10, title, "ptitle"))
    y += 18
    top = y
    body = []

    def row(r: Row, label: str, cls: str) -> None:
        nonlocal y
        cy = y + FOREST_PITCH / 2
        marks = [text(0, cy + 4, _fit(label, label_w - 8), cls)]
        missing = []
        for prefix, series, dy in (("a", "s1", -4), ("b", "s2", 4)):
            p, ci, _, _ = _rate(r, prefix)
            if p is None:
                missing.append(names[series])
                continue
            marks.append(_mark(sx(p), cy + dy, "pt" + (" s2" if series == "s2" else ""),
                               ci, sx, _title(r, prefix, f"{r.get('label') or ''} · "
                                              f"{names[series]}: ")))
        if missing:
            marks.append(text(x0 + pw + 6, cy + 4, "n/a: " + ", ".join(missing), "dim"))
        body.append(f'<g class="row">{"".join(marks)}</g>')
        y += FOREST_PITCH

    for g in groups:
        body.append(text(0, y + 13, g.get("title") or "", "ptitle"))
        y += 18
        for r in g.get("rows") or []:
            row(r, (f"{FLAG} " if r.get("flag") else "") + str(r.get("label") or ""), None)
        if g.get("pooled"):
            row(g["pooled"], str(g["pooled"].get("label") or "pooled"), "ptitle")
        if g.get("note"):
            body.append(text(0, y + 8, g["note"], "dim"))
            w = max(w, len(str(g["note"])) * CHAR_W)
            y += 16
        y += 6
    out.append(pct_axis(x0, pw, y, top))
    out.extend(body)
    n_rows = sum(len(g.get("rows") or []) for g in groups)
    return svg(w, y + 22, "".join(out), f"{title} per row, arm A and arm B with 95% intervals "
                                        f"({n_rows} row(s))")


BAR_H = 12
BAR_PITCH = 22


def _bar(x: float, y: float, w: float, h: float) -> str:
    """A bar from the baseline `x`, its far end rounded 4px, its base square."""
    if w < 4:
        return (f'<rect class="bar" x="{_n(x)}" y="{_n(y)}" width="{_n(max(w, 0))}" '
                f'height="{_n(h)}"/>')
    return (f'<path class="bar" d="M{_n(x)} {_n(y)}h{_n(w - 4)}a4 4 0 0 1 4 4v{_n(h - 8)}'
            f'a4 4 0 0 1-4 4h-{_n(w - 4)}z"/>')


def bars(rows: Iterable[Row], prefix: str, title: str, label_w: float = 220,
         panel_w: float = 240) -> str:
    """One series of 0-100% bars, one per row (`{"label", "group", {prefix}_rate/_ci/_k/
    _n}`), `k/n` at each bar's tip and the full rate in its `<title>`. A change of
    `group` leaves a gap. A None rate draws "n/a"."""
    rows = list(rows)
    x0, pw = label_w + 6, panel_w - 12
    out, y = [text(label_w, 16, title, "ptitle")], 24.0
    top, body, last = y, [], None
    for i, r in enumerate(rows):
        if i and r.get("group") != last:
            y += 6
        last = r.get("group")
        cy = y + BAR_PITCH / 2
        label = str(r.get("label") or "")
        p, _, k, n = _rate(r, prefix)
        parts = [text(0, cy + 4, _fit(label, label_w - 8))]
        if p is None:
            parts.append(text(x0, cy + 4, "n/a", "dim"))
        else:
            bw = pw * max(0.0, min(1.0, float(p)))
            bar = _bar(x0, cy - BAR_H / 2, bw, BAR_H) if bw > 0 else ""   # 0%: no bar
            parts.append(f'<g class="bm">{_title(r, prefix, label + ": ")}{bar}'
                         f'{text(x0 + bw + 4, cy + 4, f"{k}/{n}", "val")}</g>')
        body.append(f'<g class="row">{"".join(parts)}</g>')
        y += BAR_PITCH
    y += 4
    out.append(pct_axis(x0, pw, y, top))
    out.extend(body)
    return svg(label_w + panel_w + 40, y + 22, "".join(out), f"{title} ({len(rows)} bar(s))")

def _square(x: float, y: float, s: float, cls: str) -> str:
    return f'<path class="key {cls}" d="M{_n(x)} {_n(y)}h{_n(s)}v{_n(s)}h-{_n(s)}z"/>'


def strip(cells: Iterable[Row], lanes: Sequence[str],
          href_fn: Callable[[Row], str | None] | None = None, uid: str = "strip",
          lane_w: float = 64) -> str:
    """One 10x10 cell per episode: a row per lane (e.g. `("seeded", "clean")`), cells
    grouped by `cell["group"]` (the app) in first-seen order, groups aligned across
    lanes. A cell is `{"lane", "group", "status", "title"}` plus whatever `href_fn`
    reads; `status` is a `STATUS` key (anything else raises). Each cell is a status
    class, a glyph `<text>`, a `<title>` and, when `href_fn` gives one, an `<a>`.
    Excluded cells are hatched with a pattern whose id starts with `uid`, so two strips
    on one page need two uids."""
    cells = list(cells)
    for c in cells:
        if c.get("status") not in STATUS:
            raise ValueError(f"strip: unknown status {c.get('status')!r}")
    groups: list[str] = []
    for c in cells:
        if c.get("group") not in groups:
            groups.append(c.get("group"))
    by: dict[tuple, list[Row]] = {}
    for c in cells:
        by.setdefault((c.get("lane"), c.get("group")), []).append(c)
    pitch = CELL + CELL_GAP
    widths = [max((len(by.get((ln, g), [])) for ln in lanes), default=0) * pitch for g in groups]
    out = [(f'<defs><pattern id="{esc(uid)}-hatch" width="4" height="4" '
            f'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
            f'<path class="hatch" d="M0 0v4"/></pattern></defs>')]
    x, y0 = lane_w, 16.0
    for g, gw in zip(groups, widths):
        if gw:
            name = str(g or "")
            out.append(f'<text x="{_n(x)}" y="10" class="dim"><title>{esc(name)}</title>'
                       f'{esc(_fit(name, gw + GROUP_GAP - 2))}</text>')
            x += gw + GROUP_GAP
    grid_w = max(lane_w, x - GROUP_GAP)
    for li, lane in enumerate(lanes):
        y = y0 + li * (pitch + 6)
        out.append(text(0, y + 9, lane, "dim"))
        x = lane_w
        for g, gw in zip(groups, widths):
            for i, c in enumerate(by.get((lane, g), [])):
                cls, glyph, what = STATUS[c["status"]]
                fill = f' fill="url(#{esc(uid)}-hatch)"' if cls == "st-ex" else ""
                cx = x + i * pitch
                cell = (f'<g class="cell {cls}"><title>{esc(c.get("title") or what)}</title>'
                        f'<rect x="{_n(cx)}" y="{_n(y)}" width="{CELL}" height="{CELL}" '
                        f'rx="2"{fill}/>{text(cx + CELL / 2, y + 8, glyph, None, "middle")}</g>')
                link = href_fn(c) if href_fn else None
                out.append(f'<a href="{esc(link)}">{cell}</a>' if link else cell)
            if gw:
                x += gw + GROUP_GAP
    y = y0 + len(lanes) * (pitch + 6) + 8
    seen = [s for s in STATUS if any(c["status"] == s for c in cells)]
    legend, lx, legend_w, wrap_w = [], 0.0, 0.0, max(grid_w, 480)
    for s in seen:
        cls, glyph, what = STATUS[s]
        item_w = 16 + len(what) * CHAR_W + 14
        if lx and lx + item_w > wrap_w:
            lx, y = 0.0, y + 16
        legend_w = max(legend_w, lx + item_w - 14)
        legend.append(f'<g class="cell {cls}">{_square(lx, y - 9, CELL, cls)}'
                      f'{text(lx + CELL / 2, y - 1, glyph, None, "middle")}</g>'
                      + text(lx + 16, y, what))
        lx += item_w
    out.append(f'<g class="legend">{"".join(legend)}</g>')
    return svg(max(grid_w, legend_w), y + 6, "".join(out), f"{len(cells)} episodes by status")
