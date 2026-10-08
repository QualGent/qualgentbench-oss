"""The shared instant tooltip (QUA-2948): `tooltip.finish` and the static contract."""

from __future__ import annotations

import re

from qualgentbench import tooltip

PAGE = ("<!doctype html><html><head><title>t</title></head><body>"
        '<th data-term="a" data-tip="Alpha: first">a</th>'
        '<a href="#x" data-tip="Alpha: first">?</a>'
        '<span class="chk" data-tip="B &amp; c">b</span>'
        '<label data-tip="Gamma"><input type="checkbox"> g</label>'
        '<span data-tip="Delta" aria-describedby="own">d</span>'
        '<svg class="chart" viewBox="0 0 1 1"><g class="cell"><title>mark</title></g></svg>'
        "<script>const x = `<b data-tip=\"${t}\">`;</script>"
        "</body></html>\n")


def test_finish_describes_each_target_once_per_text_and_makes_it_focusable():
    out = tooltip.finish(PAGE)
    assert out == tooltip.finish(PAGE)                                   # pure
    assert ('<th data-term="a" data-tip="Alpha: first" aria-describedby="qtd-1" '
            'tabindex="0">') in out
    assert '<a href="#x" data-tip="Alpha: first" aria-describedby="qtd-1">' in out
    assert '<span class="chk" data-tip="B &amp; c" aria-describedby="qtd-2" tabindex="0">' in out
    assert '<label data-tip="Gamma" aria-describedby="qtd-3">' in out    # its input focuses
    assert '<span data-tip="Delta" aria-describedby="own">' in out       # left alone
    assert ('<div id="qtip-d" hidden><span id="qtd-1">Alpha: first</span>'
            '<span id="qtd-2">B &amp; c</span><span id="qtd-3">Gamma</span></div>\n'
            + tooltip.SCRIPT + "\n</body></html>\n") in out
    assert "`<b data-tip=\"${t}\">`" in out                             # scripts untouched
    assert "<title>mark</title>" in out                                  # SVG keeps <title>


def test_a_page_without_targets_still_gets_the_script_and_no_description_block():
    out = tooltip.finish("<html><body><p>x</p></body></html>")
    assert 'id="qtip-d"' not in out and out.count(tooltip.SCRIPT) == 1
    assert tooltip.finish("<p>fragment</p>") == "<p>fragment</p>"


def test_the_script_and_styles_are_self_contained_and_small():
    for text in (tooltip.SCRIPT, tooltip.CSS):
        for bad in ("src=", "http", "xmlns", "import(", "fetch(", "innerHTML"):
            assert bad not in text, bad
    body = tooltip.SCRIPT.removeprefix("<script>").removesuffix("</script>").strip()
    assert len(body.splitlines()) <= 90
    for name in ("--tip-bg", "--tip-fg", "--tip-border", ".qtip", "[data-tip]",
                 "prefers-reduced-motion", "prefers-color-scheme:dark"):
        assert name in tooltip.CSS, name
    for event in ("pointerover", "pointerout", "pointerdown", "focusin", "focusout",
                  "'Escape'", "'scroll'"):
        assert event in tooltip.SCRIPT, event


def _luminance(hex_: str) -> float:
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_the_tooltip_colours_pass_contrast_in_light_and_dark():
    """Text on the card >= 4.5:1 (WCAG AA) and the card stands off the page, both modes
    (the page backgrounds are `view.CSS`'s `--bg`)."""
    light, dark = re.findall(r"--tip-bg:(#\w{6});--tip-fg:(#\w{6})", tooltip.CSS)
    for (bg, fg), page in ((light, "#ffffff"), (dark, "#161618")):
        assert _contrast(bg, fg) >= 4.5, (bg, fg)
        assert _contrast(bg, page) >= 3, (bg, page)
