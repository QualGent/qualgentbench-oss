#!/usr/bin/env python3
"""One-time builder for a seeded-bug app: clones the upstream repo, applies the
spec's bugs as exact-string patches, and builds clean + buggy APKs into
dist/<id>/. Authoring-only; at runtime the benchmark uses the prebuilt APK."""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).resolve()
QGB = _SCRIPT.parents[1]                     # QualGentBench/
REPOS = _SCRIPT.parents[2]                   # QualGent-Repos/
BENCHMARKS = QGB / "src" / "qualgentbench" / "data" / "benchmarks"


def _java_home() -> str:
    jh = os.environ.get("JAVA_HOME")
    if jh and (Path(jh) / "bin/java").exists():
        return jh
    jbr = "/Applications/Android Studio.app/Contents/jbr/Contents/Home"
    if (Path(jbr) / "bin/java").exists():
        return jbr
    sys.exit("No JDK found. Set JAVA_HOME (Android Studio's JBR works).")


def _load_spec(app_id: str) -> dict:
    path = BENCHMARKS / f"{app_id}.yaml"
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
    (app_dir / "local.properties").write_text(f"sdk.dir={os.path.expanduser('~/Library/Android/sdk')}\n")
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


def _smoke(apk: Path, pkg: str, serial: str | None) -> bool:
    """Install, cold-launch, and assert the app is actually usable — "it compiled"
    is not "it runs", and a launch-crash otherwise costs a whole benchmark run."""
    dev = ["-s", serial] if serial else []

    def adb(*args: str, timeout: int = 120) -> tuple[int, str]:
        p = subprocess.run(["adb", *dev, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    print(f"  smoking {apk.name} on {serial or 'default device'} …")
    adb("uninstall", pkg)
    code, out = adb("install", "-r", "-g", str(apk))
    if code != 0 or "Success" not in out:
        print(f"    ✗ install failed: {out.strip()[:200]}")
        return False
    adb("logcat", "-c", "-b", "crash")
    adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(8)
    _, activities = adb("shell", "dumpsys", "activity", "activities")
    foreground = any(pkg in ln for ln in activities.splitlines() if "topResumedActivity" in ln)
    _, crash = adb("logcat", "-d", "-b", "crash")
    crashes = crash.count("FATAL EXCEPTION")
    if crashes or not foreground:
        first = next((ln for ln in crash.splitlines() if "Caused by" in ln or "Exception" in ln), "")
        print(f"    ✗ crashes={crashes} foreground={foreground} {first.strip()[:160]}")
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

    if args.emit_source:
        print(f"Emitting {app_id} buggy source ({len(setup)} setup + {len(patches)} bugs)…")
        out = emit_source(spec, app_dir, dist)
        print(f"  ✓ {out.relative_to(QGB)}")
        if not (args.clean or args.buggy):
            print(f"\nDone → {dist}")
            return

    java_home = _java_home()

    files = sorted({rel for rel, _, _ in (patches + setup)})
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
            _assemble(app_dir, build, java_home)
            _emit(app_dir, build, dist, "buggy.apk")
    finally:
        _restore(app_dir, snap)  # leave the checkout pristine
        if shim and shim.exists():
            shim.unlink()        # the generated shim is a build artifact, not source

    if args.smoke is not None:
        pkg = str(spec["app"].get("package") or "")
        if not pkg:
            sys.exit("--smoke needs app.package in the spec")
        serial = args.smoke or None
        built = [n for n, want in (("clean.apk", do_clean), ("buggy.apk", do_buggy)) if want]
        failed = [n for n in built if not _smoke(dist / n, pkg, serial)]
        if failed:
            sys.exit(f"runtime smoke FAILED for {', '.join(failed)} — do not author against this build")

    print(f"\nDone → {dist}")


if __name__ == "__main__":
    main()
