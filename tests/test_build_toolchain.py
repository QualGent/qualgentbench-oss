"""The build toolchain preflight: what a clean machine is told when it cannot build.

Every seeded app is pinned to a release tag, so its toolchain floor is that release's,
not the newest one installed. Two shapes have to keep working:

  * Google ships API 37 as `platforms/android-37.0` while the build file asks for
    `compileSdk = 37`. An exact-name check reports an INSTALLED platform as missing.
  * The requirement may not be a literal at all — Fossify reaches it through
    `libs.versions.app.build.compileSDKVersion`, which only `gradle/libs.versions.toml`
    can resolve.

And the failure has to name the missing piece plus the command that installs it: the
message this replaces was a Gradle stack trace 90 seconds into the build.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_app", ROOT / "scripts" / "build_app.py")
build_app = importlib.util.module_from_spec(_spec)
sys.modules["build_app"] = build_app
_spec.loader.exec_module(build_app)


def _tree(tmp_path: Path, app_gradle: str, catalog: str = "") -> Path:
    app = tmp_path / "App"
    (app / "app").mkdir(parents=True)
    (app / "app" / "build.gradle.kts").write_text(app_gradle)
    if catalog:
        (app / "gradle").mkdir()
        (app / "gradle" / "libs.versions.toml").write_text(catalog)
    return app


# ── platform matching ──────────────────────────────────────────────────────────

def test_dotted_platform_directory_satisfies_a_plain_api_level():
    """`platforms/android-37.0` IS API 37 — the one that made this check necessary."""
    assert build_app.platform_dir_for(37, ["android-34", "android-36", "android-37.0"]) == "android-37.0"


def test_exact_platform_directory_still_matches():
    assert build_app.platform_dir_for(36, ["android-34", "android-36"]) == "android-36"


def test_a_lower_api_is_not_accepted_as_a_prefix():
    """android-37 must not satisfy compileSdk 3, nor android-3 satisfy 37."""
    assert build_app.platform_dir_for(3, ["android-37", "android-36"]) is None
    assert build_app.platform_dir_for(37, ["android-3"]) is None


def test_missing_platform_reports_none():
    assert build_app.platform_dir_for(37, ["android-34", "android-36"]) is None


# ── requirement detection ──────────────────────────────────────────────────────

def test_compile_sdk_read_as_a_literal(tmp_path):
    app = _tree(tmp_path, "android {\n    compileSdk = 37\n    minSdk = 28\n}\n")
    assert build_app.detect_compile_sdk(app) == 37


def test_compile_sdk_resolved_through_the_version_catalog(tmp_path):
    """The alias `app-build-compileSDKVersion` is spelled `app.build.compileSDKVersion`
    in the build file — Gradle maps `-` and `_` onto `.`."""
    app = _tree(
        tmp_path,
        "android {\n    compileSdk = project.libs.versions.app.build.compileSDKVersion.get().toInt()\n}\n",
        '[versions]\napp-build-compileSDKVersion = "36"\napp-build-minimumSDK = "26"\n',
    )
    assert build_app.detect_compile_sdk(app) == 36


def test_compile_sdk_is_none_when_nothing_declares_it(tmp_path):
    assert build_app.detect_compile_sdk(_tree(tmp_path, "android {\n}\n")) is None


def test_min_jdk_takes_the_highest_level_the_build_asks_for(tmp_path):
    app = _tree(tmp_path, "java {\n    sourceCompatibility = JavaVersion.VERSION_17\n}\n"
                          "toolchain {\n    languageVersion.set(JavaLanguageVersion.of(21))\n}\n")
    assert build_app.detect_min_jdk(app) == 21


def test_min_jdk_resolved_through_the_version_catalog(tmp_path):
    app = _tree(
        tmp_path,
        "val v = JavaVersion.valueOf(libs.versions.app.build.javaVersion.get().toString())\n",
        '[versions]\napp-build-javaVersion = "VERSION_17"\n',
    )
    assert build_app.detect_min_jdk(app) == 17


# ── the SDK root ───────────────────────────────────────────────────────────────

def test_sdk_root_prefers_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(tmp_path))
    assert build_app.android_sdk_root() == tmp_path


def test_sdk_root_falls_back_to_android_home(monkeypatch, tmp_path):
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path))
    assert build_app.android_sdk_root() == tmp_path


# ── the message ────────────────────────────────────────────────────────────────

def _sdk(tmp_path: Path, platforms: list[str]) -> Path:
    sdk = tmp_path / "sdk"
    for name in platforms:
        (sdk / "platforms" / name).mkdir(parents=True)
    (sdk / "platforms").mkdir(parents=True, exist_ok=True)
    return sdk


def test_missing_platform_names_the_install_command(monkeypatch, tmp_path, capsys):
    sdk = _sdk(tmp_path, ["android-34", "android-36"])
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk))
    monkeypatch.setattr(build_app, "jdk_major", lambda _jh: 21)
    app = _tree(tmp_path, "android {\n    compileSdk = 37\n}\n")
    with pytest.raises(SystemExit) as e:
        build_app.require_toolchain("medtimer", {"ref": "v1.25.2"}, app, "/jdk")
    msg = str(e.value)
    assert "Android SDK platform 37" in msg
    assert "android-34, android-36" in msg          # what IS there, so the gap is obvious
    assert "sdkmanager 'platforms;android-37.0'" in msg
    assert str(sdk) in msg


def test_too_old_jdk_is_named_and_the_app_is_not_downgraded(monkeypatch, tmp_path):
    sdk = _sdk(tmp_path, ["android-37.0"])
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk))
    monkeypatch.setattr(build_app, "jdk_major", lambda _jh: 17)
    app = _tree(tmp_path, "android {\n    compileSdk = 37\n}\n")
    with pytest.raises(SystemExit) as e:
        build_app.require_toolchain("medtimer", {"ref": "v1.25.2", "jdk": 21}, app, "/jdk")
    msg = str(e.value)
    assert "needs JDK 21+" in msg and "JAVA_HOME is JDK 17" in msg
    assert "Do NOT lower" in msg


def test_the_spec_overrides_what_the_checkout_says(monkeypatch, tmp_path):
    """`build.compile_sdk` is authoritative: upstream moving the value around in its
    build files must not silently change what the harness demands."""
    sdk = _sdk(tmp_path, ["android-36"])
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk))
    monkeypatch.setattr(build_app, "jdk_major", lambda _jh: 21)
    app = _tree(tmp_path, "android {\n    compileSdk = 37\n}\n")
    got = build_app.require_toolchain("x", {"compile_sdk": 36, "jdk": 17}, app, "/jdk")
    assert got["compile_sdk"] == 36 and got["platform"] == "android-36"


def test_a_real_sdk_is_required_before_anything_else(monkeypatch, tmp_path):
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(tmp_path / "nope"))
    with pytest.raises(SystemExit) as e:
        build_app.require_toolchain("x", {}, tmp_path, "/jdk")
    assert "No Android SDK" in str(e.value)


# ── the throwaway attribution canary ───────────────────────────────────────────

def test_demo_fired_call_is_fully_qualified(tmp_path):
    """No import is edited into the file, so the anchor can live in any sub-package."""
    spec = {"flags": {"package": "org.fossify.calendar"},
            "build": {"demo_fired": {"file": "a/Main.kt", "find": "super.onCreate(x)"}}}
    rel, find, replace = build_app._demo_fired_patch(spec)
    assert rel == "a/Main.kt" and find == "super.onCreate(x)"
    assert replace == 'super.onCreate(x)\norg.fossify.calendar.QgbFlags.fired("demo")'


def test_demo_fired_without_an_anchor_says_what_to_add(tmp_path):
    with pytest.raises(SystemExit) as e:
        build_app._demo_fired_patch({"flags": {"package": "p"}, "build": {}})
    assert "build.demo_fired" in str(e.value)


def test_demo_fired_needs_the_flag_shim(tmp_path):
    with pytest.raises(SystemExit) as e:
        build_app._demo_fired_patch({"build": {"demo_fired": {"file": "f", "find": "x"}}})
    assert "flags:" in str(e.value)
