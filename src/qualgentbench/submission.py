"""The portable findings contract — one schema, identical in both ablation arms.
`AREA:` lines remain a fallback; both channels are read from the same ordered
stream, last write wins."""

from __future__ import annotations

from dataclasses import dataclass, field

import re

import yaml

FILENAME = "findings.yaml"

VERDICTS = ("as_specified", "deviates", "blocked")


@dataclass
class Step:
    """One replayable action, anchored on TEXT — a coordinate repro cannot be
    replayed on a fresh device."""

    action: str
    value: str = ""

    def as_dict(self) -> dict:
        return {"action": self.action, "value": self.value}


@dataclass
class Expectation:
    """A checkable post-condition: `present`/`absent` (screen text — all an
    agent can write) or `db` (harness-only, reads the app's own database)."""

    mode: str                 # "present" | "absent" | "db"
    text: str = ""            # present/absent
    db: str = ""              # db: filename under databases/
    query: str = ""           # db: SQL, one scalar
    equals: str = ""          # db: expected result, compared as a string

    def as_dict(self) -> dict:
        if self.mode == "db":
            return {"mode": "db", "db": self.db, "query": self.query,
                    "equals": self.equals}
        return {"mode": self.mode, "text": self.text}


# The replayable action vocabulary — kept small: every verb must run
# identically on any device, and an agent must be able to emit it unaided.
ACTIONS = {
    "launch": False,     # value required?
    "relaunch": False,
    "wait": False,
    "tap": True,
    # Selection mode in list apps is only reachable by long press.
    "long_press": True,
    "type": True,        # SETS the field's value, clearing whatever it held
    # Appends at the cursor; only differs from `type` on a pre-filled field.
    "append": True,
    "press": True,       # back | home | enter
    "swipe": True,       # up | down | left | right
}

_PRESS_KEYS = ("back", "home", "enter")
_SWIPE_DIRS = ("up", "down", "left", "right")


@dataclass
class Claim:
    area: str
    verdict: str
    expected: str = ""
    actual: str = ""
    # A machine-executable reproduction, captured but not yet scored — a run
    # recorded today can be verified later; one without repros never can.
    steps: list[Step] = field(default_factory=list)
    expect: Expectation | None = None

    @property
    def replayable(self) -> bool:
        return bool(self.steps and self.expect)


@dataclass
class Submission:
    claims: list[Claim] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.claims) and not self.errors


def _norm_verdict(raw: object) -> str | None:
    """Accept the asked-for vocabulary plus two common spellings. Deliberately
    not fuzzy: mapping the guided-mode words here would silently accept a
    report written against the wrong contract."""
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if v in VERDICTS:
        return v
    return {"as_expected": "as_specified", "deviation": "deviates"}.get(v)


_ENTRY_RE = re.compile(r"^\s*-\s+(?:area|id)\s*:", re.M)


def _salvage_entries(text: str) -> list[dict]:
    """Parse a broken findings file one entry at a time — split on `- area:`
    lines, load each chunk alone; a chunk that still fails is dropped and the
    rest survive."""
    starts = [m.start() for m in _ENTRY_RE.finditer(text)]
    out: list[dict] = []
    for i, s in enumerate(starts):
        chunk = text[s: starts[i + 1] if i + 1 < len(starts) else len(text)]
        lines = chunk.splitlines()
        pad = len(lines[0]) - len(lines[0].lstrip())
        flat = "\n".join(ln[pad:] if len(ln) >= pad else ln for ln in lines)
        try:
            got = yaml.safe_load(flat)
        except yaml.YAMLError:
            continue
        if isinstance(got, list):
            out.extend(e for e in got if isinstance(e, dict))
        elif isinstance(got, dict):
            out.append(got)
    return out


def parse(text: str, known_areas: set[str] | None = None) -> Submission:
    """Parse a findings.yaml payload (a top-level list is also accepted).
    Naming an unknown area is an ERROR, not a silent skip — a typo'd id would
    otherwise read as a missing verdict and score 0 with no diagnosis."""
    sub = Submission()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # One bad line must not cost every finding — an unquoted `actual:`
        # containing ": " reads as nested YAML and breaks the whole document.
        entries = _salvage_entries(text)
        if not entries:
            sub.errors.append(f"invalid YAML: {str(exc).splitlines()[0]}")
            return sub
        sub.errors.append(
            f"invalid YAML: {str(exc).splitlines()[0]} — recovered "
            f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} individually")
        return _entries_to_claims(entries, sub, known_areas)

    if isinstance(doc, dict):
        entries = doc.get("findings", doc.get("areas"))
    else:
        entries = doc
    if entries is None:
        sub.errors.append("no `findings:` key")
        return sub
    if not isinstance(entries, list):
        sub.errors.append("`findings` must be a list")
        return sub
    return _entries_to_claims(entries, sub, known_areas)


def _entries_to_claims(entries: list, sub: "Submission",
                       known_areas: set[str] | None) -> "Submission":
    """Turn parsed entries into claims. Shared by the normal and salvage paths,
    so a recovered file is validated identically."""
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            sub.errors.append(f"entry {i}: not a mapping")
            continue
        area = entry.get("area") or entry.get("id")
        if not isinstance(area, str) or not area.strip():
            sub.errors.append(f"entry {i}: missing `area`")
            continue
        area = area.strip()
        verdict = _norm_verdict(entry.get("verdict"))
        if verdict is None:
            sub.errors.append(
                f"{area}: verdict must be one of {'|'.join(VERDICTS)}, "
                f"got {entry.get('verdict')!r}")
            continue
        if known_areas is not None and area not in known_areas:
            sub.errors.append(f"{area}: not an area of this app")
            continue
        # Last wins, matching the AREA-line rule: agents may correct themselves.
        if area in seen:
            sub.claims = [c for c in sub.claims if c.area != area]
        seen.add(area)
        steps, step_errs = _parse_steps(entry.get("steps"), area)
        expect, exp_err = _parse_expect(entry.get("expect") or entry.get("expectation"), area)
        # A malformed repro must never cost a verdict — errors are recorded
        # and the claim stands on its own.
        sub.errors.extend(step_errs)
        if exp_err:
            sub.errors.append(exp_err)
        sub.claims.append(Claim(
            area=area,
            verdict=verdict,
            expected=str(entry.get("expected") or "").strip(),
            actual=str(entry.get("actual") or "").strip(),
            steps=steps,
            expect=expect,
        ))
    return sub


def _parse_steps(raw: object, area: str) -> tuple[list[Step], list[str]]:
    """`steps:` as a list of bare verbs (`launch`) or single-key maps (`tap: "Save"`)."""
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [f"{area}: `steps` must be a list"]
    steps: list[Step] = []
    errors: list[str] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            action, value = item.strip().lower(), ""
        elif isinstance(item, dict) and len(item) == 1:
            (key, val), = item.items()
            action, value = str(key).strip().lower(), str(val if val is not None else "").strip()
        else:
            errors.append(f"{area} step {i}: expected a verb or a single-key mapping")
            continue
        if action not in ACTIONS:
            errors.append(f"{area} step {i}: unknown action {action!r} "
                          f"(one of {', '.join(sorted(ACTIONS))})")
            continue
        if ACTIONS[action] and not value:
            errors.append(f"{area} step {i}: `{action}` needs a value")
            continue
        if action == "press" and value.lower() not in _PRESS_KEYS:
            errors.append(f"{area} step {i}: press must be {'|'.join(_PRESS_KEYS)}")
            continue
        if action == "swipe" and value.lower() not in _SWIPE_DIRS:
            errors.append(f"{area} step {i}: swipe must be {'|'.join(_SWIPE_DIRS)}")
            continue
        steps.append(Step(action, value))
    return steps, errors


def _parse_expect(raw: object, area: str) -> tuple[Expectation | None, str | None]:
    """`{present: "text"}`, `{absent: "text"}`, or the harness-only database form
    `{db: "notes.db", query: "select ...", equals: "1"}`."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict) or not raw:
        return None, f"{area}: `expect` must be a mapping"
    if "db" in raw:
        db = str(raw.get("db") or "").strip()
        query = str(raw.get("query") or "").strip()
        if not db or not query:
            return None, f"{area}: a `db` expectation needs both `db` and `query`"
        equals = raw.get("equals")
        if equals is None:
            return None, f"{area}: a `db` expectation needs `equals`"
        return Expectation("db", db=db, query=query, equals=str(equals).strip()), None
    if len(raw) != 1:
        return None, f"{area}: `expect` must be {{present: <text>}} or {{absent: <text>}}"
    (mode, text), = raw.items()
    mode = str(mode).strip().lower()
    if mode not in ("present", "absent"):
        return None, f"{area}: `expect` mode must be present|absent, got {mode!r}"
    text = str(text if text is not None else "").strip()
    if not text:
        return None, f"{area}: `expect` needs the text to look for"
    return Expectation(mode, text=text), None


def instruction(areas: list[str]) -> str:
    """The reporting section of the brief. Byte-identical in both arms by
    construction — it names no tool, because naming one is what split them."""
    example = areas[0] if areas else "<area>"
    return f"""HOW TO REPORT
Report each area AS SOON AS you finish examining it — do not save them for the end.

Maintain a file `{FILENAME}` in your working directory. After you finish examining an
area, add (or correct) its entry and save the file:

  findings:
    - area: {example}
      verdict: as_specified|deviates|blocked
      expected: what the spec says should happen
      actual: what you observed on the device
      steps:                 # how to reproduce what you observed, from a fresh start
        - launch
        - tap: "New"
        - type: "QA test entry"
        - tap: "Save"
        - press: back
        - relaunch
      expect:                # the check that decides it, on the screen you end on
        present: "QA test entry"

`steps` and `expect` are how someone else confirms your finding without taking your
word for it — a bug report without a reproduction is not a bug report. Write them for
EVERY area, not just the ones that deviate.

  - Start from `launch` (or `relaunch`) so the steps do not depend on where you
    happened to be.
  - Refer to elements by their visible TEXT, never by coordinates — coordinates mean
    nothing on a fresh run.
  - When a control has NO readable text (an icon button, a FAB, a switch), use its
    RESOURCE ID instead. It is in every observation: the `identifier` field, or
    `resource-id` in a `uiautomator dump`. Use the part after the `/`:

        tap: "fab_new_list"        # correct — the button has no label
        tap: "CREATE A LIST"       # WRONG — that is a caption BESIDE the button

    Name the control you actually pressed, not the nearest words on screen.
  - Each area's steps must be SELF-CONTAINED. Before they run, the app's data is
    restored to EXACTLY what it was when you started — content that already existed
    then (including app-generated sample content, even with a random-looking name)
    WILL be there, and you may refer to it by its exact visible text. Anything YOU
    created while testing (this area or another) will NOT be there: if your steps
    refer to a note, task or list you made, the same steps must CREATE it first.
  - Actions: launch, relaunch, wait, tap: "<text>", long_press: "<text>",
    type: "<text>", press: back|home|enter, swipe: up|down|left|right
  - `expect` is `present: "<text>"` or `absent: "<text>"` — what SHOULD be true if the
    area works. For an area you found deviating, this is the check that fails.
  - QUOTE the `expected` and `actual` values. They are prose, and an unquoted value
    containing `: ` is read as YAML nesting, which invalidates the entry:
      actual: "tapped \"Update event\": the list still shows \"Alice\""   <- correct
      actual: tapped "Update event": the list still shows "Alice"         <- breaks

Use `blocked` when you genuinely could not exercise an area — for example because
something it depends on did not work. Do not guess `as_specified` or `deviates` for an
area you could not actually test.

You may also emit the same verdicts as lines in your messages, in this exact format:

  AREA: <area> | VERDICT: as_specified|deviates|blocked | EXPECTED: <what the spec says> | ACTUAL: <what you observed>

Report every area in the list."""
