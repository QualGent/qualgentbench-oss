"""The tooltip contract every page the view writes must keep (QUA-2948), as one check
the run-view and experiment-view tests share."""

from __future__ import annotations

import html
import re

from qualgentbench import tooltip

_SCRIPTS = re.compile(r"<script\b.*?</script>", re.DOTALL)
_TAG = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)(\s[^<>]*)?>")
_FOCUSABLE = {"a", "button", "input", "select", "textarea", "label", "summary"}


def check_page(page: str, where: str = "") -> int:
    """Assert `page` keeps the contract and return how many `data-tip` targets it has:
    no `title` attribute anywhere (markup or the rows its script draws); every target
    described by a span in the hidden `#qtip-d` block carrying its exact text, and
    focusable; the shared script once, just before `</body>`; nothing fetched."""
    assert page.count(tooltip.SCRIPT) == 1, where
    assert page.rstrip().endswith(tooltip.SCRIPT + "\n</body></html>"), where
    for bad in ("<script src", "http://", "https://", "xmlns"):
        assert bad not in page, (where, bad)
    for script in _SCRIPTS.findall(page):
        assert 'title="' not in script, where
    markup = _SCRIPTS.sub(" ", page)
    m = re.search(r'<div id="qtip-d" hidden>(.*?)</div>', markup, re.DOTALL)
    desc = dict(re.findall(r'<span id="(qtd-\d+)">([^<]*)</span>', m.group(1))) if m else {}
    tips = 0
    for name, attrs in _TAG.findall(markup):
        assert not re.search(r"\stitle=", attrs or ""), (where, name, attrs)
        tip = re.search(r'\sdata-tip="([^"]*)"', attrs or "")
        if not tip:
            continue
        tips += 1
        ref = re.search(r'\saria-describedby="([^"]+)"', attrs)
        assert ref and desc.get(ref.group(1)) == tip.group(1), (where, attrs)
        assert html.unescape(tip.group(1)).strip(), (where, attrs)
        if name.lower() not in _FOCUSABLE:
            assert 'tabindex="0"' in attrs, (where, attrs)
    assert len(desc) == len(set(desc.values())), where             # one span per text
    return tips
