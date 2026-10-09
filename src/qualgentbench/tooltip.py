"""Instant, styled hover help for the view's pages (QUA-2948).

A native `title` tooltip waits about a second, cannot be styled and never shows on a
touch screen. Every element with hover help carries `data-tip="<plain text>"` instead
(no `title`, so no second native tooltip), and one small inline script shows it in a
shared `position: fixed` card the moment the pointer or keyboard focus arrives. SVG
chart marks keep their `<title>`: the script reads it into the same card and takes it
off the mark while the mouse is over it, so the browser's own tooltip never fires.

The contract (shared with any page that wants to look the same):

  markup   `[data-tip]` on the target; `.qtip` is the floating card (`aria-hidden`).
           `finish(page)` gives each target `aria-describedby` to a span in the
           hidden `#qtip-d` block (the help text without JavaScript) and
           `tabindex="0"` where the target is not focusable already.
  colours  `--tip-bg`, `--tip-fg`, `--tip-border` (light: dark card on the light page;
           dark: light card on the dark page; both pairs at least 14:1).
  script   `SCRIPT`: static text, no `src`, no network, no dependencies.

Public API:

  CSS            the card's styles (appended to the view's `style.css`)
  SCRIPT         the inline `<script>` every page ends with
  attr(tip)      ` data-tip="…"` for an element with hover help
  finish(page)   a whole page with descriptions, focus stops and the script added
"""

from __future__ import annotations

import html
import re

#: Tags a keyboard already reaches (or that hand focus to a control inside them): no
#: `tabindex` added.
_FOCUSABLE = frozenset({"a", "button", "input", "select", "textarea", "label", "summary"})

CSS = """
:root{--tip-bg:#1d1d1f;--tip-fg:#f5f5f7;--tip-border:#3a3a3e;--tip-shadow:0 4px 14px #0000002e}
@media (prefers-color-scheme:dark){:root{--tip-bg:#ececef;--tip-fg:#1d1d1f;--tip-border:#c7c7cc;
 --tip-shadow:0 4px 16px #00000080}}
[data-tip]:not(a){cursor:help}.term[data-tip]{text-decoration:underline dotted var(--dim)}
[data-tip]:focus-visible{outline:2px solid var(--link);outline-offset:1px}
.qtip{position:fixed;left:0;top:0;z-index:1000;max-width:min(300px,calc(100vw - 16px));
 box-sizing:border-box;padding:8px 10px;border-radius:6px;border:1px solid var(--tip-border);
 background:var(--tip-bg);color:var(--tip-fg);box-shadow:var(--tip-shadow);
 font:13px/1.45 -apple-system,system-ui,sans-serif;text-align:left;white-space:normal;
 overflow-wrap:anywhere;pointer-events:none;opacity:0;visibility:hidden;
 transition:opacity 60ms ease-out,visibility 0s linear 60ms}
.qtip.on{opacity:1;visibility:visible;transition:opacity 60ms ease-out}
.qtip b{font-weight:600}
.qtip::after{content:"";position:absolute;left:var(--qtip-ax,50%);bottom:-5px;width:8px;height:8px;
 background:var(--tip-bg);border:solid var(--tip-border);border-width:0 1px 1px 0;
 transform:translateX(-50%) rotate(45deg)}
.qtip.below::after{top:-5px;bottom:auto;border-width:1px 0 0 1px}.qtip.side::after{display:none}
@media (prefers-reduced-motion:reduce){.qtip,.qtip.on{transition:none}}
"""

SCRIPT = """<script>
(() => {
  const d = document, T = d.createElement('div'), ids = new Map();
  T.className = 'qtip'; T.setAttribute('aria-hidden', 'true');
  let D = d.getElementById('qtip-d'), cur = null, stash = null;
  if (!D) { D = d.createElement('div'); D.id = 'qtip-d'; D.hidden = true; d.body.appendChild(D); }
  D.querySelectorAll('span[id]').forEach(s => ids.set(s.textContent, s.id));
  const FOCUS = 'a[href],button,input,select,textarea,label,summary,[tabindex]';
  const prep = () => d.querySelectorAll('[data-tip]:not([aria-describedby])').forEach(el => {
    const s = el.getAttribute('data-tip');
    if (!ids.has(s)) {
      const sp = d.createElement('span'); sp.id = 'qtd-r' + ids.size; sp.textContent = s;
      D.appendChild(sp); ids.set(s, sp.id);
    }
    el.setAttribute('aria-describedby', ids.get(s));
    if (!el.matches(FOCUS)) el.tabIndex = 0;
  });
  const titled = n => [...n.children].find(c => c.tagName === 'title');
  function host(el) {
    if (!el || !el.closest) return null;
    if (el.closest('svg')) {
      for (let n = el; n && n.tagName !== 'svg'; n = n.parentNode) if (titled(n)) return n;
      const t = el.tagName === 'a' && el.querySelector('title');
      if (t) return t.parentNode;
    }
    return el.closest('[data-tip]');
  }
  function hide() {
    if (stash && cur) cur.insertBefore(stash, cur.firstChild);
    stash = cur = null; T.classList.remove('on');
  }
  function place(h) {
    const r = h.getBoundingClientRect(), W = innerWidth, H = innerHeight, g = 8, a = 8;
    const w = T.offsetWidth, t = T.offsetHeight, cx = r.left + r.width / 2;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));
    let x, y, side = r.top - t - a >= g ? 'above' : r.bottom + t + a <= H - g ? 'below'
      : r.right + w + a <= W - g ? 'right' : r.left - w - a >= g ? 'left'
      : r.top > H - r.bottom ? 'above' : 'below';
    if (side === 'above' || side === 'below') {
      x = clamp(cx - w / 2, g, W - g - w);
      y = clamp(side === 'above' ? r.top - t - a : r.bottom + a, g, Math.max(g, H - g - t));
      T.style.setProperty('--qtip-ax', clamp(cx - x, 10, w - 10) + 'px');
    } else {
      x = side === 'right' ? r.right + a : r.left - w - a;
      y = clamp(r.top + r.height / 2 - t / 2, g, Math.max(g, H - g - t));
    }
    T.className = 'qtip' + (side === 'below' ? ' below' : side === 'above' ? '' : ' side');
    T.style.left = x + 'px'; T.style.top = y + 'px';
  }
  function show(h, mouse) {
    if (h === cur) return;
    hide();
    let s = h.getAttribute('data-tip');
    if (s === null) {
      const t = titled(h);
      if (!t) return;
      s = t.textContent;
      if (mouse) { stash = t; t.remove(); }
    }
    cur = h; T.textContent = '';
    const m = /^([^:.]{1,48}): (.+)$/s.exec(s);
    if (m) { const b = d.createElement('b'); b.textContent = m[1] + ':'; T.append(b, ' ' + m[2]); }
    else T.textContent = s;
    if (!T.isConnected) d.body.appendChild(T);
    place(h); T.classList.add('on');
  }
  d.addEventListener('pointerover', e => {
    if (e.pointerType !== 'mouse') return;
    const h = host(e.target);
    if (h && !(cur && h !== cur && h.contains(cur))) show(h, true);
    else if (!h && cur && !cur.contains(e.target)) hide();
  });
  d.addEventListener('pointerout', e => {
    if (e.pointerType === 'mouse' && cur && !(e.relatedTarget && cur.contains(e.relatedTarget))) hide();
  });
  d.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse') return;
    const h = host(e.target);
    if (h && h !== cur) show(h, false); else hide();
  }, true);
  d.addEventListener('focusin', e => { const h = host(e.target); if (h) show(h, false); });
  d.addEventListener('focusout', e => {
    if (cur && (e.target === cur || e.target.contains(cur))
        && !(e.relatedTarget && cur.contains(e.relatedTarget))) hide();
  });
  d.addEventListener('keydown', e => { if (e.key === 'Escape') hide(); });
  addEventListener('scroll', hide, true); addEventListener('resize', hide);
  prep(); new MutationObserver(prep).observe(d.body, {childList: true, subtree: true});
})();
</script>"""

_SCRIPT_BLOCK = re.compile(r"(<script\b.*?</script>)", re.DOTALL | re.IGNORECASE)
_START_TAG = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)(\s[^<>]*?)?(/?)>")
_TIP = re.compile(r'\sdata-tip="([^"]*)"')


def attr(tip: str) -> str:
    """The attribute of an element whose hover help is `tip` (plain text)."""
    return f' data-tip="{html.escape(tip)}"'


def finish(page: str) -> str:
    """`page` with every `data-tip` target described (`aria-describedby` to a span in
    the hidden `#qtip-d` block, one per distinct text, numbered in page order) and
    focusable, and the tooltip script before `</body>`. Script blocks are left alone
    (the rows a page draws itself are described by the script at run time). Pure:
    the same page in, the same bytes out."""
    ids: dict[str, str] = {}

    def tag(m: re.Match) -> str:
        name, attrs, close = m.group(1), m.group(2) or "", m.group(3)
        tip = _TIP.search(attrs)
        if tip is None or "aria-describedby=" in attrs:
            return m.group(0)
        text = tip.group(1)
        if text not in ids:
            ids[text] = f"qtd-{len(ids) + 1}"
        extra = f' aria-describedby="{ids[text]}"'
        if name.lower() not in _FOCUSABLE and "tabindex=" not in attrs:
            extra += ' tabindex="0"'
        return f"<{name}{attrs[:tip.end()]}{extra}{attrs[tip.end():]}{close}>"

    parts = _SCRIPT_BLOCK.split(page)
    page = "".join(p if i % 2 else _START_TAG.sub(tag, p) for i, p in enumerate(parts))
    desc = ("".join(f'<span id="{i}">{t}</span>' for t, i in ids.items()))
    block = f'<div id="qtip-d" hidden>{desc}</div>\n' if ids else ""
    head, sep, tail = page.rpartition("</body>")
    if not sep:
        return page
    return f"{head}{block}{SCRIPT}\n</body>{tail}"
