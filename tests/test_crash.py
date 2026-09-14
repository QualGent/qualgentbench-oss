"""Crash attribution — parsing, classification, signatures, exit-info gating and the
smoke verdict. Everything runs on captured logcat / dumpsys text from emulator-5554
(tests/fixtures/crash/); no test touches a device."""

from __future__ import annotations

from pathlib import Path

import pytest

from qualgentbench.verify import crash as C

FIX = Path(__file__).parent / "fixtures" / "crash"
NATIVE = (FIX / "native_foreign_media_module.log").read_text()
SHELL = (FIX / "java_shell_uiautomator_no_process.log").read_text()
APP = (FIX / "java_app_medtimer.log").read_text()
EXITS = (FIX / "exit_info_medtimer_and_latin.txt").read_text()
PKG = "com.futsch1.medtimer"


# ------------------------------------------------------------------ parsing

def test_parse_java_app_crash():
    recs = C.parse_crash_buffer(APP)
    assert len(recs) == 1
    r = recs[0]
    assert r.kind == "java"
    assert r.process == PKG
    assert r.pid == 28567
    assert r.timestamp == "09-08 23:00:23.458"
    assert r.exception == "android.view.InflateException"
    assert r.message.startswith("Binary XML file line #14")
    assert r.frames[0].startswith("at androidx.fragment.app.FragmentManager.checkStateLoss(")
    assert any("com.futsch1.medtimer.databinding.ContentMainBinding" in f for f in r.frames)
    assert "FATAL EXCEPTION: main" in r.raw


def test_parse_native_crash():
    recs = C.parse_crash_buffer(NATIVE)
    assert len(recs) == 1
    r = recs[0]
    assert r.kind == "native"
    # pid/process come from the ">>> proc <<<" line, not the crash_dump logger pid
    assert r.pid == 1705
    assert r.process == "com.google.android.providers.media.module"
    assert r.exception == "SIGABRT"
    assert r.message.startswith("Check failed: active_nodes_")
    assert len(r.frames) == 11
    assert r.frames[0].startswith("#00 pc ")


def test_parse_shell_java_crash_has_no_process():
    # `uiautomator dump` dying: "PID: n" only, no "Process:" line, and the block is
    # followed by a "Couldn't report crash" re-dump which must not become a record.
    recs = C.parse_crash_buffer(SHELL)
    assert len(recs) == 1
    r = recs[0]
    assert r.process == ""
    assert r.pid == 26657
    assert r.exception == "java.lang.IllegalStateException"
    assert r.frames[0] == "at android.os.Parcel.createExceptionOrNull(Parcel.java:3381)"
    # frames stop at the blank line: the re-dump's frames are not appended
    assert len(r.frames) == 19


def test_parse_multiple_records_in_order_and_interleaved():
    recs = C.parse_crash_buffer(NATIVE + SHELL + APP)
    assert [r.kind for r in recs] == ["native", "java", "java"]
    assert [r.pid for r in recs] == [1705, 26657, 28567]

    # line-level interleaving of two blocks from different pids
    a, b = APP.splitlines(), SHELL.splitlines()
    mixed = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            mixed.append(a[i])
        if i < len(b):
            mixed.append(b[i])
    recs = C.parse_crash_buffer("\n".join(mixed))
    by_pid = {r.pid: r for r in recs}
    assert set(by_pid) == {26657, 28567}
    assert by_pid[28567].process == PKG
    assert by_pid[28567].exception == "android.view.InflateException"
    assert by_pid[26657].exception == "java.lang.IllegalStateException"
    assert all(f.startswith(("at androidx", "at android", "at com.", "at java", "at kotlin")) for f in by_pid[28567].frames)


@pytest.mark.parametrize("garbage", [
    "", "\n\n", "--------- beginning of crash\n", "not a log line\n\x00\xff garbage",
    "09-08 23:00:23.458 28567 28567 E AndroidRuntime: FATAL EXCEPTION: main\n",   # truncated header only
    "09-08 23:00:23.458 28567 28567 E AndroidRuntime: \tat foo.Bar.baz(Bar.java:1)\n",  # frame with no header
    "09-08 18:53:14.192 11026 11026 F DEBUG   : *** *** ***\n09-08 18:53:14.192 11026 11026 F DEBUG   : pid: x, tid: y\n",
    APP.replace("Process: com.futsch1.medtimer, PID: 28567", "Process: "),
])
def test_parse_never_raises_on_malformed(garbage):
    recs = C.parse_crash_buffer(garbage)
    assert isinstance(recs, list)


def test_parse_time_format_variant():
    text = ("09-08 23:00:23.458 E/AndroidRuntime(28567): FATAL EXCEPTION: main\n"
            "09-08 23:00:23.458 E/AndroidRuntime(28567): Process: org.x.app, PID: 28567\n"
            "09-08 23:00:23.458 E/AndroidRuntime(28567): java.lang.NullPointerException: boom\n"
            "09-08 23:00:23.458 E/AndroidRuntime(28567): \tat org.x.app.Main.onCreate(Main.kt:12)\n")
    recs = C.parse_crash_buffer(text)
    assert len(recs) == 1 and recs[0].process == "org.x.app" and recs[0].frames == ["at org.x.app.Main.onCreate(Main.kt:12)"]


def test_parse_anr_block_when_present():
    text = ("09-10 10:00:00.000   731   800 E ActivityManager: ANR in org.x.app (org.x.app/.MainActivity)\n"
            "09-10 10:00:00.000   731   800 E ActivityManager: PID: 4242\n"
            "09-10 10:00:00.000   731   800 E ActivityManager: Reason: Input dispatching timed out (Waiting to send key event)\n"
            "09-10 10:00:00.000   731   800 E ActivityManager: Load: 1.0 / 2.0 / 3.0\n")
    recs = C.parse_crash_buffer(text)
    assert len(recs) == 1
    r = recs[0]
    assert (r.kind, r.exception, r.process, r.pid) == ("anr", "ANR", "org.x.app", 4242)
    assert r.message.startswith("Input dispatching timed out")
    assert C.classify(r, "org.x.app") == "app"


# ------------------------------------------------------------------ classify

def _rec(process: str, kind: str = "java") -> C.CrashRecord:
    return C.CrashRecord(process=process, pid=1, timestamp="09-01 00:00:00.000", kind=kind,
                         exception="E", message="m")


def test_classify_three_ways():
    assert C.classify(_rec("org.foo"), "org.foo") == "app"
    assert C.classify(_rec("org.foo:service"), "org.foo") == "app"
    assert C.classify(_rec("org.foo", "native"), "org.foo") == "app-native"
    assert C.classify(_rec("org.foo:remote", "native"), "org.foo") == "app-native"
    assert C.classify(_rec("com.google.android.inputmethod.latin", "native"), "org.foo") == "foreign"
    assert C.classify(_rec(""), "org.foo") == "foreign"        # shell command with no Process:


def test_classify_prefix_package_does_not_claim_longer_package():
    assert C.classify(_rec("org.foobar"), "org.foo") == "foreign"
    assert C.classify(_rec("org.foo.bar"), "org.foo") == "foreign"
    assert C.classify(_rec("org.foo"), "org.foobar") == "foreign"


def test_classify_real_fixtures():
    recs = C.parse_crash_buffer(NATIVE + SHELL + APP)
    assert [C.classify(r, PKG) for r in recs] == ["foreign", "foreign", "app"]


# ------------------------------------------------------------------ signature

def test_signature_stable_and_normalised():
    r = C.parse_crash_buffer(APP)[0]
    sig = C.signature(r, PKG)
    assert sig.startswith("android.view.InflateException@")
    # app frames chosen over the androidx frames that top the stack
    assert "com.futsch1.medtimer.databinding.ContentMainBinding.inflate(ContentMainBinding.java)" in sig
    assert ":42)" not in sig and ":132)" not in sig            # line numbers stripped
    assert sig == C.signature(C.parse_crash_buffer(APP)[0], PKG)

    # same crash, different pid / line numbers / object hash -> same signature
    again = (APP.replace("28567", "31337")
                .replace("ContentMainBinding.java:42", "ContentMainBinding.java:57")
                .replace("AppNavigation.kt:132", "AppNavigation.kt:140"))
    assert C.signature(C.parse_crash_buffer(again)[0], PKG) == sig

    # a different crash differs
    other = (APP.replace("android.view.InflateException", "java.lang.NullPointerException")
                .replace("ContentMainBinding.inflate", "ContentMainBinding.bind"))
    assert C.signature(C.parse_crash_buffer(other)[0], PKG) != sig


def test_signature_strips_hashes_and_synthetic_lambdas():
    text = ("09-08 23:00:23.458 1 1 E AndroidRuntime: FATAL EXCEPTION: main\n"
            "09-08 23:00:23.458 1 1 E AndroidRuntime: Process: org.x.app, PID: 1\n"
            "09-08 23:00:23.458 1 1 E AndroidRuntime: java.lang.IllegalStateException: View@1a2b3c already attached\n"
            "09-08 23:00:23.458 1 1 E AndroidRuntime: \tat org.x.app.Foo$$ExternalSyntheticLambda7.run(D8$$SyntheticClass:0)\n"
            "09-08 23:00:23.458 1 1 E AndroidRuntime: \tat org.x.app.Foo.$r8$lambda$PpTq9iiXs893M8HhCTZFJ_nW2zg(Unknown Source:0)\n"
            "09-08 23:00:23.458 1 1 E AndroidRuntime: \tat org.x.app.Foo.bar(Foo.kt:9)\n")
    sig = C.signature(C.parse_crash_buffer(text)[0], "org.x.app")
    assert sig == ("java.lang.IllegalStateException@org.x.app.Foo$$Lambda.run(D8$$SyntheticClass) > "
                   "org.x.app.Foo.$r8$lambda(Unknown Source) > org.x.app.Foo.bar(Foo.kt)")
    text2 = text.replace("ExternalSyntheticLambda7", "ExternalSyntheticLambda12").replace("PpTq9iiXs893M8HhCTZFJ_nW2zg", "AbCdEf")
    assert C.signature(C.parse_crash_buffer(text2)[0], "org.x.app") == sig


def test_signature_native_frames_and_depth():
    r = C.parse_crash_buffer(NATIVE)[0]
    sig = C.signature(r, "org.unrelated", depth=2)
    assert sig == "SIGABRT@libc.so (abort) > libart.so (art::Runtime::Abort(char const*))"
    assert "BuildId" not in sig and "00000000000754b0" not in sig
    # a native frame inside the app's own lib is preferred when the package matches
    own = C.CrashRecord(process="org.x.app", pid=5, timestamp="t", kind="native", exception="SIGSEGV",
                        message="signal 11", frames=[
                            "#00 pc 0000000000075abc  /apex/com.android.runtime/lib64/bionic/libc.so (memcpy+12) (BuildId: aa)",
                            "#01 pc 000000000001f0f0  /data/app/~~Zz==/org.x.app-Qq==/base.apk!libnative.so (offset 0x1000) (Java_org_x_app_Native_boom+40) (BuildId: bb)",
                        ])
    assert C.signature(own, "org.x.app") == "SIGSEGV@base.apk!libnative.so (Java_org_x_app_Native_boom)"


def test_signature_without_frames_uses_normalised_message():
    a = C.CrashRecord("org.x.app", 10, "t", "java", "java.lang.OutOfMemoryError", "Failed to allocate 1048576 bytes at 0x7f3a")
    b = C.CrashRecord("org.x.app", 11, "t", "java", "java.lang.OutOfMemoryError", "Failed to allocate 2097152 bytes at 0x1b00")
    assert C.signature(a, "org.x.app") == C.signature(b, "org.x.app")
    assert C.signature(a, "org.x.app").startswith("java.lang.OutOfMemoryError@msg:")


def test_anr_signature_is_the_normalised_reason():
    a = C.CrashRecord("org.x.app", 10, "t", "anr", "ANR",
                      "Input dispatching timed out (a1b2c3d org.x.app/org.x.app.Main is not responding. Waited 5001ms for MotionEvent).")
    b = C.CrashRecord("org.x.app", 11, "t", "anr", "ANR",
                      "Input dispatching timed out (e5f6a7b org.x.app/org.x.app.Main is not responding. Waited 5127ms for KeyEvent).")
    assert C.signature(a, "org.x.app") == C.signature(b, "org.x.app")
    assert C.signature(a, "org.x.app") == "ANR@Input dispatching timed out (org.x.app/org.x.app.Main)"


# ------------------------------------------------------------------ exit-info

def test_parse_exit_info_filters_to_package_and_normalises_reasons():
    recs = C.parse_exit_info(EXITS, PKG)
    assert recs and all(r.process == PKG for r in recs)
    assert {r.reason for r in recs} == {"USER_REQUESTED"}      # our own force-stops
    assert {r.sub_reason for r in recs} == {"FORCE_STOP"}
    assert recs[0].pid == 26047
    assert recs[0].timestamp == "2026-09-09 00:10:36.610"
    assert recs[0].description == "stop com.futsch1.medtimer due to from pid 26736"
    assert not any(r.fatal for r in recs)

    latin = C.parse_exit_info(EXITS, "com.google.android.inputmethod.latin")
    assert [r.reason for r in latin] == ["USER_REQUESTED", "CRASH_NATIVE", "CRASH_NATIVE", "PACKAGE_UPDATED", "SIGNALED"]
    assert latin[0].process == "com.google.android.inputmethod.latin:primes_lifeboat"
    assert latin[0].sub_reason == "KILL_BACKGROUND"
    assert latin[1].fatal and latin[1].pid == 12648 and latin[1].description == "crash"
    assert latin[4].sub_reason is None and latin[4].description == ""


def test_parse_exit_info_fatal_reason_gating():
    # synthetic records in the real dump format for the two reasons no fixture carries
    text = EXITS + (
        "        ApplicationExitInfo #9:\n"
        "          timestamp=2026-09-14 15:20:01.000 pid=777 realUid=1 packageUid=1 definingUid=1 user=0\n"
        f"          process={PKG} reason=4 (APP CRASH(EXCEPTION)) subreason=0 (UNKNOWN) status=0\n"
        "          importance=100 pss=0.00 rss=1MB description=crash state=empty trace=null\n"
        "        ApplicationExitInfo #10:\n"
        "          timestamp=2026-09-14 15:21:01.000 pid=778 realUid=1 packageUid=1 definingUid=1 user=0\n"
        f"          process={PKG}:worker reason=6 (ANR) subreason=0 (UNKNOWN) status=0\n"
        "          importance=100 pss=0.00 rss=1MB description=ANR Input dispatching timed out state=empty trace=null\n"
        "        ApplicationExitInfo #11:\n"
        "          timestamp=2026-09-14 15:22:01.000 pid=779 realUid=1 packageUid=1 definingUid=1 user=0\n"
        f"          process={PKG}bar reason=4 (APP CRASH(EXCEPTION)) subreason=0 (UNKNOWN) status=0\n"
        "          importance=100 pss=0.00 rss=1MB description=crash state=empty trace=null\n"
    )
    recs = C.parse_exit_info(text, PKG)
    fatal = [r for r in recs if r.fatal]
    assert [(r.reason, r.pid) for r in fatal] == [("CRASH", 777), ("ANR", 778)]
    assert fatal[1].process == f"{PKG}:worker"
    assert not any(r.pid == 779 for r in recs)                  # prefix package not claimed
    assert C.FATAL_REASONS == {"CRASH", "CRASH_NATIVE", "ANR"}
    assert C.parse_exit_info("garbage\n  process=x reason=zz\n", PKG) == []


# ------------------------------------------------------------------ events

def test_parse_event_log():
    text = ("09-12 04:43:05.470   731  1215 I am_kill : [0,20481,com.android.chrome:sandboxed_process0,0,isolated not needed,0]\n"
            "09-12 04:43:06.000   731   762 I am_crash: [4242,10229,com.futsch1.medtimer,955725381,java.lang.NullPointerException,boom, with comma,Main.kt,12]\n"
            "09-12 04:43:07.000   731   762 I am_anr  : [10229,4243,com.futsch1.medtimer,955725381,Input dispatching timed out (x, y)]\n"
            "junk\n")
    evs = C.parse_event_log(text)
    assert [(e.kind, e.pid, e.package) for e in evs] == [("crash", 4242, PKG), ("anr", 4243, PKG)]
    assert evs[0].detail == "java.lang.NullPointerException"
    assert evs[1].detail == "Input dispatching timed out (x, y)"
    assert evs[0].timestamp == "09-12 04:43:06.000"


# ------------------------------------------------------------------ time window

def test_is_after_handles_both_timestamp_shapes():
    assert C.is_after("09-09 00:10:36.610", "09-09 00:10:36.000")
    assert C.is_after("2026-09-09 00:10:36.610", "09-09 00:10:36.000")
    assert not C.is_after("2026-09-08 23:59:59.999", "09-09 00:00:00.000")
    assert C.is_after("09-01 00:00:00.000", "")


# ------------------------------------------------------------------ smoke verdict

def test_smoke_verdict_ignores_foreign_crashes():
    # 275 FATAL EXCEPTIONs on the real emulator were all shell/keyboard/media crashes:
    # foreign crash + no app crash + only harness force-stops in exit-info must PASS.
    ok, report = C.smoke_verdict(NATIVE + SHELL, EXITS, PKG, since="")
    assert ok, report
    assert "2 foreign crash(es) ignored:" in report
    assert "com.google.android.providers.media.module" in report
    assert "1 unnamed shell process(es)" in report
    assert "✗" not in report


def test_smoke_verdict_fails_on_app_crash_with_signature():
    ok, report = C.smoke_verdict(NATIVE + SHELL + APP, EXITS, PKG, since="")
    assert not ok
    lines = report.splitlines()
    assert lines[0].startswith(f"✗ app crash in {PKG} (pid 28567) at 09-08 23:00:23.458: android.view.InflateException")
    assert lines[1].startswith("  signature: android.view.InflateException@com.futsch1.medtimer.")
    assert "2 foreign crash(es) ignored" in report


def test_smoke_verdict_respects_since_window():
    # the app crash is at 09-08 23:00:23; a launch timestamp after it must not see it
    ok, report = C.smoke_verdict(NATIVE + SHELL + APP, EXITS, PKG, since="09-08 23:30:00.000")
    assert ok, report
    assert report == ""
    ok, _ = C.smoke_verdict(NATIVE + SHELL + APP, EXITS, PKG, since="09-08 22:00:00.000")
    assert not ok


def test_smoke_verdict_fails_on_exit_info_alone():
    exits = EXITS + (
        "        ApplicationExitInfo #9:\n"
        "          timestamp=2026-09-14 15:20:01.000 pid=777 realUid=1 packageUid=1 definingUid=1 user=0\n"
        f"          process={PKG} reason=6 (ANR) subreason=0 (UNKNOWN) status=0\n"
        "          importance=100 pss=0.00 rss=1MB description=Input dispatching timed out state=empty trace=null\n"
    )
    ok, report = C.smoke_verdict("", exits, PKG, since="09-14 15:00:00.000")
    assert not ok
    assert report.startswith(f"✗ exit-info: {PKG} pid 777 died with ANR at 2026-09-14 15:20:01.000: Input dispatching")
    # older than the launch: the same record is ignored
    ok, report = C.smoke_verdict("", exits, PKG, since="09-14 15:30:00.000")
    assert ok and report == ""


def test_smoke_verdict_app_prefix_package_is_foreign():
    other = APP.replace("Process: com.futsch1.medtimer,", "Process: com.futsch1.medtimerpro,")
    ok, report = C.smoke_verdict(other, "", PKG, since="")
    assert ok
    assert "1 foreign crash(es) ignored: com.futsch1.medtimerpro" in report


# ------------------------------------------------------------------ adb wrappers (no device)

async def test_app_crashed_since_combines_signals(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_adb(serial, *args, timeout=30.0):
        calls.append(args)
        if args[0] == "logcat":
            assert "-c" not in args and "-T" in args     # time window, never a buffer clear
            return NATIVE + SHELL                          # foreign only
        if args[:4] == ("shell", "dumpsys", "activity", "exit-info"):
            return EXITS + (
                "        ApplicationExitInfo #9:\n"
                "          timestamp=2026-09-14 15:20:01.000 pid=777 realUid=1 packageUid=1 definingUid=1 user=0\n"
                f"          process={PKG} reason=5 (APP CRASH(NATIVE)) subreason=0 (UNKNOWN) status=0\n"
                "          importance=100 pss=0.00 rss=1MB description=crash state=empty trace=null\n")
        raise AssertionError(args)

    monkeypatch.setattr(C, "_adb_text", fake_adb)
    assert await C.crashes_since("emulator-5554", PKG, "09-01 00:00:00.000") == []
    assert len(await C.crashes_since("emulator-5554", PKG, "09-01 00:00:00.000", include_foreign=True)) == 2
    r = await C.app_crashed_since("emulator-5554", PKG, "09-14 15:00:00.000")
    assert r is not None and r.kind == "native" and r.exception == "CRASH_NATIVE" and r.pid == 777
    assert r.raw.startswith("exit-info:")
    assert await C.app_crashed_since("emulator-5554", PKG, "09-14 15:30:00.000") is None
    assert ("logcat", "-d", "-b", "crash", "-v", "threadtime", "-T", "09-14 15:00:00.000") in calls


async def test_app_crashed_since_prefers_crash_buffer(monkeypatch):
    async def fake_adb(serial, *args, timeout=30.0):
        return APP if args[0] == "logcat" else ""
    monkeypatch.setattr(C, "_adb_text", fake_adb)
    r = await C.app_crashed_since(None, PKG, "")
    assert r is not None and r.kind == "java" and r.pid == 28567 and r.frames


async def test_device_time_format(monkeypatch):
    """The format must reach the device shell as ONE word. `adb shell` joins argv with
    spaces and the device re-parses, so `("shell", "date", "+%m-%d %H:%M:%S.000")` made
    toybox see two arguments and answer `date: Max 1 argument` — which then became
    every crash window's `since` and silently disabled the check on emulator-5554."""
    async def fake_adb(serial, *args, timeout=30.0):
        assert args == ("shell", "date '+%m-%d %H:%M:%S.000'"), args
        return "09-14 15:06:18.000\n"
    monkeypatch.setattr(C, "_adb_text", fake_adb)
    assert await C.device_time("emulator-5554") == "09-14 15:06:18.000"


# ------------------------------------------------------------------ ANR (live signals)
#
# Captured 2026-09-14 on the same emulator (stock Android 16 Google Play image) from
# an input-dispatch ANR induced with `run-as <pkg> kill -STOP <pid>` + `input tap`:
# the WindowManager one-liner and `am_anr` at +5 s, the ActivityManager block at
# +17.5 s, the exit-info row only after "Close app". The crash buffer stayed empty.

ANR_MS = (FIX / "anr_main_system_medtimer.log").read_text()
ANR_EV = (FIX / "anr_events_medtimer.log").read_text()
ANR_EXITS = (FIX / "exit_info_medtimer_anr.txt").read_text()
ANR_INPUT = (FIX / "dumpsys_input_anr_excerpt.txt").read_text()
ANR_INPUT_RECOVERED = (FIX / "dumpsys_input_after_anr_recovered_excerpt.txt").read_text()
ANR_SIG = "ANR@Input dispatching timed out (com.futsch1.medtimer/com.futsch1.medtimer.MainActivity)"
WM_LINE = next(l for l in ANR_MS.splitlines() if "WindowManager: ANR in Window" in l)


def test_parse_real_anr_wm_line_and_am_block_are_one_record():
    recs = C.parse_crash_buffer(ANR_MS)
    assert len(recs) == 1, [(r.timestamp, r.process) for r in recs]
    r = recs[0]
    assert (r.kind, r.exception, r.process, r.pid) == ("anr", "ANR", PKG, 28277)
    # first sighting: the WindowManager one-liner, 12.5 s before the block
    assert r.timestamp == "09-14 16:12:01.279"
    assert r.message.startswith("Input dispatching timed out (d8334d4 com.futsch1.medtimer/")
    assert "Waited 5001ms for MotionEvent" in r.message
    assert "E ActivityManager: ANR in com.futsch1.medtimer (com.futsch1.medtimer/.MainActivity)" in r.raw
    assert r.frames == []
    assert C.classify(r, PKG) == "app"
    assert C.signature(r, PKG) == ANR_SIG


def test_parse_anr_block_alone_when_the_wm_line_is_not_in_the_read():
    # `anrs_since` reads `-s ActivityManager:E`, so the block arrives without the WM line.
    text = "\n".join(l for l in ANR_MS.splitlines() if "WindowManager" not in l)
    recs = C.parse_crash_buffer(text)
    assert len(recs) == 1
    assert (recs[0].pid, recs[0].timestamp) == (28277, "09-14 16:12:13.731")
    assert recs[0].message.startswith("Input dispatching timed out (d8334d4 ")
    assert C.signature(recs[0], PKG) == ANR_SIG


def test_parse_wm_line_alone_is_a_record_with_no_pid():
    recs = C.parse_crash_buffer(WM_LINE + "\n")
    assert len(recs) == 1
    assert (recs[0].kind, recs[0].process, recs[0].pid) == ("anr", PKG, None)
    assert recs[0].message.startswith("Input dispatching timed out (")


def test_wm_line_and_block_for_different_processes_do_not_merge():
    other = WM_LINE.replace("com.futsch1.medtimer/com.futsch1.medtimer.MainActivity",
                            "org.other.app/org.other.app.Main")
    recs = C.parse_crash_buffer(other + "\n" + ANR_MS.replace(WM_LINE, ""))
    assert sorted(r.process for r in recs) == [PKG, "org.other.app"]
    assert C.classify(next(r for r in recs if r.process == "org.other.app"), PKG) == "foreign"


def test_parse_real_am_anr_event():
    evs = C.parse_event_log(ANR_EV)
    assert [e.kind for e in evs] == ["anr"]            # am_kill / am_proc_died / am_proc_start are not records
    e = evs[0]
    assert (e.pid, e.package, e.timestamp) == (28277, PKG, "09-14 16:12:01.286")
    assert e.detail.startswith("Input dispatching timed out (d8334d4 com.futsch1.medtimer/")


def test_merge_anr_signals_folds_the_block_into_the_event():
    recs = C.merge_anr_signals(C.parse_event_log(ANR_EV), C.parse_crash_buffer(ANR_MS), PKG)
    assert len(recs) == 1
    r = recs[0]
    assert (r.kind, r.pid, r.process, r.timestamp) == ("anr", 28277, PKG, "09-14 16:12:01.286")
    assert r.raw.startswith("am_anr: pid=28277") and "E ActivityManager: ANR in" in r.raw
    assert C.signature(r, PKG) == ANR_SIG


def test_merge_anr_signals_with_either_signal_alone():
    # +5..+17 s: only am_anr exists yet (stack collection still running)
    only_event = C.merge_anr_signals(C.parse_event_log(ANR_EV), [], PKG)
    assert len(only_event) == 1 and only_event[0].pid == 28277
    assert C.signature(only_event[0], PKG) == ANR_SIG
    # the events ring rolled over: the block stands on its own
    only_block = C.merge_anr_signals([], C.parse_crash_buffer(ANR_MS), PKG)
    assert len(only_block) == 1 and only_block[0].pid == 28277
    assert C.signature(only_block[0], PKG) == ANR_SIG


def test_merge_anr_signals_gates_on_package_and_window():
    evs, blocks = C.parse_event_log(ANR_EV), C.parse_crash_buffer(ANR_MS)
    assert C.merge_anr_signals(evs, blocks, "org.other.app") == []
    assert C.merge_anr_signals(evs, blocks, "com.futsch1") == []          # prefix is not ownership
    assert C.merge_anr_signals(evs, blocks, PKG, since="09-14 16:12:00.000")
    assert C.merge_anr_signals(evs, blocks, PKG, since="09-14 16:13:00.000") == []


def test_exit_info_anr_row_is_fatal_and_signs_like_the_log_lines():
    rows = C.parse_exit_info(ANR_EXITS, PKG)
    assert [r.reason for r in rows] == ["ANR", "USER_REQUESTED", "USER_REQUESTED"]
    e = rows[0]
    assert e.fatal and e.pid == 28277 and e.reason_code == 6
    assert e.timestamp == "2026-09-14 16:12:28.628"
    assert e.description.startswith("user request after error: Input dispatching timed out (d8334d4 ")
    # The harness's own force-stop of a process that had ANR'd earlier carries the
    # ANR trace file but stays USER_REQUESTED/FORCE_STOP — not a fatal row.
    assert not rows[1].fatal and rows[1].sub_reason == "FORCE_STOP"
    assert "trace=/data/system/procexitstore/anr_" in ANR_EXITS
    late = C.CrashRecord(process=e.process, pid=e.pid, timestamp=e.timestamp, kind="anr",
                         exception="ANR", message=e.description)
    assert C.signature(late, PKG) == ANR_SIG


@pytest.mark.parametrize("reason, expected", [
    ("Input dispatching timed out (d8334d4 com.x/com.x.Main is not responding. Waited 5001ms for MotionEvent).",
     "Input dispatching timed out (com.x/com.x.Main)"),
    ("user request after error: Input dispatching timed out (6733a2e com.x/com.x.Main is not responding. Waited 5002ms for KeyEvent).",
     "Input dispatching timed out (com.x/com.x.Main)"),
    ("Input dispatching timed out (Application does not have a focused window)",
     "Input dispatching timed out (Application does not have a focused window)"),
    ("Broadcast of Intent { act=android.intent.action.BOOT flg=0x10 cmp=com.x/.Recv }",
     "Broadcast of Intent { act=android.intent.action.BOOT flg=0x cmp=com.x/.Recv }"),
    ("executing service com.x/.Svc, waited 20001ms", "executing service com.x/.Svc, waited #ms"),
])
def test_norm_anr_reason(reason, expected):
    assert C._norm_anr_reason(reason) == expected


def test_dispatching_timeout_is_read_from_the_live_section_only():
    assert C.parse_dispatching_timeout_ms(ANR_INPUT) == 5000
    assert C.parse_dispatching_timeout_ms(ANR_INPUT_RECOVERED) == 5000
    assert C.parse_dispatching_timeout_ms("") is None
    stale_only = ANR_INPUT[ANR_INPUT.index("Input Dispatcher State at time of last ANR:"):]
    assert "dispatchingTimeout=5000ms" in stale_only
    assert C.parse_dispatching_timeout_ms(stale_only) is None
    assert C.parse_dispatching_timeout_ms(ANR_INPUT.replace("dispatchingTimeout=5000ms", "dispatchingTimeout=8000ms", 1)) == 8000


def test_unresponsive_windows_ignores_the_last_anr_snapshot():
    rows = C.parse_unresponsive_windows(ANR_INPUT)
    assert rows == [("d8334d4 com.futsch1.medtimer/com.futsch1.medtimer.MainActivity", 5)]
    assert C._window_owned_by(rows[0][0], PKG)
    assert not C._window_owned_by(rows[0][0], "com.futsch1")
    # After kill -CONT the live connection is responsive again; the snapshot at the
    # end of the dump still says false — a naive grep would report the old ANR forever.
    assert "responsive=false" in ANR_INPUT_RECOVERED
    assert C.parse_unresponsive_windows(ANR_INPUT_RECOVERED) == []
    assert C.parse_unresponsive_windows("") == []


def _fake_adb(monkeypatch, *, crash="", events="", main_system="", exit_info="", dumpsys_input=""):
    calls: list[tuple[str, ...]] = []

    async def fake(serial, *args, timeout=30.0):
        calls.append(args)
        if args[:1] == ("logcat",):
            if "crash" in args:
                return crash
            if "events" in args:
                return events
            if "main,system" in args:
                return main_system
        if "exit-info" in args:
            return exit_info
        if args[-1] == "input" and "dumpsys" in args:
            return dumpsys_input
        return ""
    monkeypatch.setattr(C, "_adb_text", fake)
    return calls


@pytest.mark.asyncio
async def test_app_crashed_since_finds_a_live_anr_from_the_log_lines(monkeypatch):
    calls = _fake_adb(monkeypatch, events=ANR_EV, main_system=ANR_MS, exit_info=ANR_EXITS)
    since = "09-14 16:11:56.000"
    rec = await C.app_crashed_since("s", PKG, since)
    assert rec is not None
    assert (rec.kind, rec.pid, rec.process, rec.timestamp) == ("anr", 28277, PKG, "09-14 16:12:01.286")
    assert C.signature(rec, PKG) == ANR_SIG
    # Only filtered reads, each opened at the window — never a whole buffer.
    assert ("logcat", "-d", "-b", "events", "-v", "threadtime", "-s", "am_anr", "-T", since) in calls
    assert ("logcat", "-d", "-b", "main,system", "-v", "threadtime", "-s", "ActivityManager:E", "-T", since) in calls
    assert not any("exit-info" in c for c in calls), "exit-info is the late signal; not needed here"


@pytest.mark.asyncio
async def test_app_crashed_since_falls_back_to_the_exit_info_anr_row(monkeypatch):
    _fake_adb(monkeypatch, events=ANR_EV, main_system=ANR_MS, exit_info=ANR_EXITS)
    # A window opened after the log lines but before "Close app" killed the process.
    rec = await C.app_crashed_since("s", PKG, "09-14 16:12:20.000")
    assert rec is not None and rec.kind == "anr" and rec.raw.startswith("exit-info:")
    assert rec.pid == 28277 and C.signature(rec, PKG) == ANR_SIG
    # And a window after everything: nothing.
    assert await C.app_crashed_since("s", PKG, "09-14 16:13:00.000") is None


@pytest.mark.asyncio
async def test_app_crashed_since_prefers_the_earliest_event(monkeypatch):
    # A java crash 20 s after the ANR: the ANR is what the repro hit first.
    later_crash = APP.replace("09-08 23:00:23.458", "09-14 16:12:21.000")
    _fake_adb(monkeypatch, crash=later_crash, events=ANR_EV, main_system=ANR_MS)
    rec = await C.app_crashed_since("s", PKG, "09-14 16:11:56.000")
    assert rec.kind == "anr"
    earlier_crash = APP.replace("09-08 23:00:23.458", "09-14 16:11:59.000")
    _fake_adb(monkeypatch, crash=earlier_crash, events=ANR_EV, main_system=ANR_MS)
    rec = await C.app_crashed_since("s", PKG, "09-14 16:11:56.000")
    assert rec.kind == "java"


@pytest.mark.asyncio
async def test_anr_timeout_ms_reads_the_device_or_falls_back(monkeypatch):
    _fake_adb(monkeypatch, dumpsys_input=ANR_INPUT)
    assert await C.anr_timeout_ms("s") == 5000
    _fake_adb(monkeypatch, dumpsys_input=ANR_INPUT.replace("dispatchingTimeout=5000ms", "dispatchingTimeout=8000ms", 1))
    assert await C.anr_timeout_ms("s") == 8000
    _fake_adb(monkeypatch, dumpsys_input="")
    assert await C.anr_timeout_ms("s") == C.DEFAULT_ANR_TIMEOUT_MS == 5000


@pytest.mark.asyncio
async def test_unresponsive_windows_filters_by_package(monkeypatch):
    _fake_adb(monkeypatch, dumpsys_input=ANR_INPUT)
    win = "d8334d4 com.futsch1.medtimer/com.futsch1.medtimer.MainActivity"
    assert await C.unresponsive_windows("s") == [(win, 5)]
    assert await C.unresponsive_windows("s", PKG) == [(win, 5)]
    assert await C.unresponsive_windows("s", "org.other") == []


def test_smoke_verdict_sees_an_anr_only_through_exit_info():
    # The build smoke gate reads the crash buffer + exit-info: an ANR that only
    # reached the log lines is invisible to it; the exit-info row is not.
    ok, report = C.smoke_verdict("", ANR_EXITS, PKG, "09-14 16:11:56.000")
    assert not ok and "died with ANR" in report
    ok, _ = C.smoke_verdict("", ANR_EXITS, PKG, "09-14 16:13:00.000")
    assert ok
