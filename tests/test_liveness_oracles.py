"""The liveness oracles — `crash:` / `anr:` gates, the `stuck:` probe — and the
attribution canary. Device-free: every adb-shaped call is stubbed."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from qualgentbench import journey, replay as rp, submission
from qualgentbench.adb_meter import AdbMeter, deny_reason
from qualgentbench.submission import Expectation, Step, _parse_expect
from qualgentbench.verify.canary import parse_fired
from qualgentbench.verify.crash import CrashRecord

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import derive_journey as dj  # noqa: E402

SCREEN = ('<hierarchy><node text="Medicine" clickable="true" bounds="[0,0][100,50]"/>'
          '<node text="Overview" clickable="true" bounds="[0,60][100,110]"/></hierarchy>')
EMPTY = "<hierarchy/>"
ANR_MSG = ("Input dispatching timed out (5c6ed55 com.x/com.x.MainActivity is not "
           "responding. Waited 5001ms for MotionEvent).")


def _java_crash(exc="java.lang.IllegalStateException", frame="com.x.Repo.save(Repo.java)"):
    return rp.ReplayResult(rp.CRASHED, "step 2: com.x crashed — boom", 1,
                           crash={"process": "com.x", "kind": "java", "exception": exc,
                                  "message": "seeded", "signature": f"{exc}@{frame}"})


def _anr_result():
    return rp.ReplayResult(rp.CRASHED, "step 2: com.x stopped responding (ANR)", 1,
                           crash={"process": "com.x", "kind": "anr", "exception": "ANR",
                                  "message": ANR_MSG,
                                  "signature": "ANR@Input dispatching timed out (com.x/com.x.MainActivity)"})


# ── parsing ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    {"crash": True}, {"crash": "IllegalState"}, {"anr": True}, {"stuck": "Medicine"},
    {"present": "Saved", "crash": True}, {"db": "n.db", "query": "select 1", "equals": "1", "stuck": "Save"},
])
def test_an_agent_submission_cannot_use_a_liveness_mode(raw):
    """A liveness claim steers what a crash on the seeded build MEANS; only the spec
    author may write one. The rejection wording names them so the error is diagnosable."""
    expect, err = _parse_expect(raw, "area")
    assert expect is None and "harness-only" in err and "crash/anr/stuck" in err


def test_the_trusted_path_parses_standalone_and_riding_forms():
    e, err = _parse_expect({"crash": True}, "a", trusted=True)
    assert err is None and e.mode == "crash" and e.crash is True and e.gates == {"crash": True}
    e, _ = _parse_expect({"anr": "Input dispatching"}, "a", trusted=True)
    assert e.mode == "anr" and e.anr == "Input dispatching"
    e, _ = _parse_expect({"stuck": " Save "}, "a", trusted=True)
    assert e.mode == "stuck" and e.stuck == "Save"
    e, _ = _parse_expect({"db": "n.db", "query": "select 1", "equals": 1, "crash": "ISE"}, "a", trusted=True)
    assert e.mode == "db" and e.equals == "1" and e.crash == "ISE"
    e, _ = _parse_expect({"present": "Saved", "stuck": "Save", "anr": True}, "a", trusted=True)
    assert e.mode == "present" and e.text == "Saved" and e.gates == {"anr": True, "stuck": "Save"}


@pytest.mark.parametrize("raw,msg", [
    ({"crash": False}, "`crash` must be true"),
    ({"crash": ""}, "`crash` must be true"),
    ({"anr": 3}, "`anr` must be true"),
    ({"stuck": True}, "`stuck` needs the anchor"),
    ({"stuck": ""}, "`stuck` needs the anchor"),
])
def test_malformed_liveness_values_are_errors(raw, msg):
    expect, err = _parse_expect(raw, "a", trusted=True)
    assert expect is None and msg in err


def test_liveness_expectations_round_trip_through_as_dict():
    for e in (Expectation("crash", crash=True), Expectation("anr", anr="Input"),
              Expectation("stuck", stuck="Save"),
              Expectation("db", db="n.db", query="q", equals="1", crash="ISE", stuck="Save"),
              Expectation("present", text="x", anr=True)):
        assert Expectation(**e.as_dict()).as_dict() == e.as_dict()
    assert Expectation("crash", crash=True).as_dict() == {"mode": "crash", "crash": True}
    # A plain expectation gains no gate keys — old artifacts keep parsing byte-identical.
    assert Expectation("present", text="x").as_dict() == {"mode": "present", "text": "x"}


# ── gate_crash: what a CRASHED pass means ─────────────────────────────────────

def test_a_non_crash_is_never_touched_by_the_gate():
    for outcome in (rp.HOLDS, rp.VIOLATED, rp.INCONCLUSIVE):
        r = rp.ReplayResult(outcome, "x", 2)
        assert rp.gate_crash(r, Expectation("crash", crash="anything"), ["bug"], ["bug"]) is r


def test_crash_true_accepts_any_death_of_the_app():
    e = Expectation("crash", crash=True)
    assert rp.gate_crash(_java_crash(), e).outcome == rp.CRASHED
    assert rp.gate_crash(_anr_result(), e).outcome == rp.CRASHED


def test_a_different_crash_is_inconclusive_never_violated():
    """The seeded fault is not what fired: nothing demonstrated, nothing disproved."""
    e = Expectation("db", db="n", query="q", equals="1", crash="NullPointerException")
    got = rp.gate_crash(_java_crash(), e, fired=[], seeded=["bug"])
    assert got.outcome == rp.INCONCLUSIVE
    assert got.detail.endswith("crashed, but not the expected crash: "
                               "java.lang.IllegalStateException@com.x.Repo.save(Repo.java)")
    assert got.crash == _java_crash().crash and got.fired == []


def test_the_signature_gate_is_a_case_insensitive_substring_of_signature_or_exception():
    assert rp.gate_crash(_java_crash(), Expectation("crash", crash="illegalstate")).outcome == rp.CRASHED
    assert rp.gate_crash(_java_crash(), Expectation("crash", crash="Repo.save")).outcome == rp.CRASHED
    assert rp.gate_crash(_java_crash(), Expectation("crash", crash="Repo.load")).outcome == rp.INCONCLUSIVE


def test_anr_demands_a_hang_not_a_crash():
    got = rp.gate_crash(_java_crash(), Expectation("anr", anr=True))
    assert got.outcome == rp.INCONCLUSIVE and "an ANR was expected" in got.detail
    assert rp.gate_crash(_anr_result(), Expectation("anr", anr=True)).outcome == rp.CRASHED
    assert rp.gate_crash(_anr_result(), Expectation("anr", anr="input dispatching")).outcome == rp.CRASHED
    got = rp.gate_crash(_anr_result(), Expectation("anr", anr="Broadcast of Intent"))
    assert got.outcome == rp.INCONCLUSIVE and "not the expected ANR" in got.detail


def test_a_fired_marker_for_the_seeded_bug_is_identity_by_construction():
    got = rp.gate_crash(_java_crash(), Expectation("crash", crash="IllegalState"),
                        fired=["crash-on-save"], seeded=["crash-on-save", "disp"])
    assert got.outcome == rp.CRASHED
    assert got.detail.endswith("— fired: crash-on-save") and got.fired == ["crash-on-save"]


def test_marker_and_signature_disagreement_is_inconclusive_with_both_facts():
    got = rp.gate_crash(_java_crash(), Expectation("crash", crash="NullPointer"),
                        fired=["crash-on-save"], seeded=["crash-on-save"])
    assert got.outcome == rp.INCONCLUSIVE
    assert "fired marker ['crash-on-save'] says the seeded path ran, yet crashed, but not the expected crash" in got.detail


def test_a_marker_for_an_unseeded_fault_means_the_flag_gate_failed():
    got = rp.gate_crash(_java_crash(), Expectation("crash", crash=True),
                        fired=["other-bug"], seeded=["crash-on-save"])
    assert got.outcome == rp.INCONCLUSIVE and "flag gate did not hold" in got.detail
    # ...and on a CLEAN pass any marker at all is that failure.
    got = rp.gate_crash(_java_crash(), Expectation("crash", crash=True), fired=["crash-on-save"], seeded=[])
    assert got.outcome == rp.INCONCLUSIVE and "clean pass" in got.detail


def test_no_markers_leaves_the_signature_as_the_only_signal():
    got = rp.gate_crash(_java_crash(), Expectation("crash", crash="IllegalState"), fired=None, seeded=["b"])
    assert got.outcome == rp.CRASHED and got.fired is None


# ── replay(): the stuck probe with the device stubbed ─────────────────────────

class _Proc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.killed = 0

    def kill(self):
        self.killed += 1
        self.returncode = -9


@pytest.fixture
def _probe(monkeypatch):
    """Stubs for everything `_check_stuck` touches; returns the mutable state."""
    state = {"screen": SCREEN, "proc": _Proc(returncode=0), "taps": [], "hung": [],
             "anrs": [], "timeout": 5000, "since": "09-14 12:00:00.000"}

    async def _dump(serial, retries=3):
        return state["screen"]

    async def _tap(serial, centre):
        state["taps"].append(centre)
        return state["proc"]

    async def _hung(serial, package=""):
        return list(state["hung"])

    async def _anrs(serial, package, since):
        return list(state["anrs"])

    async def _timeout(serial):
        return state["timeout"]

    async def _clock(serial):
        return state["since"]

    async def _steps(serial, bundle, steps, choices=None):
        return rp.ReplayResult(rp.HOLDS, "", len(steps))

    async def _no_fired(serial, bundle):
        return []
    monkeypatch.setattr(rp, "dump_vh", _dump)
    monkeypatch.setattr(rp, "_probe_tap", _tap)
    monkeypatch.setattr(rp, "unresponsive_windows", _hung)
    monkeypatch.setattr(rp, "anrs_since", _anrs)
    monkeypatch.setattr(rp, "anr_timeout_ms", _timeout)
    monkeypatch.setattr(rp, "device_time", _clock)
    monkeypatch.setattr(rp, "run_steps", _steps)
    monkeypatch.setattr(rp, "fired_markers", _no_fired)
    monkeypatch.setattr(rp, "_STUCK_MARGIN_MS", 0)
    monkeypatch.setattr(rp, "_STUCK_POLL_S", 0)
    rp._LAST_VH.pop("serial", None)
    return state


@pytest.mark.asyncio
async def test_a_screen_that_answers_the_probe_HOLDS(_probe):
    e = Expectation("stuck", stuck="Medicine")
    got = await rp.replay("serial", "com.x", [Step("launch"), Step("tap", "Medicine")], e)
    assert got.outcome == rp.HOLDS and got.detail.startswith("stuck probe: 'Medicine' answered in")
    assert _probe["taps"] == [(50, 25)], "exactly one probe tap, on the anchor"
    assert got.crash is None


@pytest.mark.asyncio
async def test_a_hung_screen_is_CRASHED_as_an_ANR(_probe):
    _probe["proc"] = _Proc(returncode=None)                     # the tap never returns
    _probe["hung"] = [("5c6ed55 com.x/com.x.MainActivity", 2)]
    _probe["anrs"] = [CrashRecord("com.x", 7, "09-14 12:00:06.000", "anr", "ANR", ANR_MSG)]
    e = Expectation("stuck", stuck="Medicine")
    got = await rp.replay("serial", "com.x", [Step("launch"), Step("tap", "Medicine")], e)
    assert got.outcome == rp.CRASHED
    assert got.crash["kind"] == "anr"
    assert got.crash["signature"] == "ANR@Input dispatching timed out (com.x/com.x.MainActivity)"
    assert got.crash["probe"] == {"anchor": "Medicine", "source": "screen",
                                  "unresponsive": [("5c6ed55 com.x/com.x.MainActivity", 2)],
                                  "timeout_ms": 5000}
    assert "stopped responding (ANR) — stuck probe on 'Medicine' unanswered" in got.detail
    assert len(_probe["taps"]) == 1 and _probe["proc"].killed == 1, \
        "one input, and the blocked client is killed rather than re-sent"


@pytest.mark.asyncio
async def test_the_dispatcher_alone_is_enough_when_the_anr_log_has_not_landed(_probe):
    _probe["proc"] = _Proc(returncode=None)
    _probe["hung"] = [("5c6ed55 com.x/com.x.MainActivity", 1)]
    got = await rp.replay("serial", "com.x", [Step("launch")], Expectation("stuck", stuck="Medicine"))
    assert got.outcome == rp.CRASHED and got.crash["kind"] == "anr"
    assert got.crash["signature"] == "ANR@Input dispatching timed out (com.x/com.x.MainActivity)"


@pytest.mark.asyncio
async def test_a_frozen_hierarchy_falls_back_to_the_routes_last_readable_screen(_probe):
    """A hung app's hierarchy cannot be dumped (11 s, 39 bytes on the emulator); the
    anchor is taken from the last screen the route's own lookups read."""
    _probe["screen"] = ""
    rp._LAST_VH["serial"] = SCREEN
    _probe["proc"] = _Proc(returncode=None)
    _probe["hung"] = [("5c6ed55 com.x/com.x.MainActivity", 1)]
    got = await rp.replay("serial", "com.x", [Step("launch")], Expectation("stuck", stuck="Overview"))
    assert got.outcome == rp.CRASHED
    assert got.crash["probe"]["source"] == "the route's last readable screen"
    assert _probe["taps"] == [(50, 85)]


@pytest.mark.asyncio
async def test_an_anchor_found_nowhere_is_INCONCLUSIVE_and_sends_nothing(_probe):
    _probe["screen"] = EMPTY
    got = await rp.replay("serial", "com.x", [Step("launch")], Expectation("stuck", stuck="Nope"))
    assert got.outcome == rp.INCONCLUSIVE and "no element matching 'Nope'" in got.detail
    assert _probe["taps"] == []


@pytest.mark.asyncio
async def test_a_tap_that_never_returns_while_the_dispatcher_stays_quiet_is_not_a_hang(_probe):
    _probe["proc"] = _Proc(returncode=None)
    got = await rp.replay("serial", "com.x", [Step("launch")], Expectation("stuck", stuck="Medicine"))
    assert got.outcome == rp.INCONCLUSIVE and "dispatcher still responsive" in got.detail


@pytest.mark.asyncio
async def test_the_probe_runs_before_the_db_read_and_a_live_app_proceeds_to_it(_probe, monkeypatch):
    order: list[str] = []

    async def _db(serial, bundle, expect, ran):
        order.append("db")
        return rp.ReplayResult(rp.VIOLATED, "db → 0", ran)
    monkeypatch.setattr(rp, "_check_db", _db)
    real_tap = rp._probe_tap

    async def _tap(serial, centre):
        order.append("probe")
        return await real_tap(serial, centre)
    monkeypatch.setattr(rp, "_probe_tap", _tap)
    e = Expectation("db", db="n", query="q", equals="1", stuck="Medicine")
    got = await rp.replay("serial", "com.x", [Step("launch")], e)
    assert order == ["probe", "db"] and got.outcome == rp.VIOLATED


@pytest.mark.asyncio
async def test_a_standalone_crash_or_anr_expectation_HOLDS_on_a_live_route(_probe):
    for e in (Expectation("crash", crash=True), Expectation("anr", anr=True)):
        got = await rp.replay("serial", "com.x", [Step("launch")], e)
        assert got.outcome == rp.HOLDS and got.detail == "the app is alive after the route"
    assert _probe["taps"] == [], "no probe without stuck:"


@pytest.mark.asyncio
async def test_replay_reads_the_markers_and_gates_a_crash(monkeypatch):
    reads: list[str] = []

    async def _steps(serial, bundle, steps, choices=None):
        return _java_crash()

    async def _fired(serial, bundle):
        reads.append(bundle)
        return ["crash-on-save"]

    async def _clock(serial):
        return "09-14 12:00:00.000"
    monkeypatch.setattr(rp, "run_steps", _steps)
    monkeypatch.setattr(rp, "fired_markers", _fired)
    monkeypatch.setattr(rp, "device_time", _clock)

    good = Expectation("db", db="n", query="q", equals="1", crash="IllegalState")
    got = await rp.replay("serial", "com.x", [Step("launch")], good, seeded=["crash-on-save"])
    assert got.outcome == rp.CRASHED and got.fired == ["crash-on-save"] and reads == ["com.x"]
    assert got.as_dict()["fired"] == ["crash-on-save"]

    other = Expectation("db", db="n", query="q", equals="1", crash="NullPointer")
    got = await rp.replay("serial", "com.x", [Step("launch")], other, seeded=["crash-on-save"])
    assert got.outcome == rp.INCONCLUSIVE and "yet crashed, but not the expected crash" in got.detail

    # No gate at all: CRASHED stands, markers still recorded for the artifact.
    plain = Expectation("present", text="Saved")
    got = await rp.replay("serial", "com.x", [Step("launch")], plain, seeded=["crash-on-save"])
    assert got.outcome == rp.CRASHED and got.fired == ["crash-on-save"]


@pytest.mark.asyncio
async def test_pass_hands_the_live_flags_to_the_gate(monkeypatch):
    seen: list = []

    async def _reset(*a, **k):
        return True

    async def _replay(serial, bundle, steps, expect, choices=None, seeded=()):
        seen.append(list(seeded))
        return rp.ReplayResult(rp.HOLDS, "", 1)
    monkeypatch.setattr(rp, "_reset", _reset)
    monkeypatch.setattr(rp, "replay", _replay)
    claim = submission.Claim("a", "deviates", steps=[Step("launch")], expect=Expectation("crash", crash=True))
    await rp._pass("serial", "com.x", claim, ["bug-1", "bug-2"], None)
    assert seen == [["bug-1", "bug-2"]]


def test_set_flags_clears_the_markers_in_the_same_command(monkeypatch):
    sent: list[str] = []

    async def _adb(serial, *args):
        sent.append(" ".join(args))
        return 0, b""
    monkeypatch.setattr(rp, "_adb", _adb)
    asyncio.run(rp.set_flags("serial", "com.x", ["b"]))
    assert len(sent) == 1 and "rm -rf files/.qgb/fired; mkdir -p files" in sent[0]


# ── the canary ────────────────────────────────────────────────────────────────

def test_fired_markers_parse_a_listing_and_never_an_error():
    assert parse_fired("crash-on-save\nstock_bug.v2\n") == ["crash-on-save", "stock_bug.v2"]
    assert parse_fired("ls: files/.qgb/fired: No such file or directory") == []
    assert parse_fired("run-as: package not debuggable: com.x") == []
    assert parse_fired("") == [] and parse_fired("  \n") == []
    assert parse_fired("b\nb\na") == ["a", "b"]


def test_the_generated_shims_carry_a_fired_call():
    import build_app
    for tmpl in (build_app._FLAG_SHIM, build_app._FLAG_SHIM_JAVA):
        text = tmpl.format(pkg="com.x", app_id="com.x")
        assert "fired(" in text and '/data/data/com.x/files/.qgb/fired' in text
        assert "createNewFile()" in text


# ── the leak: the agent's adb is denied the sandbox ───────────────────────────

@pytest.mark.parametrize("request_,why", [
    ("shell:run-as com.x cat files/qgb_flags.txt", "run-as"),
    ("shell,v2,TERM=xterm,raw:run-as com.x sh -c 'ls files/.qgb/fired'", "run-as"),
    ("exec:run-as com.x id", "run-as"),
    ("shell:cat /data/data/com.x/files/qgb_flags.txt", "app sandbox"),
    ("shell:ls /data/user/0/com.x/files/.qgb/fired", "app sandbox"),
    ("shell:ls /data/user_de/0/com.x/", "app sandbox"),
    ("shell:cat /data/local/tmp/qgb_flags.txt", "harness scratch"),
    ("shell:find / -name qgb_flags.txt", "harness files"),
    ("shell:find / -path '*/.qgb/*'", "harness files"),
    ("backup:apk com.x", "backup"),
])
def test_answer_key_paths_are_denied(request_, why):
    assert deny_reason(request_) == why


@pytest.mark.parametrize("request_", [
    "shell:input tap 1 1", "shell:uiautomator dump /sdcard/qgb_vh.xml", "shell:cat /sdcard/qgb_vh.xml",
    "shell:dumpsys activity exit-info com.x", "shell:logcat -d -b crash", "shell:pm path com.x",
    "shell:am start -n com.x/.Main", "shell:ls /data/local/tmp", "host:tport:serial:x", "sync:",
    "shell:run_as_helper --help", "shell:ls /sdcard/Android/data/com.x/files",
])
def test_ordinary_qa_requests_are_not_denied(request_):
    assert deny_reason(request_) is None


class _Upstream:
    def __init__(self):
        self.requests: list[str] = []

    async def start(self):
        self._srv = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._srv.sockets[0].getsockname()[1]

    async def stop(self):
        self._srv.close()
        await self._srv.wait_closed()

    async def _handle(self, r, w):
        try:
            while True:
                prefix = await r.readexactly(4)
                req = (await r.readexactly(int(prefix.decode(), 16))).decode()
                self.requests.append(req)
                w.write(b"OKAY")
                if req.startswith("host:tport"):
                    w.write((1).to_bytes(8, "little"))
                    await w.drain()
                    continue
                await w.drain()
                break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()


@pytest.mark.asyncio
async def test_a_denied_request_gets_FAIL_at_the_socket_and_never_reaches_the_server(tmp_path):
    up = _Upstream()
    meter = AdbMeter(tmp_path / "c.json", upstream_port=await up.start())
    port = await meter.start()
    r, w = await asyncio.open_connection("127.0.0.1", port)
    for req in ("host:tport:serial:emulator-5554", "shell:run-as com.x cat files/qgb_flags.txt"):
        w.write(f"{len(req):04x}{req}".encode())
        await w.drain()
        status = await r.readexactly(4)
        if req.startswith("host:tport"):
            assert status == b"OKAY"
            await r.readexactly(8)
    assert status == b"FAIL"
    n = int((await r.readexactly(4)).decode(), 16)
    assert (await r.readexactly(n)).decode() == "qualgentbench: run-as is not available to the agent"
    assert await r.read() == b""                       # the connection is closed
    w.close()
    await asyncio.sleep(0.05)
    assert up.requests == ["host:tport:serial:emulator-5554"], "the sandbox read never left the meter"
    assert meter.counts.denied == 1 and meter.counts.total == 0, "denied, and not charged as a step"
    assert meter.counts.as_metrics()["metered_denied"] == 1
    assert any(k.startswith("denied:run-as") for k in meter.counts.services)
    await meter.stop()
    await up.stop()


# ── derive_journey: the corpus gate ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluate_probes_first_then_reads_state(monkeypatch):
    order: list[str] = []

    async def _probe(serial, bundle, expect, ran, since):
        order.append("probe")
        return rp.ReplayResult(rp.HOLDS, "answered", ran)

    async def _adb(*a, **k):
        order.append("force-stop")
        return 0, b""

    async def _db(serial, bundle, expect, ran):
        order.append("db")
        return rp.ReplayResult(rp.HOLDS, "db", ran)
    monkeypatch.setattr(rp, "_check_stuck", _probe)
    monkeypatch.setattr(rp, "_adb", _adb)
    monkeypatch.setattr(rp, "_check_db", _db)
    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(dj.asyncio, "sleep", _no_sleep)
    e = Expectation("db", db="n", query="q", equals="1", stuck="Save")
    got = await dj.evaluate("serial", "com.x", e, 3, since="09-14 12:00:00.000")
    assert got.outcome == rp.HOLDS and order == ["probe", "force-stop", "db"]
    got = await dj.evaluate("serial", "com.x", Expectation("crash", crash=True), 3, since="x")
    assert got.outcome == rp.HOLDS and got.detail == "the app is alive after the route"


@pytest.mark.asyncio
async def test_evaluate_returns_a_hung_probe_without_touching_the_state(monkeypatch):
    async def _probe(serial, bundle, expect, ran, since):
        return rp.ReplayResult(rp.CRASHED, "hung", ran, crash={"kind": "anr"})

    async def boom(*a, **k):
        raise AssertionError("no state read on a hung app")
    monkeypatch.setattr(rp, "_check_stuck", _probe)
    monkeypatch.setattr(rp, "_check_db", boom)
    monkeypatch.setattr(rp, "_adb", boom)
    e = Expectation("db", db="n", query="q", equals="1", stuck="Save")
    got = await dj.evaluate("serial", "com.x", e, 3, since="x")
    assert got.outcome == rp.CRASHED


def _design(death=None):
    return {"bugs": ["crash-on-save"], "blocking": "crash-on-save", "side": [],
            "expected": "FAIL", "death": death}


def _trials(clean, seeded):
    # A seeded arm that DIES leaves a screen the clean arm never showed — here the
    # launcher behind a root-activity crash. A death that leaves every screen identical
    # is refused on its own (QUA-2742, tests/test_derive_invisible_death.py), so a
    # fixture giving both arms the same screens would be testing that instead.
    after = [["Home"], ["At a glance", "Chrome"]] if seeded.outcome == rp.CRASHED else [["Home"], ["Saved"]]
    return {"clean": [(clean, [["Home"], ["Saved"]])], "seeded": [(seeded, after)]}


def test_a_case_that_names_a_death_needs_the_seeded_arm_to_die_that_way():
    row = dj.judge_case(_design("crash"), _trials(rp.ReplayResult(rp.HOLDS, "", 2),
                                                 rp.ReplayResult(rp.VIOLATED, "db → 0", 2)))
    assert not row["agrees"]
    assert row["problems"] == ["check expects the seeded version to fail by crash, but it failed with "
                               "the app alive (violated: db → 0)"]
    crashed = _java_crash()
    crashed.fired = ["crash-on-save"]
    row = dj.judge_case(_design("crash"), _trials(rp.ReplayResult(rp.HOLDS, "", 2), crashed))
    assert row["agrees"] and row["measured"] == "FAIL"


def test_a_different_crash_makes_the_seeded_arm_undecidable_with_the_reason():
    incon = rp.ReplayResult(rp.INCONCLUSIVE, "step 2: com.x crashed — crashed, but not the expected crash: NPE@x", 1)
    row = dj.judge_case(_design("crash"), _trials(rp.ReplayResult(rp.HOLDS, "", 2), incon))
    assert not row["agrees"] and row["measured"] == "undecidable"
    assert "measured undecidable: step 2: com.x crashed — crashed, but not the expected crash" in row["problems"][0]


def test_markers_on_the_wrong_arm_are_corpus_problems():
    clean = rp.ReplayResult(rp.HOLDS, "", 2)
    clean.fired = ["crash-on-save"]
    row = dj.judge_case(_design("crash"), _trials(clean, _java_crash()))
    assert any("fired on the CLEAN version" in p for p in row["problems"])
    seeded = _java_crash()
    seeded.fired = ["some-other-bug"]
    row = dj.judge_case(_design("crash"), _trials(rp.ReplayResult(rp.HOLDS, "", 2), seeded))
    assert any("but not the blocking bug's (crash-on-save)" in p for p in row["problems"])


# ── journey: oracle shape and the case design ─────────────────────────────────

DEFECTS = {"crash-on-save": {"kind": "functional", "tier": "L4", "marker": "", "symptoms": []},
           "disp": {"kind": "display", "tier": "L2", "marker": "OOPS", "symptoms": []}}


def test_case_design_records_the_expected_death_only_when_blocking():
    case = {"id": "c", "bugs": ["crash-on-save"],
            "check": {"steps": ["launch"], "expect": {"db": "n", "query": "q", "equals": "1", "crash": "ISE"}}}
    assert journey.case_design(case, DEFECTS)["death"] == "crash"
    assert journey.case_design({**case, "check": {"expect": {"stuck": "Save"}}}, DEFECTS)["death"] == "stuck"
    assert journey.case_design({**case, "check": {"expect": {"anr": True}}}, DEFECTS)["death"] == "anr"
    assert journey.case_design({**case, "check": {"expect": {"present": "x"}}}, DEFECTS)["death"] is None
    # A display-only case is expected to PASS; naming a death there means nothing.
    assert journey.case_design({**case, "bugs": ["disp"]}, DEFECTS)["death"] is None


def test_oracle_carries_the_gate_and_standalone_liveness_modes():
    o = journey._oracle({"check": {"expect": {"db": "n", "query": "q", "equals": "1", "crash": True}}})
    assert o["mode"] == "db" and o["gate"] == {"crash": True}
    o = journey._oracle({"check": {"expect": {"stuck": "Save"}}})
    assert o["mode"] == "stuck" and o["gate"] == {"stuck": "Save"} and o["evidence"] == []
    o = journey._oracle({"check": {"expect": {"anr": "Input"}}})
    assert o["mode"] == "anr"
    assert "gate" not in journey._oracle({"check": {"expect": {"present": "x"}}})


def test_the_verdict_reads_a_liveness_oracle_like_a_db_one():
    spec = {"oracle": {"mode": "crash", "expect": {"crash": True}, "evidence": []}, "oracle_result": "holds"}
    assert journey._oracle_verdict(spec, [])[0] is True
    spec["oracle_result"], spec["oracle_detail"] = "violated", "crashed while the agent ran"
    ok, why = journey._oracle_verdict(spec, [])
    assert ok is False and "crash oracle violated" in why
    spec["oracle_result"] = "inconclusive"
    assert journey._oracle_verdict(spec, [])[0] is None


# ── episode_runner: the oracle after the agent exits ──────────────────────────

def _episode_spec(expect, crashes=(), fired=None, active=(), blocking=None):
    from qualgentbench.journey import _oracle
    spec = {"case_id": "c", "oracle": _oracle({"check": {"expect": expect}}),
            "app_crashes": list(crashes), "active_bugs": list(active), "blocking": blocking}
    if fired is not None:
        spec["fired"] = fired
    return spec


_ROW = {"process": "com.x", "kind": "java", "exception": "java.lang.IllegalStateException",
        "message": "seeded", "signature": "java.lang.IllegalStateException@com.x.Repo.save(Repo.java)",
        "classification": "app", "timestamp": "09-14 12:00:05.000"}


@pytest.mark.asyncio
async def test_the_runner_reads_the_crash_oracle_off_the_recorded_crashes(monkeypatch):
    from qualgentbench import episode_runner as er

    async def boom(*a, **k):
        raise AssertionError("no device query: the fact is already on the spec")
    monkeypatch.setattr(rp, "_check_stuck", boom)
    monkeypatch.setattr(rp, "_check_db", boom)

    spec = _episode_spec({"crash": True}, crashes=[_ROW])
    await er._journey_oracle("serial", "com.x", spec)
    assert spec["oracle_result"] == "violated" and "crashed while the agent ran" in spec["oracle_detail"]

    spec = _episode_spec({"crash": True})
    await er._journey_oracle("serial", "com.x", spec)
    assert spec["oracle_result"] == "holds"

    # A different crash than the gate names: inconclusive, never a verdict.
    spec = _episode_spec({"db": "n", "query": "q", "equals": "1", "crash": "NullPointer"}, crashes=[_ROW])
    await er._journey_oracle("serial", "com.x", spec)
    assert spec["oracle_result"] == "inconclusive" and "not the expected crash" in spec["oracle_detail"]

    # Foreign rows are not the app's death.
    spec = _episode_spec({"crash": True}, crashes=[{**_ROW, "classification": "foreign", "process": "com.ime"}])
    await er._journey_oracle("serial", "com.x", spec)
    assert spec["oracle_result"] == "holds"

    # A blocked (seeded, expected FAIL) version is judged on the verdict, not here.
    spec = _episode_spec({"crash": True}, crashes=[_ROW], blocking="crash-on-save")
    await er._journey_oracle("serial", "com.x", spec)
    assert "oracle_result" not in spec


@pytest.mark.asyncio
async def test_the_runner_probes_a_stuck_oracle_when_nothing_died(monkeypatch):
    from qualgentbench import episode_runner as er
    probes: list[str] = []

    async def _probe(serial, bundle, expect, ran, since):
        probes.append(expect.stuck)
        return rp.ReplayResult(rp.HOLDS, "answered in 40ms", 0)

    async def _clock(serial):
        return "09-14 12:00:00.000"
    monkeypatch.setattr(rp, "_check_stuck", _probe)
    monkeypatch.setattr(er, "crash_window", _clock)
    spec = _episode_spec({"stuck": "Save"})
    await er._journey_oracle("serial", "com.x", spec)
    assert spec["oracle_result"] == "holds" and probes == ["Save"]

    async def _hung(serial, bundle, expect, ran, since):
        return rp.ReplayResult(rp.CRASHED, "stuck", 0, crash={"kind": "anr"})
    monkeypatch.setattr(rp, "_check_stuck", _hung)
    spec = _episode_spec({"stuck": "Save"})
    await er._journey_oracle("serial", "com.x", spec)
    assert spec["oracle_result"] == "violated"


def test_the_flags_writer_wipes_the_markers_too(monkeypatch):
    from qualgentbench import episode_runner as er
    sent: list[str] = []

    async def _adb(*args):
        sent.append(" ".join(args))
        return 0, b""
    monkeypatch.setattr(er, "_adb", _adb)
    asyncio.run(er.write_bug_flags("serial", "com.x", {"active_bugs": ["b"]}))
    assert len(sent) == 1 and "rm -rf files/.qgb/fired; mkdir -p files" in sent[0]


# ── the corpus's two FREEZE exemplars (QUA-2711) ──────────────────────────────
# Until these landed, both detection paths had only ever met a process frozen by hand
# (`scripts/crash_probe.py --anr` / `--stuck`, which SIGSTOPs the app). These tests are
# device-free and read the real MedTimer corpus, so they pin the authored SHAPE — the
# live derivation is recorded in data/test-cases/medtimer.yaml.

_ANR_CASE = "medtimer-take-dose-then-medicine-list"
_STUCK_CASE = "medtimer-analysis-tabular-view"


def _medtimer_specs():
    from qualgentbench import bugs as _bugs
    suite = next(s for s in _bugs.load_apps() if s["app"]["id"] == "medtimer")
    return {(t.bug_spec["case_id"], t.bug_spec["version"]): t.bug_spec
            for t in journey.journey_tasks(suite)}


def test_the_corpus_carries_one_anr_case_and_one_stuck_case():
    """The pair is the point: the ANR case has PENDING INPUT, so Android raises the
    ANR itself and the route is the detector; the stuck case has none, so only the
    probe's one tap can reveal it. Their gates must therefore differ."""
    specs = _medtimer_specs()
    anr = specs[(_ANR_CASE, "seeded")]
    assert anr["oracle"]["mode"] == "db", "the clean arm still passes an ordinary state oracle"
    assert anr["oracle"]["gate"] == {"anr": True}
    assert anr["blocking"] == "overview-action-blocks-main-thread"

    stuck = specs[(_STUCK_CASE, "seeded")]
    # Standalone, not riding on a `present:`: only db/content/standalone gates are
    # evaluated by the episode runner (`_journey_oracle`), so a `present:` gate here
    # would be diagnostic only and the clean arm's probe would never run.
    assert stuck["oracle"]["mode"] == "stuck"
    assert stuck["oracle"]["gate"] == {"stuck": "Tabular view"}
    assert stuck["oracle"]["witness"] == ["Ibuprofen"], \
        "a route that writes nothing needs a screen witness for completion"
    assert stuck["blocking"] == "analysis-table-freezes-on-open"


def test_the_anr_case_answers_ibuprofens_reminder_whatever_the_hour():
    """QUA-2735. Every raised reminder's status icon reads "Reminded", and after 08:00
    Aspirin's is raised too and sorts first, so the bare anchor answered Aspirin and
    the clean arm failed its Ibuprofen oracle. The route's reminder tap is scoped to the
    row the agent-facing brief names — and nothing else about the case moved: the same
    oracle, the same gate, the same seeded bug."""
    from qualgentbench import truth

    case = next(c for c in journey.load_cases("medtimer")["test_cases"] if c["id"] == _ANR_CASE)
    steps = truth._steps(case["check"]["steps"])
    assert [(s.action, s.value, s.row) for s in steps] == [
        ("launch", "", ""), ("tap", "Reminded", "Ibuprofen (4)"), ("tap", "Taken", ""),
        ("tap", "Medicine", ""), ("wait", "", "")]
    assert '"Ibuprofen (4)"' in case["steps"][0], "the brief names the same reminder"
    assert "amount='4'" in case["check"]["expect"]["query"]
    assert case["check"]["expect"]["anr"] is True
    assert case["bugs"] == ["overview-action-blocks-main-thread"]


def _medtimer_hunt_features() -> dict:
    from qualgentbench import corpus
    from qualgentbench.bugs import load_suite
    suite = load_suite(corpus.spec_path("medtimer"))
    return {f["id"]: f for f in suite["exploration"]["features"]}


def test_the_hunt_checks_answer_ibuprofens_reminder_whatever_the_hour():
    """QUA-2736, the hunt twin of the test above. The hard tier's `event_take` and
    `dose_stock` checks answer the same raised 4-unit reminder, so after 08:00 the bare
    anchor answered Aspirin's there too. Read through `truth.check_of`, the parser
    `derive_truth.py` replays — and nothing else about either area moved: the same
    state, the same seeded bug, the same oracle."""
    from qualgentbench import truth

    features = _medtimer_hunt_features()
    for area in ("event_take", "dose_stock"):
        claim = truth.check_of(features[area])
        assert [(s.action, s.value, s.row) for s in claim.steps] == [
            ("launch", "", ""), ("tap", "Reminded", "Ibuprofen (4)"), ("tap", "Taken", ""),
            ("wait", "", "")], area
    assert features["event_take"]["state"] == "ok"
    assert "amount='4' and status='TAKEN'" in features["event_take"]["check"]["expect"]["query"]
    assert (features["dose_stock"]["state"], features["dose_stock"]["bug_id"]) == (
        "broken", "stock-decrement-ignores-amount")
    assert features["dose_stock"]["check"]["expect"]["equals"] == "6.0"


def test_no_medtimer_route_taps_a_bare_reminded():
    """"Reminded" names a STATE and repeats on every raised reminder, so a tap on it
    that does not say which row answers whichever reminder sorts first — a function of
    the device clock. Every medtimer route, hunt check or journey case, must scope it."""
    from qualgentbench.submission import ROW_VERBS, route_item

    routes = {f"hunt:{fid}": (f.get("check") or {}).get("steps")
              for fid, f in _medtimer_hunt_features().items()}
    routes.update({f"journey:{c['id']}": (c.get("check") or {}).get("steps")
                   for c in journey.load_cases("medtimer")["test_cases"]})
    bare = [where for where, steps in routes.items() for item in steps or []
            if (shape := route_item(item)) and shape[0] in ROW_VERBS
            and shape[1] == "Reminded" and not shape[2]]
    assert not bare, f"unscoped 'Reminded' taps: {bare}"
    assert sum(1 for steps in routes.values() for item in steps or []
               if (shape := route_item(item)) and shape[1] == "Reminded") >= 3, \
        "the scan found the routes it is meant to guard"


def test_a_freeze_case_credits_nothing_from_the_screen_the_agent_could_not_have_seen():
    """The guard that makes a death case scorable at all. A frozen or dead app stops
    drawing, so the clean/seeded diff is full of strings the seeded agent never saw —
    on the ANR case the diff literally captures the platform's own dialog. None of it
    may become quotable evidence, so `journey_tasks` empties `blocking_texts` whenever
    the check names a death and builds `crash_texts` instead."""
    specs = _medtimer_specs()
    for cid in (_ANR_CASE, _STUCK_CASE):
        spec = specs[(cid, "seeded")]
        assert spec["blocking_texts"] == [], f"{cid}: the screen diff must not be quotable"
        assert spec["side"] == [], f"{cid}: no display bug rides along"
        # Nothing the BRIEF hands the agent — and nothing the route could type — earns
        # the bug. (These routes type nothing at all, which is the cheapest defence.)
        for noun in ("Ibuprofen", "Aspirin", "Taken", "Medicine", "Tabular view",
                     "The Ibuprofen dose is recorded as taken"):
            r = journey.BugReport(step=None, screen="other", observed=noun,
                                  expected="it works", description="")
            assert journey.match_report(r, spec) is None, f"{cid}: {noun!r} bought credit"


# QUA-2711 left a test here pinning the gap that `observed: "the app is not responding"`
# earned the blocking bug on both freeze cases with no device contact at all, with
# instructions to delete it when the gap closed. QUA-2717 closed it: the platform's
# wording is now `echo_texts`, credited only to a report the DEVICE answered with it,
# and the two adversaries that model the attack (`dialog-echo`, `brief-echo`) are
# standing entries in `journey_adversary_check.GUESSERS`. The test below replaces it.


def test_the_platform_anr_wording_needs_the_device_to_have_said_it():
    """A freeze case has NO defect-identifying evidence: `anr: true` names no signature
    (deliberately — the dispatcher's wording carries a per-run window hash), so the
    platform's own sentence is the entire quotable surface. It is also a sentence
    anyone can write, which is the whole of QUA-2717.

    Both facts are kept: the honest agent whose app stopped answering quotes the dialog
    and is credited, because its transcript shows the device saying it; the guesser
    writing the same sentence into a findings file earns nothing. Polarity still backs
    this up — the same report is false on the clean arm — but it is no longer the only
    thing holding the line."""
    specs = _medtimer_specs()
    for cid in (_ANR_CASE, _STUCK_CASE):
        seeded = specs[(cid, "seeded")]
        assert seeded["crash_texts"] == [], "`anr: true` names no signature to quote"
        assert "MedTimer is not responding" in seeded["echo_texts"]

        def report(grounded):
            return journey.BugReport(step=None, screen="other", description="",
                                     observed="the app is not responding",
                                     expected="it keeps answering", grounded=grounded)

        assert journey.match_report(report(False), seeded) is None, \
            f"{cid}: the dialog wording still buys the bug with no device contact"
        assert journey.match_report(report(True), seeded) == seeded["blocking"]
        assert journey.match_report(report(True), specs[(cid, "clean")]) is None, \
            "the clean arm must credit nothing — that is where a sprayer pays"
