"""CreateBench v2: the creation brief an author is given (QUA-2856).

A creation episode's instruction has four parts, three of them ours and public:

1. the rendered creator template — PRIVATE, read at run time from the arm's DevLoop
   pin and delivered as developer instructions, never as the prompt (`create/arm.py`);
2. the harness note `arm.SURFACE_NOTE` — the device and server note, the no-source note
   and the fixed headless-approval shim (the device is reserved and approval is granted
   in advance, because `codex exec` is single-turn and nobody answers);
3. the NEUTRAL feature brief of the case (`brief: {title, intended_behavior}` in
   `data/test-cases/<app>.yaml`, QUA-2853, gated by `scripts/lint_create_briefs.py`);
4. one fixed request sentence (`ASK`).

`render_brief` joins 3 and 4; `arm.creation_prompt` wraps them in 2. Both texts are part
of the treatment, so they are versioned together like the QA brief (`brief.BRIEF_VERSION`):
`CREATE_BRIEF_VERSION` is stamped into every creation episode's provenance and into
plan.json's environment, and a resume across a change is refused.
`tests/test_create_runner.py` pins the sha256 of both texts to the version, so an edit
that forgets the bump fails the suite.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .arm import SURFACE_NOTE

# Bump when the harness note or the brief rendering below changes what the author is
# told — episodes on either side of the change are not directly comparable.
#
#   1  (QUA-2856) the QUA-2852 harness note (device reserved, no source, approval
#      granted in advance) around `Feature: <title>`, the intended behaviour, and one
#      request for a single test case written the way a user would use the feature.
#      The spike's (QUA-2851) wording, which made the author submit in the same turn.
CREATE_BRIEF_VERSION = 1

#: The request, after the feature. Our own words; names no procedure and no defect.
ASK = ("Write one QualGent test case that checks this feature the way a user would "
       "use it.")

_BRIEF_FORMAT = "Feature: {title}\n{intended_behavior}\n\n{ask}"


class BriefError(ValueError):
    """A case whose `brief:` block cannot be rendered."""


def case_brief(case: dict[str, Any]) -> dict[str, str] | None:
    """The case's `{title, intended_behavior}` or None when it carries no usable one."""
    b = case.get("brief")
    if not isinstance(b, dict):
        return None
    title, behaviour = b.get("title"), b.get("intended_behavior")
    if not (isinstance(title, str) and title.strip()
            and isinstance(behaviour, str) and behaviour.strip()):
        return None
    return {"title": " ".join(title.split()), "intended_behavior": " ".join(behaviour.split())}


def render_brief(brief: dict[str, str]) -> str:
    """The feature brief the author reads, inside the harness note."""
    if not brief or not brief.get("title") or not brief.get("intended_behavior"):
        raise BriefError("a creation brief needs a title and an intended behaviour")
    return _BRIEF_FORMAT.format(title=brief["title"],
                                intended_behavior=brief["intended_behavior"], ask=ASK)


def text_sha256() -> str:
    """One hash over every public text the author is given around the brief: the
    harness note and the rendering. `tests/test_create_runner.py` ties it to
    `CREATE_BRIEF_VERSION`."""
    return hashlib.sha256((SURFACE_NOTE + "\0" + _BRIEF_FORMAT + "\0" + ASK).encode()).hexdigest()
