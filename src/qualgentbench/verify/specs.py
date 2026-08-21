"""Per-task verification specs + the runner. After the agent stops: relaunch, tap
through `nav`, dump the VH, assert required nodes present and excluded ones absent.
Pinned artifact values MUST match the task instruction — keep the two in lockstep."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .device import (
    current_activity,
    disable_animations,
    dump_vh,
    probe_root,
    relaunch,
    scroll_down,
    tap_node,
    wait_stable,
)
from .match import node_present, visible_texts


@dataclass
class Spec:
    case_id: str
    bundle_hint: str = ""                       # informational
    relaunch: bool = True                       # cold-launch to the landing screen first
    nav: list[dict] = field(default_factory=list)        # taps to reach the verify screen
    activity_contains: Optional[str] = None     # foreground activity must contain this
    require: list[dict] = field(default_factory=list)    # node-matchers that must be present
    exclude: list[dict] = field(default_factory=list)    # node-matchers that must be absent


@dataclass
class VerifyResult:
    passed: bool
    evidence: str


async def run_spec(spec: Spec, serial: str, bundle: str) -> VerifyResult:
    # Zero animation scales so uiautomator dump reaches idle (the empty-dump fix).
    await disable_animations(serial)
    if spec.relaunch:
        await relaunch(serial, bundle)
    for nav_matcher in spec.nav:
        await tap_node(serial, nav_matcher)

    await wait_stable(serial)
    xml = await dump_vh(serial)
    if not xml:
        return VerifyResult(False, "could not read UI hierarchy")

    # The required nodes are the real check; the brittle activity name is only a
    # failure diagnostic, never a gate. A require entry is a matcher dict OR a list
    # of matcher dicts meaning "any of these" (value-format tolerance).
    def _present(dumps: list[str], entry) -> bool:
        matchers = entry if isinstance(entry, list) else [entry]
        return any(node_present(d, m) for m in matchers for d in dumps)

    def _missing(dumps: list[str]) -> list:
        return [e for e in spec.require if not _present(dumps, e)]

    dumps = [xml]
    missing = _missing(dumps)
    if missing:
        # the artifact may be below the fold — scroll once and re-check both dumps
        await scroll_down(serial)
        await wait_stable(serial)
        dumps.append(await dump_vh(serial))
        missing = _missing(dumps)

    leaked = [m for m in spec.exclude if any(node_present(d, m) for d in dumps)]
    if not missing and not leaked:
        result = VerifyResult(True, f"{len(spec.require)} required node(s) present")
    else:
        act = await current_activity(serial)
        seen: list = []
        for d in dumps:
            for v in visible_texts(d):
                if v not in seen:
                    seen.append(v)
        parts = []
        if missing:
            parts.append(f"missing {missing}")
        if leaked:
            parts.append(f"excluded present {leaked}")
        parts.append(f"foreground={act or '?'}")
        parts.append(f"screen={seen[:30]}")
        result = VerifyResult(False, "; ".join(parts))

    # Append root-capability signals to every verdict (worker-side adb, which
    # works even though a local tunnel times out) so we can decide UI-only vs
    # storage-based verification. Last, since `adb root` can bounce the tunnel.
    try:
        result.evidence += " | " + await probe_root(serial)
    except Exception:  # noqa: BLE001
        pass
    return result


# ── The 5 Easy reference specs ────────────────────────────────────────────────
# case_id -> Spec. Artifact values are pinned in the matching test-case instruction.
_SPECS: dict[str, Spec] = {
    # Loop – Add new habit → habit appears on the home habit-list.
    "ce492244-6b7b-4b2f-bb53-7ee67ebaee9e": Spec(
        case_id="ce492244-6b7b-4b2f-bb53-7ee67ebaee9e",
        bundle_hint="org.isoron.uhabits",
        require=[{"any_text": "QA-Habit-01"}],
    ),
    # Markor – Create new Markdown note → note file appears in the notebook list.
    "02cca915-ea1d-459b-942c-5c3e1e101816": Spec(
        case_id="02cca915-ea1d-459b-942c-5c3e1e101816",
        bundle_hint="net.gsantner.markor",
        require=[{"any_text": "QA-Note-01"}],
    ),
    # Expense Tracker – Add recurring expense → expense appears on home with the
    # right amount (not just the name — verifies the agent set the value too). The
    # amount's rendered format is uncertain, so match either "1,000" or "1000".
    "06873828-b284-4c83-b7f3-620cb7364958": Spec(
        case_id="06873828-b284-4c83-b7f3-620cb7364958",
        bundle_hint="de.dbauer.expensetracker",
        # tap the Home tab first — the app's default tab is a persisted setting, so
        # after relaunch dl may land on Upcoming/Settings; the recurring list is Home.
        nav=[{"any_text": "Home"}],
        require=[
            {"any_text": "QA-Rent"},
            [{"any_text": "1,000"}, {"any_text": "1000"}],
        ],
    ),
    # My Brain – Create new task → tap into Tasks, task appears in the list.
    "9bfa39c6-a015-444c-86ce-05e7db064894": Spec(
        case_id="9bfa39c6-a015-444c-86ce-05e7db064894",
        bundle_hint="com.mhss.app.mybrain",
        nav=[{"any_text": "Tasks"}],
        require=[{"any_text": "QA-Task-01"}],
    ),
    # Markor – Create first ToDo list → tap the To-Do tab, entry appears.
    "426e4e4b-4101-4bff-ac42-8c42f5aae4d1": Spec(
        case_id="426e4e4b-4101-4bff-ac42-8c42f5aae4d1",
        bundle_hint="net.gsantner.markor",
        nav=[{"any_text": "To-Do"}],
        require=[{"any_text": "QA-Todo-01"}],
    ),
}


def get_spec(case_id: str) -> Optional[Spec]:
    return _SPECS.get(str(case_id))
