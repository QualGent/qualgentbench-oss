"""VH node matching, adapted from LlamaTouch's exact_match. Operates purely on a
uiautomator XML dump (no root): attribute-based single-node match + activity match,
minus the ground-truth-trace / embedding / image-hash paths. Stdlib ElementTree only."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

# Structured attributes compared exactly (booleans normalized from "true"/"false").
# text / content-desc are handled separately as case-insensitive substrings, since
# list rows often decorate the entered value (e.g. "Rent  $1000/mo").
_EXACT_ATTRS = (
    "class", "resource-id", "checked", "checkable", "selected", "enabled",
    "focused", "focusable", "clickable", "long-clickable", "password", "scrollable",
)

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
# uiautomator occasionally emits raw control chars that break XML parsing.
_BAD_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def parse_vh(xml: str) -> Optional[ET.Element]:
    """Parse a uiautomator XML dump into a root Element; None if it isn't one."""
    if not xml or ("<hierarchy" not in xml and "<node" not in xml):
        return None
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        try:
            return ET.fromstring(_BAD_XML_CHARS.sub("", xml))
        except ET.ParseError:
            return None


def parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    """ElementTree nodes carry no parent pointers; ancestor walks need this map."""
    return {child: parent for parent in root.iter() for child in parent}


def nearest_clickable(node: ET.Element,
                      pmap: dict[ET.Element, ET.Element]) -> Optional[ET.Element]:
    """The node itself if clickable, else its closest clickable ancestor, else None.
    A tap only means something when some ancestor consumes it — which one, and how
    big, is what disambiguates two nodes carrying the same label."""
    cur: Optional[ET.Element] = node
    while cur is not None:
        if (cur.get("clickable") or "").lower() == "true":
            return cur
        cur = pmap.get(cur)
    return None


def _norm_bool(value):
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def _node_matches(node: ET.Element, matcher: dict) -> bool:
    # any_text: the value appears in text OR content-desc (case-insensitive
    # substring). Robust to list-vs-grid/card rendering, where the same label
    # lands in `text` in one layout and `content-desc` in another.
    any_text = matcher.get("any_text")
    if any_text is not None:
        want = str(any_text).strip().lower()
        if (want not in (node.get("text") or "").lower()
                and want not in (node.get("content-desc") or "").lower()):
            return False
    for key in ("text", "content-desc"):
        want = matcher.get(key)
        if want is not None:
            if str(want).strip().lower() not in (node.get(key) or "").lower():
                return False
    for key in _EXACT_ATTRS:
        if key not in matcher:
            continue
        want = matcher[key]
        node_val = _norm_bool(node.get(key))
        if isinstance(want, bool):
            if node_val is not want:
                return False
        elif (node.get(key) or "") != str(want):
            return False
    return True


def node_present(xml: str, matcher: dict) -> bool:
    """True if any node in the dump matches every attribute in `matcher`."""
    root = parse_vh(xml)
    if root is None:
        return False
    return any(_node_matches(node, matcher) for node in root.iter())


def find_center(xml: str, matcher: dict) -> Optional[Tuple[int, int]]:
    """Center (x, y) of the first node matching `matcher`, for nav taps."""
    root = parse_vh(xml)
    if root is None:
        return None
    for node in root.iter():
        if _node_matches(node, matcher):
            m = _BOUNDS_RE.search(node.get("bounds") or "")
            if not m:
                continue
            left, top, right, bottom = map(int, m.groups())
            return (left + right) // 2, (top + bottom) // 2
    return None


def activity_matches(current: str, expected_substr: str) -> bool:
    return expected_substr.lower() in (current or "").lower()


def visible_texts(xml: str, limit: int = 40) -> list:
    """Distinct non-empty text + content-desc strings on screen — so a failed
    verification records what was actually there (data-driven spec calibration)."""
    root = parse_vh(xml)
    if root is None:
        return []
    seen: list = []
    for node in root.iter():
        for key in ("text", "content-desc"):
            v = (node.get(key) or "").strip()
            if v and v not in seen:
                seen.append(v)
                if len(seen) >= limit:
                    return seen
    return seen


def find_button(xml: str, label: str) -> Optional[Tuple[int, int]]:
    """Center of a node whose text/content-desc EQUALS `label` (case-insensitive), for
    overlay dismissal. Only a node with a clickable ancestor counts as a button — plain
    prose "OK" must not get tapped, and Compose renders button text as a non-clickable child."""
    root = parse_vh(xml)
    if root is None:
        return None
    pmap = parent_map(root)
    target = label.strip().lower()
    for node in root.iter():
        text = (node.get("text") or "").strip().lower()
        desc = (node.get("content-desc") or "").strip().lower()
        if text == target or desc == target:
            if nearest_clickable(node, pmap) is None:
                continue
            m = _BOUNDS_RE.search(node.get("bounds") or "")
            if m:
                left, top, right, bottom = map(int, m.groups())
                return (left + right) // 2, (top + bottom) // 2
    return None
