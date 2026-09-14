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
    a = C.CrashRecord("org.x.app", 10, "t", "anr", "ANR", "Input dispatching timed out (pid 10, 5001ms)")
    b = C.CrashRecord("org.x.app", 11, "t", "anr", "ANR", "Input dispatching timed out (pid 11, 5127ms)")
    assert C.signature(a, "org.x.app") == C.signature(b, "org.x.app")
    assert C.signature(a, "org.x.app").startswith("ANR@msg:")


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
    async def fake_adb(serial, *args, timeout=30.0):
        assert args == ("shell", "date", "+%m-%d %H:%M:%S.000")
        return "09-14 15:06:18.000\n"
    monkeypatch.setattr(C, "_adb_text", fake_adb)
    assert await C.device_time("emulator-5554") == "09-14 15:06:18.000"
