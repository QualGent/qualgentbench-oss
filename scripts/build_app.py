#!/usr/bin/env python3
"""One-time builder for a seeded-bug app: clones the upstream repo, applies the
spec's bugs as exact-string patches, and builds clean + buggy APKs into
dist/<id>/. Authoring-only; at runtime the benchmark uses the prebuilt APK."""

from __future__ import annotations

import argparse
import asyncio
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).resolve()
QGB = _SCRIPT.parents[1]                     # QualGentBench/
REPOS = _SCRIPT.parents[2]                   # QualGent-Repos/

sys.path.insert(0, str(QGB / "src"))
from qualgentbench.verify.canary import fired_markers  # noqa: E402
from qualgentbench.verify.crash import _DEVICE_DATE_FMT, smoke_verdict  # noqa: E402
from qualgentbench.verify.device import _adb_bin, _pick_launch_activity  # noqa: E402


_ANDROID_STUDIO_JBR = "/Applications/Android Studio.app/Contents/jbr/Contents/Home"


def _java_home() -> str:
    jh = os.environ.get("JAVA_HOME")
    if jh and (Path(jh) / "bin/java").exists():
        return jh
    if (Path(_ANDROID_STUDIO_JBR) / "bin/java").exists():
        return _ANDROID_STUDIO_JBR
    sys.exit("No JDK found. Set JAVA_HOME (Android Studio's JBR works).")


# ── Toolchain preflight ──────────────────────────────────────
# These apps are pinned to a RELEASE TAG (`build.ref`), so their toolchain floor is
# whatever that release needed, not whatever the machine happens to have. Without this
# check a missing SDK platform surfaces ~90s in as a Gradle "Failed to find Platform
# SDK with path: platforms;android-37" — or, worse, as a Kotlin daemon error that
# names no platform at all — and a too-old JDK surfaces as an unrelated toolchain
# provisioning failure. Both are checked BEFORE the first Gradle invocation and the
# missing piece is named together with the command that installs it.

_SDK_ENV_VARS = ("ANDROID_SDK_ROOT", "ANDROID_HOME")
_DEFAULT_SDK_ROOT = "~/Library/Android/sdk"
_DEFAULT_MIN_JDK = 17

_JAVA_VERSION_RE = re.compile(r'version "(?:1\.)?(\d+)')
# `compileSdk = 37` / `compileSdkVersion 36`
_COMPILE_SDK_LITERAL_RE = re.compile(r"compileSdk(?:Version)?\s*=?\s*\(?\s*(\d+)")
# `compileSdk = project.libs.versions.app.build.compileSDKVersion.get().toInt()`
_COMPILE_SDK_CATALOG_RE = re.compile(
    r"compileSdk(?:Version)?\s*=?\s*\(?\s*(?:project\.)?libs\.versions\.([A-Za-z0-9_.]+?)\.get\(\)")
# `sourceCompatibility = JavaVersion.VERSION_21` / `JavaLanguageVersion.of(21)`
_JAVA_LEVEL_RE = re.compile(r"JavaVersion\.VERSION_(\d+)|JavaLanguageVersion\.of\((\d+)\)")
_JAVA_LEVEL_CATALOG_RE = re.compile(
    r"JavaVersion\.valueOf\(\s*(?:project\.)?libs\.versions\.([A-Za-z0-9_.]+?)\.get\(\)")


def android_sdk_root() -> Path:
    """ANDROID_SDK_ROOT / ANDROID_HOME, else the macOS default. One definition — the
    preflight and the `local.properties` Gradle reads must never disagree, or the
    check passes against one SDK and the build fails against another."""
    for var in _SDK_ENV_VARS:
        raw = (os.environ.get(var) or "").strip()
        if raw:
            return Path(raw).expanduser()
    return Path(_DEFAULT_SDK_ROOT).expanduser()


def _sdkmanager(sdk: Path) -> str:
    return str(sdk / "cmdline-tools/latest/bin/sdkmanager")


def installed_platforms(sdk: Path) -> list[str]:
    d = sdk / "platforms"
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []


def platform_dir_for(api: int, names: list[str]) -> str | None:
    """The installed platform directory that satisfies `compileSdk = api`, if any.

    Google ships API 37 as `platforms/android-37.0` (a dotted minor), while the build
    file asks for `37`. An exact-name check alone would report a platform that IS
    installed as missing, so a dotted suffix counts."""
    want = f"android-{api}"
    for name in names:
        if name == want or name.startswith(want + "."):
            return name
    return None


def _version_catalog(app_dir: Path) -> dict[str, str]:
    """`[versions]` of gradle/libs.versions.toml keyed the way build files spell it.
    Gradle maps `-` and `_` in an alias onto `.`, so the entry `app-build-javaVersion`
    is read as `libs.versions.app.build.javaVersion`."""
    toml = app_dir / "gradle" / "libs.versions.toml"
    if not toml.is_file():
        return {}
    out: dict[str, str] = {}
    in_versions = False
    for line in toml.read_text().splitlines():
        s = line.strip()
        if s.startswith("["):
            in_versions = s == "[versions]"
            continue
        if not in_versions or "=" not in s or s.startswith("#"):
            continue
        key, _, value = s.partition("=")
        value = value.strip().split("#")[0].strip().strip('"\'')
        out[key.strip().replace("-", ".").replace("_", ".")] = value
    return out


def _build_files(app_dir: Path) -> list[Path]:
    """The build files that can carry the toolchain floor: the app module's first
    (it wins), then the root project's."""
    candidates = [app_dir / "app" / "build.gradle.kts", app_dir / "app" / "build.gradle",
                  app_dir / "build.gradle.kts", app_dir / "build.gradle"]
    return [p for p in candidates if p.is_file()]


def detect_compile_sdk(app_dir: Path) -> int | None:
    catalog = _version_catalog(app_dir)
    for path in _build_files(app_dir):
        text = path.read_text()
        m = _COMPILE_SDK_CATALOG_RE.search(text)
        if m and (raw := catalog.get(m.group(1), "")).isdigit():
            return int(raw)
        if m := _COMPILE_SDK_LITERAL_RE.search(text):
            return int(m.group(1))
    return None


def detect_min_jdk(app_dir: Path) -> int | None:
    """The highest Java language level the build asks for — a Gradle Java toolchain of
    21 cannot be satisfied by a JDK 17 on PATH."""
    catalog = _version_catalog(app_dir)
    levels: list[int] = []
    for path in _build_files(app_dir):
        text = path.read_text()
        for m in _JAVA_LEVEL_RE.finditer(text):
            levels.append(int(m.group(1) or m.group(2)))
        for m in _JAVA_LEVEL_CATALOG_RE.finditer(text):
            raw = catalog.get(m.group(1), "").removeprefix("VERSION_")
            if raw.isdigit():
                levels.append(int(raw))
    return max(levels) if levels else None


def jdk_major(java_home: str) -> int | None:
    try:
        p = subprocess.run([str(Path(java_home) / "bin/java"), "-version"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _JAVA_VERSION_RE.search((p.stderr or "") + (p.stdout or ""))
    return int(m.group(1)) if m else None


def require_toolchain(app_id: str, build: dict, app_dir: Path, java_home: str) -> dict:
    """Assert this machine can build `app_id`, naming the missing piece and the exact
    command that installs it. `build.compile_sdk` / `build.jdk` in the spec are the
    declared floor; the checkout is only consulted when the spec is silent, so a spec
    stays authoritative even if upstream's build file moves the value around."""
    sdk = android_sdk_root()
    want_sdk = build.get("compile_sdk") or detect_compile_sdk(app_dir)
    want_jdk = build.get("jdk") or detect_min_jdk(app_dir) or _DEFAULT_MIN_JDK

    if not (sdk / "platforms").is_dir():
        sys.exit(
            f"No Android SDK at {sdk} (no platforms/ inside it).\n"
            f"  Point ANDROID_SDK_ROOT at a real SDK, or install one with Android Studio.")

    have_jdk = jdk_major(java_home)
    if have_jdk is None:
        sys.exit(f"Could not read a Java version from {java_home}/bin/java — is JAVA_HOME a JDK?")
    if have_jdk < int(want_jdk):
        hint = (f"  Android Studio ships one: JAVA_HOME={_ANDROID_STUDIO_JBR}\n"
                if Path(_ANDROID_STUDIO_JBR, "bin/java").exists() else "")
        sys.exit(
            f"{app_id} needs JDK {want_jdk}+ (its build sets that Java language level); "
            f"JAVA_HOME is JDK {have_jdk}.\n"
            f"  JAVA_HOME = {java_home}\n" + hint +
            "  Then re-run. Do NOT lower the app's Java level to fit the JDK.")

    platform = None
    if want_sdk:
        platform = platform_dir_for(int(want_sdk), installed_platforms(sdk))
        if platform is None:
            have = ", ".join(installed_platforms(sdk)) or "none"
            sys.exit(
                f"{app_id} needs Android SDK platform {want_sdk} (compileSdk {want_sdk} at "
                f"ref {build.get('ref') or 'HEAD'}); it is not installed.\n"
                f"  SDK root  = {sdk}   (override with ANDROID_SDK_ROOT)\n"
                f"  installed = {have}\n"
                f"  install it:\n"
                f"    {_sdkmanager(sdk)} 'platforms;android-{want_sdk}.0' "
                f"'build-tools;{want_sdk}.0.0'\n"
                f"  (run `{_sdkmanager(sdk)} --list | grep platforms\\;android-{want_sdk}` first — "
                f"Google may ship it only under a dotted or -beta name.)")

    print(f"  toolchain ok: JDK {have_jdk} (need {want_jdk}+), "
          f"platform {platform or 'unconstrained'} (need compileSdk {want_sdk or '?'}), SDK {sdk}")
    return {"sdk_root": str(sdk), "jdk": have_jdk, "jdk_required": int(want_jdk),
            "compile_sdk": int(want_sdk) if want_sdk else None, "platform": platform}


def _load_spec(app_id: str) -> dict:
    from qualgentbench import corpus
    path = corpus.spec_path(app_id)
    if not path.exists():
        sys.exit(f"No benchmark spec: {path}")
    return yaml.safe_load(path.read_text())


def _clone_if_needed(build: dict, app_dir: Path) -> None:
    if not app_dir.exists():
        repo = build.get("repo")
        if not repo:
            sys.exit(f"{app_dir} missing and no build.repo to clone from.")
        print(f"Cloning {repo} → {app_dir} …")
        subprocess.run(["git", "clone", "--depth", "1", repo, str(app_dir)], check=True)
        # Some apps keep code in git submodules; without them the build fails.
        subprocess.run(["git", "-C", str(app_dir), "submodule", "update", "--init", "--recursive"],
                       check=True)
    # Some repos ship gradlew without the exec bit.
    gradlew = app_dir / "gradlew"
    if gradlew.exists():
        gradlew.chmod(gradlew.stat().st_mode | 0o111)
    _checkout_ref(build, app_dir)


def _checkout_ref(build: dict, app_dir: Path) -> None:
    """Pin the checkout to build.ref (a release tag). A moving main can be broken
    against published library releases, so we build from tags, never branches."""
    ref = str(build.get("ref") or "").strip()
    if not ref:
        return
    head = subprocess.run(["git", "-C", str(app_dir), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    target = subprocess.run(["git", "-C", str(app_dir), "rev-parse", f"{ref}^{{commit}}"],
                            capture_output=True, text=True).stdout.strip()
    if target and target == head:
        print(f"  ref {ref} already checked out")
        return
    print(f"Checking out {ref} …")
    # A --depth 1 clone has no tags; fetch them before asking for one.
    subprocess.run(["git", "-C", str(app_dir), "fetch", "--tags", "--depth", "1", "origin"],
                   check=False, capture_output=True)
    res = subprocess.run(["git", "-C", str(app_dir), "checkout", "-q", ref], capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"could not check out ref {ref!r} in {app_dir}: {res.stderr.strip()}")
    subprocess.run(["git", "-C", str(app_dir), "submodule", "update", "--init", "--recursive"],
                   check=False)


def _patches(spec: dict) -> list[tuple[str, str, str]]:
    """Every patch site, flattened. A bug may declare patch: (one site) or
    patches: (several) — a feature often has multiple entry points, and gating
    only one leaves it working through the other, i.e. a dead bug."""
    out = []
    for bug in spec.get("bugs", []):
        sites = bug.get("patches") or ([bug["patch"]] if bug.get("patch") else [])
        for p in sites:
            out.append((p["file"], p["find"], p["replace"]))
    return out


def _setup_patches(spec: dict) -> list[tuple[str, str, str]]:
    """Patches applied to BOTH clean and buggy builds — e.g. seeding sample data so
    the app isn't empty. NOT bugs; they make every feature exercisable."""
    return [(p["file"], p["find"], p["replace"]) for p in spec.get("setup", [])]


# ── Per-episode bug activation ───────────────────────────────
# One gated APK gives a random live-bug subset per episode (anti-memorization) in 2
# builds, not 2^n. The shim reads a sandbox file: missing → all bugs, else listed ids.
_FLAG_SHIM = '''package {pkg}

import java.io.File

/** Test-harness bug gate — see the QualGentBench docs. */
object QgbFlags {{
    private const val PATH = "/data/data/{app_id}/files/qgb_flags.txt"
    private const val FIRED_DIR = "/data/data/{app_id}/files/.qgb/fired"

    @Volatile private var cache: Set<String>? = null
    @Volatile private var allOn: Boolean = true

    private fun load(): Set<String> {{
        cache?.let {{ return it }}
        val loaded = try {{
            val f = File(PATH)
            if (!f.exists()) {{
                allOn = true
                emptySet()
            }} else {{
                allOn = false
                f.readLines().map {{ it.trim() }}.filter {{ it.isNotEmpty() }}.toSet()
            }}
        }} catch (t: Throwable) {{
            allOn = true
            emptySet()
        }}
        cache = loaded
        return loaded
    }}

    @JvmStatic
    fun on(id: String): Boolean {{
        val ids = load()
        return allOn || ids.contains(id)
    }}

    /** Attribution canary: the seeded path for [id] executed. Call it on the line
     *  BEFORE the fault takes effect (a crash right after must still find the marker
     *  on disk — File.createNewFile is synchronous). Harness-read only, via run-as. */
    @JvmStatic
    fun fired(id: String) {{
        try {{
            val dir = File(FIRED_DIR)
            dir.mkdirs()
            File(dir, id).createNewFile()
        }} catch (t: Throwable) {{
        }}
    }}
}}
'''


# Java variant for apps built without the Kotlin plugin. Same contract:
# missing file → all bugs live, present → only listed ids.
_FLAG_SHIM_JAVA = """package {pkg};

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;

/** Test-harness bug gate — see the QualGentBench docs. */
public final class QgbFlags {{
    private static final String PATH = "/data/data/{app_id}/files/qgb_flags.txt";
    private static final String FIRED_DIR = "/data/data/{app_id}/files/.qgb/fired";

    private static volatile Set<String> cache = null;
    private static volatile boolean allOn = true;

    private QgbFlags() {{
    }}

    private static Set<String> load() {{
        Set<String> cached = cache;
        if (cached != null) {{
            return cached;
        }}
        Set<String> loaded = new HashSet<String>();
        BufferedReader reader = null;
        try {{
            File file = new File(PATH);
            if (!file.exists()) {{
                allOn = true;
            }} else {{
                allOn = false;
                reader = new BufferedReader(new FileReader(file));
                String line;
                while ((line = reader.readLine()) != null) {{
                    String trimmed = line.trim();
                    if (trimmed.length() > 0) {{
                        loaded.add(trimmed);
                    }}
                }}
            }}
        }} catch (Throwable t) {{
            allOn = true;
        }} finally {{
            if (reader != null) {{
                try {{
                    reader.close();
                }} catch (Throwable ignored) {{
                }}
            }}
        }}
        cache = Collections.unmodifiableSet(loaded);
        return cache;
    }}

    public static boolean on(String id) {{
        Set<String> ids = load();
        return allOn || ids.contains(id);
    }}

    /** Attribution canary: the seeded path for {{@code id}} executed. Call it on the
     *  line BEFORE the fault takes effect (a crash right after must still find the
     *  marker on disk — createNewFile is synchronous). Harness-read only, via run-as. */
    public static void fired(String id) {{
        try {{
            File dir = new File(FIRED_DIR);
            dir.mkdirs();
            new File(dir, id).createNewFile();
        }} catch (Throwable ignored) {{
        }}
    }}
}}
"""


def _flag_shim_path(spec: dict) -> str | None:
    """Repo-relative path of the generated shim, from the spec's `flags:` block."""
    flags = spec.get("flags")
    if not flags:
        return None
    return str(flags["file"])


def _write_flag_shim(spec: dict, tree: Path) -> Path | None:
    """Generate QgbFlags.kt into a build tree. Returns the written path (so the
    caller can delete it again — the canonical checkout must be left pristine)."""
    flags = spec.get("flags")
    if not flags:
        return None
    rel = _flag_shim_path(spec)
    app_id = str(spec["app"]["package"])
    out = tree / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    template = _FLAG_SHIM_JAVA if out.suffix == ".java" else _FLAG_SHIM
    out.write_text(template.format(pkg=flags["package"], app_id=app_id))
    return out


# ── Attribution-canary proof ─────────────────────────────────
# `QgbFlags.fired(id)` is generated above, but until 2026-09-15 no spec ever called it,
# so nothing proved the round trip: patch → compiled APK → marker file in the app
# sandbox → `verify.canary.fired_markers` reading it back over run-as. `--demo-fired`
# seeds ONE throwaway call at the spec's `build.demo_fired` anchor and reads the marker
# after the smoke launch. It is opt-in, and its APK is emitted under a DIFFERENT name
# (`buggy-demo-fired.apk`) so `publish_apk.py`, which publishes `buggy.apk`, cannot
# ship a demo marker by accident.
DEMO_FIRED_ID = "demo"
DEMO_APK_NAME = "buggy-demo-fired.apk"


def _demo_fired_patch(spec: dict) -> tuple[str, str, str]:
    """An extra patch site that calls `QgbFlags.fired("demo")` once. The call is written
    FULLY QUALIFIED so no import has to be edited into the file — one anchor, one line,
    nothing else to get wrong on an app whose activity lives in a sub-package."""
    anchor = (spec.get("build") or {}).get("demo_fired")
    if not anchor:
        sys.exit("--demo-fired needs a `build.demo_fired: {file, find}` anchor in the spec "
                 "(a line executed on a reachable path, e.g. the launcher activity's "
                 "super.onCreate).")
    if not spec.get("flags"):
        sys.exit("--demo-fired needs a `flags:` block — the shim it calls is generated from it.")
    pkg = str(spec["flags"]["package"])
    find = str(anchor["find"])
    return (str(anchor["file"]), find, f'{find}\n{pkg}.QgbFlags.fired("{DEMO_FIRED_ID}")')


def _read_fired(serial: str | None, pkg: str) -> list[str]:
    return asyncio.run(fired_markers(serial, pkg))


def _snapshot(app_dir: Path, files: list[str]) -> dict[str, str]:
    return {f: (app_dir / f).read_text() for f in files}


def _restore(app_dir: Path, snap: dict[str, str]) -> None:
    for f, text in snap.items():
        (app_dir / f).write_text(text)


def _enclosing_method(src_lines: list[str], idx: int) -> str:
    """The method a matched line sits in — names the ambiguity in the error message."""
    sig = re.compile(r"\s*(public|private|protected|internal|override|fun|static).*\(")
    for line in reversed(src_lines[: idx + 1]):
        if sig.match(line) and "(" in line:
            return line.strip()[:60]
    return "top level"


def _apply(app_dir: Path, patches: list[tuple[str, str, str]]) -> None:
    """Indentation-robust patcher: match source lines by stripped content, then
    re-indent `replace` to the matched block's indent. An anchor MUST match exactly
    once — taking the first of several silently seeds the defect in the wrong method."""
    for rel, find, replace in patches:
        path = app_dir / rel
        src_lines = path.read_text().split("\n")
        find_lines = find.split("\n")
        repl_lines = replace.split("\n")
        n = len(find_lines)
        target = [ln.strip() for ln in find_lines]

        hits = [i for i in range(len(src_lines) - n + 1)
                if [ln.strip() for ln in src_lines[i:i + n]] == target]
        if not hits:
            sys.exit(f"Patch anchor not found in {rel} (source drifted?):\n---\n{find[:200]}\n---")
        if len(hits) > 1:
            where = ", ".join(f"line {i + 1} ({_enclosing_method(src_lines, i)})" for i in hits)
            sys.exit(
                f"Patch anchor is AMBIGUOUS in {rel}: {len(hits)} matches — {where}.\n"
                f"The first would be patched, which is how a defect ends up in the wrong\n"
                f"method. Extend `find` upward until it is unique (usually to the\n"
                f"enclosing method signature).\n---\n{find[:200]}\n---"
            )
        idx = hits[0]

        first = src_lines[idx]
        base = first[: len(first) - len(first.lstrip())]   # the block's leading indent
        reindented = [(base + ln) if ln.strip() else "" for ln in repl_lines]
        src_lines[idx:idx + n] = reindented
        path.write_text("\n".join(src_lines))


# Authoring comments (`// BUG(<id>): …`) are invisible in an APK, but in emitted
# SOURCE they are the answer key — one grep would list every seeded bug.
_LEAK_MARKER = "// BUG("


def _sanitize_bug_markers(out: Path, patches: list[tuple[str, str, str]]) -> int:
    """Strip `// BUG(...)` comments (and their continuation comment lines) from
    the patched files in the emitted tree. Returns the number of lines cleaned."""
    removed = 0
    for rel in sorted({rel for rel, _, _ in patches}):
        path = out / rel
        cleaned: list[str] = []
        dropping = False
        for ln in path.read_text().split("\n"):
            stripped = ln.strip()
            if _LEAK_MARKER in ln:
                removed += 1
                if stripped.startswith("//"):
                    dropping = True   # comment-only marker: drop + continuations
                else:                 # code with a trailing marker: keep the code
                    cleaned.append(ln[: ln.index(_LEAK_MARKER)].rstrip())
                continue
            if dropping and stripped.startswith("//"):
                removed += 1          # continuation of the bug comment
                continue
            dropping = False
            cleaned.append(ln)
        path.write_text("\n".join(cleaned))
    return removed


def _scan_for_leaks(out: Path) -> list[str]:
    """Any file in the emitted tree still carrying the marker = a leak (e.g. a
    future spec used a comment style the sanitizer doesn't know)."""
    hits: list[str] = []
    for p in out.rglob("*"):
        if not p.is_file():
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if _LEAK_MARKER in text:
            hits.append(str(p.relative_to(out)))
    return hits


def emit_source(spec: dict, app_dir: Path, dist: Path) -> Path:
    """Write the patched buggy source tree to dist/<id>/buggy-src — the workspace
    handed to coding agents. .git is stripped so the agent can't diff upstream, and
    bug-authoring comments are sanitized out; the canonical checkout is untouched."""
    out = dist / "buggy-src"
    if out.exists():
        shutil.rmtree(out)
    ignore = shutil.ignore_patterns(
        ".git", ".github", "build", ".gradle", ".idea", "local.properties",
    )
    shutil.copytree(app_dir, out, ignore=ignore, symlinks=True)
    _apply(out, _setup_patches(spec))
    _write_flag_shim(spec, out)
    _apply(out, _patches(spec))
    for bug in spec.get("bugs", []):
        if bug.get("patch"):
            print(f"    injected {bug['id']} (source)")
    removed = _sanitize_bug_markers(out, _patches(spec))
    print(f"    sanitized {removed} bug-marker comment line(s)")
    leaks = _scan_for_leaks(out)
    if leaks:
        sys.exit(f"Bug markers still present in emitted source: {', '.join(leaks)}")
    return out


def _assemble(app_dir: Path, build: dict, java_home: str) -> None:
    env = {**os.environ, "JAVA_HOME": java_home}
    (app_dir / "local.properties").write_text(f"sdk.dir={android_sdk_root()}\n")
    task = build.get("gradle_task", ":app:assembleDebug")
    subprocess.run(["./gradlew", task, "--console=plain", "-q"],
                   cwd=str(app_dir), env=env, check=True)


def _emit(app_dir: Path, build: dict, dist: Path, name: str) -> None:
    matches = glob.glob(str(app_dir / build["apk_glob"]))
    if not matches:
        sys.exit(f"No APK matched {build['apk_glob']} under {app_dir}")
    dist.mkdir(parents=True, exist_ok=True)
    dest = dist / name
    shutil.copy2(matches[0], dest)
    print(f"  ✓ {dest.relative_to(QGB)}  ({dest.stat().st_size / 1e6:.1f} MB)")


_SINCE_RE = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$")


def _smoke(apk: Path, pkg: str, serial: str | None) -> bool:
    """Install, cold-launch, and assert the app is actually usable — "it compiled"
    is not "it runs", and a launch-crash otherwise costs a whole benchmark run.

    Crashes are attributed per process (qualgentbench.verify.crash): the crash
    buffer is device-wide and never cleared, so only crashes of `pkg`'s own
    processes logged after the launch fail the smoke; other processes' crashes are
    printed and ignored. exit-info is the second signal (catches ANRs and native
    deaths the buffer misses). No `logcat -c` — the buffer is shared."""
    dev = ["-s", serial] if serial else []

    def adb(*args: str, timeout: int = 120) -> tuple[int, str]:
        p = subprocess.run([_adb_bin(), *dev, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    print(f"  smoking {apk.name} on {serial or 'default device'} …")
    adb("uninstall", pkg)
    code, out = adb("install", "-r", "-g", str(apk))
    if code != 0 or "Success" not in out:
        print(f"    ✗ install failed: {out.strip()[:200]}")
        return False
    # Window start: taken BEFORE the launch, in the form `logcat -T` accepts. Second
    # precision (.000), so a crash in the same second as the launch is still inside.
    # ONE quoted word: `adb shell` re-parses argv on the device, and an unquoted
    # format reaches toybox as two arguments ("date: Max 1 argument"), which would
    # silently turn the window into an error string that matches nothing.
    _, since = adb("shell", f"date {shlex.quote(_DEVICE_DATE_FMT)}")
    since = since.strip()
    if not _SINCE_RE.match(since):
        print(f"    ✗ could not read the device clock for the crash window: {since[:80]}")
        return False
    # Cold-launch the way the harness does at RUNTIME: resolve the launcher activity
    # and `am start` it, with monkey only as the fallback. Plain monkey silently
    # launched nothing on a Play-image emulator (exit 251 after "SYS_KEYS has no
    # physical keys"), and the gate then failed a build that launches fine as "not the
    # resumed foreground activity" (2026-09-15). `verify.device.relaunch` had already
    # moved off monkey for the same reason; this is the same resolution, including
    # skipping a debug tool's second launcher (LeakCanary).
    _, brief = adb("shell", "cmd", "package", "resolve-activity", "--brief", pkg)
    activity = _pick_launch_activity(pkg, list(reversed(brief.splitlines())))
    if not activity:
        _, listed = adb("shell", "cmd", "package", "query-activities", "--brief",
                        "-a", "android.intent.action.MAIN",
                        "-c", "android.intent.category.LAUNCHER", pkg)
        activity = _pick_launch_activity(pkg, listed.splitlines())
    if activity:
        print(f"    launching {activity}")
        adb("shell", "am", "start", "-W", "-n", activity)
    else:
        print("    no launcher activity resolved — falling back to monkey")
        adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
    foreground, activities = False, ""
    for _ in range(8):
        time.sleep(1)
        _, activities = adb("shell", "dumpsys", "activity", "activities")
        foreground = any(pkg in ln for ln in activities.splitlines() if "topResumedActivity" in ln)
        if foreground:
            break
    _, crash = adb("logcat", "-d", "-b", "crash", "-v", "threadtime", "-T", since)
    _, exits = adb("shell", "dumpsys", "activity", "exit-info", pkg)
    ok, report = smoke_verdict(crash, exits, pkg, since)
    for ln in report.splitlines():
        print(f"    {ln}")
    if not ok or not foreground:
        if not foreground:
            print(f"    ✗ {pkg} is not the resumed foreground activity after launch")
        return False
    print("    ✓ launches clean")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app_id")
    ap.add_argument("--clean", action="store_true", help="build only the clean control")
    ap.add_argument("--buggy", action="store_true", help="build only the buggy APK")
    ap.add_argument("--emit-source", action="store_true",
                    help="write the patched buggy source tree to dist/<id>/buggy-src "
                         "(no build; combine with --clean/--buggy to also build APKs)")
    ap.add_argument("--smoke", nargs="?", const="", metavar="SERIAL",
                    help="after building, install each APK and assert it cold-launches "
                         "without crashing (optionally on a given adb serial)")
    ap.add_argument("--demo-fired", action="store_true",
                    help="seed one throwaway QgbFlags.fired(\"demo\") call at the spec's "
                         "build.demo_fired anchor, emit dist/<id>/buggy-demo-fired.apk, and "
                         "(with --smoke) read the marker back through verify/canary.py")
    ap.add_argument("--check-toolchain", action="store_true",
                    help="clone/checkout, report whether this machine can build the app "
                         "(SDK platform + JDK), and stop — no patching, no build")
    args = ap.parse_args()

    spec = _load_spec(args.app_id)
    app_id = spec["app"]["id"]
    build = spec.get("build")
    if not build:
        sys.exit(f"{args.app_id}.yaml has no build: block (can't build).")

    patches = _patches(spec)
    setup = _setup_patches(spec)
    if not patches:
        sys.exit(f"{args.app_id}.yaml has no bug patches.")

    # A bug with no task is injected but never scored — the agent can trip over it
    # elsewhere and be marked wrong for reporting it.
    bug_ids = {str(b["id"]) for b in spec.get("bugs", []) if b.get("patch") or b.get("patches")}
    tested = {str(t.get("bug_id")) for t in spec.get("tasks", []) if t.get("bug_id")}
    untested = sorted(bug_ids - tested)
    if untested:
        sys.exit(f"{args.app_id}.yaml seeds bugs with no task to find them: "
                 f"{', '.join(untested)} — add a task per bug or drop the patch.")

    app_dir = REPOS / build["dir"]
    dist = QGB / "dist" / app_id
    _clone_if_needed(build, app_dir)

    if args.check_toolchain:
        java_home = _java_home()
        print(f"Toolchain for {app_id} (ref {build.get('ref') or 'HEAD'}):")
        require_toolchain(app_id, build, app_dir, java_home)
        return

    if args.emit_source:
        print(f"Emitting {app_id} buggy source ({len(setup)} setup + {len(patches)} bugs)…")
        out = emit_source(spec, app_dir, dist)
        print(f"  ✓ {out.relative_to(QGB)}")
        if not (args.clean or args.buggy):
            print(f"\nDone → {dist}")
            return

    java_home = _java_home()
    # Before the first Gradle invocation: a missing SDK platform or a too-old JDK is a
    # 90-second failure with a message that names neither.
    require_toolchain(app_id, build, app_dir, java_home)

    extra = [_demo_fired_patch(spec)] if args.demo_fired else []
    buggy_name = DEMO_APK_NAME if args.demo_fired else "buggy.apk"
    files = sorted({rel for rel, _, _ in (patches + setup + extra)})
    snap = _snapshot(app_dir, files)  # the upstream-clean tree is the baseline

    do_clean = args.clean or not args.buggy
    do_buggy = args.buggy or not args.clean
    shim: Path | None = None
    try:
        if do_clean:
            print(f"Building {app_id} clean control ({len(setup)} setup patches)…")
            _restore(app_dir, snap)
            _apply(app_dir, setup)
            _assemble(app_dir, build, java_home)
            _emit(app_dir, build, dist, "clean.apk")
        if do_buggy:
            print(f"Building {app_id} buggy ({len(setup)} setup + {len(patches)} bugs)…")
            _restore(app_dir, snap)
            _apply(app_dir, setup)
            shim = _write_flag_shim(spec, app_dir)
            if shim:
                print(f"    flag gate → {shim.relative_to(app_dir)}")
            _apply(app_dir, patches)
            for bug in spec.get("bugs", []):
                sites = bug.get("patches") or ([bug["patch"]] if bug.get("patch") else [])
                if sites:
                    n = f" ({len(sites)} sites)" if len(sites) > 1 else ""
                    print(f"    injected {bug['id']}{n}")
            if args.demo_fired:
                demo = _demo_fired_patch(spec)
                _apply(app_dir, [demo])
                print(f"    injected the throwaway fired(\"{DEMO_FIRED_ID}\") canary "
                      f"at {demo[0]}")
            _assemble(app_dir, build, java_home)
            _emit(app_dir, build, dist, buggy_name)
    finally:
        _restore(app_dir, snap)  # leave the checkout pristine
        if shim and shim.exists():
            shim.unlink()        # the generated shim is a build artifact, not source

    if args.smoke is not None:
        pkg = str(spec["app"].get("package") or "")
        if not pkg:
            sys.exit("--smoke needs app.package in the spec")
        serial = args.smoke or None
        built = [n for n, want in (("clean.apk", do_clean), (buggy_name, do_buggy)) if want]
        failed = [n for n in built if not _smoke(dist / n, pkg, serial)]
        if failed:
            sys.exit(f"runtime smoke FAILED for {', '.join(failed)} — do not author against this build")
        if args.demo_fired and do_buggy:
            # _smoke uninstalls before installing, so the sandbox was empty at launch:
            # a marker here was written by THIS run, not left over from an earlier one.
            markers = _read_fired(serial, pkg)
            print(f"  fired markers after launch: {markers or 'none'}")
            if DEMO_FIRED_ID not in markers:
                sys.exit(f"--demo-fired: verify/canary.py read back {markers or 'nothing'} — the "
                         f"shim compiled but its marker did not reach "
                         f"/data/data/{pkg}/files/.qgb/fired/{DEMO_FIRED_ID}")
            print(f"  ✓ verify/canary.fired_markers read the {DEMO_FIRED_ID!r} marker back")

    print(f"\nDone → {dist}")


if __name__ == "__main__":
    main()
