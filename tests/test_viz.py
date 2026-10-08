"""`viz` — the view's inline-SVG chart primitives (QUA-2916).

Pinned: every helper renders an `<svg class="chart">` with no `http`, no `xmlns` and no
`<script` (the view's no-CDN rule); labels and links are escaped; N rows draw N row
groups; held-out rows carry the hollow class; the same input renders byte-identical
output; a strip draws one rect per cell and every status cell carries a glyph; and the
colours used are only the classes `view.CSS` defines. Rows are synthetic.
"""

from __future__ import annotations

import re

import pytest

from qualgentbench import rates, viz
from qualgentbench.view import CSS

PANELS = [("false_alarm", "false alarm / clean case"), ("catch", "catch / seeded defect")]


def _row(model: str, fa: tuple[int, int], catch: tuple[int, int], held: bool = False,
         **extra) -> dict:
    row = {"agent": "agent-x", "model": model, "condition": "mcp", "heldout": held, **extra}
    for prefix, (k, n) in (("false_alarm", fa), ("catch", catch)):
        r = rates.rate(k, n)
        row.update(r.as_fields(prefix) if r else rates.empty_fields(prefix))
    return row


ROWS = [_row("model-a", (2, 20), (18, 22)), _row("model-b", (9, 20), (12, 22)),
        _row("model-c", (1, 4), (3, 5), held=True), _row("model-d", (0, 0), (5, 5), held=True)]


def _cells() -> list[dict]:
    seeded = ["caught", "artifact", "silent", "unreached", "truncated", "excluded"]
    cells = [{"lane": "seeded", "group": f"app-{i % 2}", "status": s, "title": f"case {i}",
              "key": f"k{i}"} for i, s in enumerate(seeded)]
    cells += [{"lane": "clean", "group": "app-0", "status": s, "key": f"c{i}"}
              for i, s in enumerate(["clean", "false_report", "excluded"])]
    return cells


def _all_charts() -> list[str]:
    return [
        viz.dots_ci(ROWS, PANELS, href=lambda r: f"#{r['model']}"),
        viz.drift(ROWS[:2], [_row("model-a", (1, 20), (20, 22)), ROWS[1]], PANELS),
        viz.strip(_cells(), ("seeded", "clean"), lambda c: f"ep/{c['key']}.html"),
        viz.svg(10, 10, viz.text(1, 2, "x") + viz.pct_axis(0, 100, 5, top=0)),
    ]


def test_every_helper_renders_inline_svg_with_no_cdn_namespace_or_script():
    for out in _all_charts():
        assert out.startswith('<svg class="chart" viewBox=') and out.endswith("</svg>")
        assert "http" not in out
        assert "xmlns" not in out
        assert "<script" not in out


def test_same_input_is_byte_identical():
    assert _all_charts() == _all_charts()
    # and not order-sensitive to anything but the input: fresh copies render the same
    rows = [dict(r) for r in ROWS]
    assert viz.dots_ci(rows, PANELS) == viz.dots_ci(ROWS, PANELS)


def test_labels_and_links_are_escaped():
    evil = '<b onload="x">&'
    out = viz.dots_ci([_row("m", (1, 10), (2, 10), label=evil)], PANELS,
                      href=lambda r: '"><script>')
    assert evil not in out and "<b " not in out and "<script>" not in out
    assert "&lt;b onload=&quot;x&quot;&gt;&amp;" in out
    assert 'href="&quot;&gt;&lt;script&gt;"' in out
    cells = [{"lane": "seeded", "group": "<g>", "status": "caught", "title": "<t>"}]
    s = viz.strip(cells, ("seeded",), lambda c: "a&b")
    assert "<t>" not in s and "&lt;t&gt;" in s and "&lt;g&gt;" in s and 'href="a&amp;b"' in s
    assert "<x>" not in viz.text(0, 0, "<x>")


def test_n_rows_draw_n_row_groups_and_a_mark_per_defined_rate():
    out = viz.dots_ci(ROWS, PANELS)
    assert out.count('<g class="row">') == len(ROWS)
    # model-d has no clean episodes: its false-alarm panel says n/a, never 0%
    assert out.count('<circle class="dot"') == 2 * len(ROWS) - 1
    assert ">n/a<" in out
    # one whisker per mark, from the row's own *_ci, and a title with k/n p% [lo–hi]
    assert out.count('class="whisker"') == 2 * len(ROWS) - 1
    assert "<title>model-a · agent-x · mcp: 18/22 82% [61–93]</title>" in out


def test_held_out_rows_are_hollow_and_come_after_public():
    out = viz.dots_ci(list(reversed(ROWS)), PANELS)
    hollow = re.findall(r'<g class="pt ho[^"]*">', out)
    assert len(hollow) == 3                      # model-c twice, model-d's catch once
    block = out.index('class="dim">held-out</text>')     # the block header, not the legend
    assert out.index(">model-a") < block < out.index(">model-c")
    assert 'class="key ho"' in out               # the legend names the hollow mark
    public_only = viz.dots_ci(ROWS[:2], PANELS)
    assert "pt ho" not in public_only and "held-out" not in public_only


def test_low_n_marks_fade_with_a_note_and_one_direct_label_per_panel():
    out = viz.dots_ci(ROWS, PANELS)
    assert out.count("lown") == 1                # model-c's 1/4 false alarm; n = 5 is not faded
    assert "faded: fewer than 5" in out
    assert out.count('class="val"') == len(PANELS)
    assert "45%</text>" in out and "100%</text>" in out
    assert "lown" not in viz.dots_ci(ROWS[:2], PANELS)


def test_arm_b_rows_use_the_second_series_and_a_legend():
    rows = [_row("m", (1, 10), (5, 10), label="A"), _row("m", (2, 10), (7, 10), label="B",
                                                         series="s2")]
    out = viz.dots_ci(rows, PANELS, series_labels={"s1": "arm one", "s2": "arm two"})
    assert out.count('class="pt s2"') == 2
    assert "arm one" in out and "arm two" in out and 'class="key s2"' in out


def test_strip_draws_one_rect_per_cell_and_a_glyph_per_status_cell():
    cells = _cells()
    out = viz.strip(cells, ("seeded", "clean"), lambda c: f"ep/{c['key']}.html")
    assert out.count("<rect") == len(cells)
    for g in re.findall(r'<g class="cell [^"]+">.*?</g>', out):
        assert re.search(r"<text [^>]*>[^<]+</text>", g), g
    for status, (cls, glyph, label) in viz.STATUS.items():
        assert f'class="cell {cls}"' in out and glyph in out and label in out, status
    assert out.count("<a href=") == len(cells)
    assert 'fill="url(#strip-hatch)"' in out and 'id="strip-hatch"' in out
    assert 'id="b-hatch"' in viz.strip(cells, ("seeded",), None, uid="b")
    assert "<a " not in viz.strip(cells, ("seeded",), None)


def test_strip_refuses_an_unknown_status():
    with pytest.raises(ValueError, match="unknown status"):
        viz.strip([{"lane": "seeded", "group": "a", "status": "passed"}], ("seeded",))


def test_drift_pairs_rows_and_labels_what_moved():
    now = [_row("model-a", (1, 20), (20, 22)), ROWS[1]]
    out = viz.drift(ROWS[:2], now, PANELS)
    assert out.count('<g class="row">') == 2
    assert out.count('class="pt rec"') == 4 and out.count('class="pt"') == 4
    assert out.count('class="conn"') == 4
    assert "−5 pp" in out and "+9 pp" in out
    assert out.count('class="val"') == 2         # model-b did not move: no label
    assert "<title>recorded 2/20 10% [3–30]</title>" in out
    assert "<title>now 1/20 5% [1–24]</title>" in out
    # a row on one side only draws its one mark
    one = viz.drift([], [ROWS[0]], PANELS)
    assert 'class="pt rec"' not in one and one.count('class="pt"') == 2


def test_chart_classes_and_tokens_are_in_the_view_stylesheet():
    for token in ("--s1:#2a78d6", "--s1:#3987e5", "--s2:#eb6834", "--s2:#d95926",
                  "--seq1:", "--seq7:", "--st-good:#0ca30c", "--st-warn:#fab219",
                  "--st-serious:#ec835a", "--st-crit:#d03b3b"):
        assert token in CSS, token
    used = set()
    for out in _all_charts():
        for attr in re.findall(r'class="([^"]+)"', out):
            used.update(attr.split())
    for cls in sorted(used - {"row"}):           # `row` is a structural hook, not a style
        assert re.search(rf"\.{re.escape(cls)}\b", CSS), cls
    # the CVD-failing --ok/--err pair is never a chart fill
    chart_css = CSS[CSS.index("svg.chart"):]
    assert "var(--ok)" not in chart_css and "var(--err)" not in chart_css
