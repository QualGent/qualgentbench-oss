"""Device-state oracle for clean (task-completion) tasks: scored on GROUND TRUTH read
from the app's own storage, not self-report — blind guessing cannot fake real state.
Debug builds let `run-as` read the sandbox; a KNOWN-named artifact keeps the query deterministic."""

from __future__ import annotations

import json
import re
import shlex
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile


def _adb_bin() -> str:
    """Same tunnel every other adb caller uses (verify/device.py, frame_capture.py):
    in Docker the client is reached through a wrapper, and an oracle that hardcoded
    `adb` would read a different device than the replay it belongs to."""
    return os.environ.get("QGB_ADB_PATH") or "adb"


def _adb(serial: str | None, *args: str, timeout: int = 30) -> tuple[int, str, str]:
    base = [_adb_bin()] + (["-s", serial] if serial else [])
    p = subprocess.run([*base, *args], capture_output=True, timeout=timeout)
    return (p.returncode, p.stdout.decode("utf-8", "replace").strip(),
            p.stderr.decode("utf-8", "replace").strip())


def _adb_bytes(serial: str | None, *args: str, timeout: int = 60) -> tuple[int, bytes, str]:
    """Like _adb but returns raw bytes (for pulling binary DB files via exec-out)."""
    base = [_adb_bin()] + (["-s", serial] if serial else [])
    p = subprocess.run([*base, *args], capture_output=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr.decode(errors="replace").strip()


_SQLITE_MAGIC = b"SQLite format 3\x00"


# ── the device's timezone ─────────────────────────────────────────────────────
#
# Every oracle and fixture that says 'localtime' means the DEVICE's local day: the
# harness pins the emulator to this zone (`episode_runner.pin_device_timezone`) and the
# answer keys were derived in it. A query evaluated by the HOST's sqlite reads TZ from
# the host process, so `date('now','localtime')` in an oracle would follow whatever
# zone the machine running the harness sits in — a case that holds in Chicago at 20:00
# fails in Berlin, where it is already tomorrow. The C library reads TZ once per
# process, so the pinned zone is applied in a child interpreter (`_sqlite_child`),
# never by mutating this one.

DEVICE_TIMEZONE = os.environ.get("QGB_DEVICE_TIMEZONE") or "America/Chicago"
_TZ_PROP = "persist.sys.timezone"
_ZONE_RE = re.compile(r"^[A-Za-z0-9_+\-/]+$")
_zone_cache: dict[str | None, str] = {}


def device_timezone(serial: str | None = None) -> str:
    """The zone the device's clock renders in: `getprop persist.sys.timezone`, read
    once per serial and cached; DEVICE_TIMEZONE (the harness's own pin) when the
    property is empty or unreadable."""
    if serial not in _zone_cache:
        code, out, _err = _adb(serial, "shell", "getprop", _TZ_PROP)
        got = out.strip().splitlines()[0].strip() if out.strip() else ""
        _zone_cache[serial] = got if code == 0 and _ZONE_RE.match(got) else DEVICE_TIMEZONE
    return _zone_cache[serial]


# ── the device's clock ────────────────────────────────────────────────────────
#
# The same argument, one level down (QUA-2781). The harness pins the device CLOCK to a
# fixed instant (`episode_runner.pin_device_clock`), so the device's "now" is not the
# host's: an oracle's `date('now','localtime')` evaluated by the host's sqlite would be
# the host's calendar day, a week away from the day the app showed the agent, and a
# fixture's `strftime('%s','now')` would stamp rows on a day the app never displays
# (MedTimer's Overview shows today's events only). So a `'now'` literal in fixture and
# oracle SQL is replaced by the DEVICE's current UTC time, read off the device when the
# statement runs. SQLite reads a 'YYYY-MM-DD HH:MM:SS' time value as UTC, exactly as it
# reads 'now', so every modifier after it ('localtime', 'start of day', '+1 day') keeps
# its meaning. Only the quoted literal is touched; a device that cannot be read leaves
# the SQL as written (the host's clock), which is what it did before the pin.

_NOW_LITERAL = re.compile(r"'now'", re.IGNORECASE)


def device_now_utc(serial: str | None = None) -> str | None:
    """The device's clock as a SQLite UTC time value ('YYYY-MM-DD HH:MM:SS'), or None."""
    try:
        code, out, _err = _adb(serial, "shell", "date -u '+%Y-%m-%d %H:%M:%S'")
    except (OSError, subprocess.SubprocessError):
        return None
    got = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return got if code == 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", got) else None


def at_device_now(sql: str, now_utc: str | None) -> str:
    """`sql` with every `'now'` literal replaced by `now_utc` (unchanged when None)."""
    if not now_utc:
        return sql
    return _NOW_LITERAL.sub(f"'{now_utc}'", sql)


def _sqlite_child(src: str, local: str, stdin: str, tz: str | None,
                  timeout: int = 120) -> subprocess.CompletedProcess:
    """Run `src` (a sqlite program over the host copy `local`, fed `stdin`) in a CHILD
    interpreter with TZ pinned to `tz`. The C library reads TZ once per process, so
    this is the only way to make 'localtime' mean the device's zone without changing
    our own."""
    env = dict(os.environ)
    if tz:
        env["TZ"] = tz
    return subprocess.run([sys.executable, "-c", src, local], input=stdin.encode(),
                          capture_output=True, env=env, timeout=timeout)


_QUERY_SRC = r"""
import json, sqlite3, sys
db, sql = sys.argv[1], sys.stdin.read()
try:
    con = sqlite3.connect(db)
    try:
        row = con.execute(sql).fetchone()
    finally:
        con.close()
except sqlite3.DatabaseError as exc:
    print(json.dumps({"error": str(exc), "database_error": True}))
    sys.exit(2)
except Exception as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "database_error": False}))
    sys.exit(2)
print(json.dumps({"value": None if row is None else str(row[0])}))
"""


def _query_script(local: str, sql: str, tz: str | None) -> str:
    """One `fetchone` over the host copy under the device's zone; the first column as
    text ("" when there is no row). A sqlite failure is re-raised as
    sqlite3.DatabaseError so `query_db` keeps its torn-page retry; anything else as
    RuntimeError."""
    p = _sqlite_child(_QUERY_SRC, local, sql, tz)
    out = p.stdout.decode(errors="replace").strip()
    try:
        doc = json.loads(out.splitlines()[-1]) if out else {}
    except ValueError:
        doc = {}
    if p.returncode == 0 and "value" in doc:
        return "" if doc["value"] is None else str(doc["value"])
    msg = (doc.get("error") or out or p.stderr.decode(errors="replace").strip()[-200:]
           or f"rc={p.returncode}")
    if doc.get("database_error"):
        raise sqlite3.DatabaseError(msg)
    raise RuntimeError(msg)

# Seconds to let the app's background writers flush before the file is pulled; the
# second value is the longer settle the single retry uses. Module-level so a test (and
# `doctor`) can exercise the oracle without paying a wait that only a live app needs.
_SETTLE_S = (1.5, 3.0)


# Why a pull produced no database, classified ONCE here — where the shell's payload is
# still in hand — and carried in the detail as a leading phrase. A caller that must
# tell "nothing staged yet" from "this device can never read that app's database" reads
# the prefix rather than re-sniffing the payload: the explanatory half of these
# messages says "not debuggable" itself, so a substring search over the whole detail
# reads every miss as a refusal.
_UNREADABLE_PREFIX = "no readable "
_DENIED_PREFIX = "sandbox refused "

# How the shell says the read was REFUSED rather than finding nothing there.
_DENIED = ("not debuggable", "unknown package", "permission denied", "exec failed",
           "operation not permitted", "inaccessible or not found")


def db_unreadable(detail: str) -> bool:
    """True when a db oracle failed for want of a database file, either way round.
    `doctor` uses this to warn ("the app has no database yet") instead of failing,
    while every other failure stays a real failure."""
    return detail.startswith((_UNREADABLE_PREFIX, _DENIED_PREFIX))


def db_denied(detail: str) -> bool:
    """True when the pull was REFUSED rather than simply finding nothing: run-as could
    not enter the sandbox at all (release build, unknown package, no permission). No
    amount of staging fixes that, so `doctor` must fail rather than warn."""
    return detail.startswith(_DENIED_PREFIX)


def query_db(oracle: dict, pkg: str, serial: str | None = None,
             _attempt: int = 0) -> tuple[str | None, str]:
    """Evaluate the oracle's SQL against the app's SQLite DB; returns (value, detail).
    Never relies on an on-device sqlite3 binary (many images lack it): `run-as cat`
    pulls the DB plus its -wal so WAL writes are seen, then queries host-side.

    The query runs under the DEVICE's timezone (`device_timezone`, in a child
    interpreter with TZ set — the same mechanism `apply_sql` uses for fixtures).
    Oracles are written against the device's day: `date('now','localtime')` and
    `strftime('%H', ..., 'localtime')` in a check mean the day and hour the app showed
    the agent, and the app's clock is the pinned emulator zone, not the host's."""
    db = oracle["db"]
    sql = oracle["query"]
    local = os.path.basename(db)

    # Let the app flush before reading, and do NOT kill it first: apps write on
    # background executors, so the row can still be in flight when the episode ends —
    # a force-stop here once killed a pending insert and left no DB at all.
    import time as _time
    _time.sleep(_SETTLE_S[0] if _attempt == 0 else _SETTLE_S[1])

    tmp = tempfile.mkdtemp(prefix="qgb_oracle_")
    try:
        # -shm is shared memory, rebuildable from -wal, and a stale copy can make SQLite
        # reject an otherwise-good pair. Pull the main file and the WAL only.
        for suffix in ("", "-wal"):
            if db.startswith("/"):
                remote = f"cat {shlex.quote(db + suffix)}"
            else:
                remote = f"run-as {shlex.quote(pkg)} cat {shlex.quote('databases/' + db + suffix)}"
            code, data, err = _adb_bytes(serial, "exec-out", remote)
            if suffix == "":
                # exec-out folds the shell's stderr into stdout and still exits 0, so a
                # missing file arrives as a short "cat: ...: No such file" payload. Trust
                # the SQLite magic, never the exit code.
                if code != 0 or not data or not data.startswith(_SQLITE_MAGIC):
                    if _attempt == 0:
                        return query_db(oracle, pkg, serial, _attempt=1)
                    shown = err or data[:80].decode(errors="replace") or "empty"
                    if any(s in shown.lower() for s in _DENIED):
                        return None, (
                            f"{_DENIED_PREFIX}{db} — run-as cannot enter this app's "
                            f"sandbox (release build, or wrong package): {shown}"
                        )
                    return None, (
                        f"{_UNREADABLE_PREFIX}{db} in the app sandbox "
                        f"(not created yet): {shown}"
                    )
                with open(os.path.join(tmp, local), "wb") as fh:
                    fh.write(data)
            elif code == 0 and data.startswith(b"\x37\x7f"):  # WAL magic (big/little endian)
                with open(os.path.join(tmp, local + suffix), "wb") as fh:
                    fh.write(data)
        # Host copy, WAL applied from the sidecars — evaluated under the DEVICE's zone,
        # so 'localtime' in the oracle is the device's day (see `device_timezone`).
        # And 'now' is the DEVICE's instant, which the harness pinned (`at_device_now`).
        value = _query_script(os.path.join(tmp, local),
                              at_device_now(sql, device_now_utc(serial)),
                              device_timezone(serial))
        return value, "ok"
    except sqlite3.DatabaseError as exc:
        # Keep reporting what we actually pulled — this diagnostic is what identified the
        # "cat: databases/DB.db: No such file" payload masquerading as a database.
        try:
            _p = os.path.join(tmp, local)
            _size = os.path.getsize(_p)
            with open(_p, "rb") as _fh:
                _head = _fh.read(16)
            _diag = f" [pulled {_size}B, header={_head!r}]"
        except OSError:
            _diag = " [pulled file missing]"
        # A genuinely torn page can still happen if the pull lands mid-checkpoint, so
        # retry once with a longer settle before giving up.
        if _attempt == 0:
            return query_db(oracle, pkg, serial, _attempt=1)
        return None, f"db oracle error: {exc}{_diag}"
    except Exception as exc:  # noqa: BLE001 - surface any pull/sqlite failure
        return None, f"db oracle error: {exc}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def compare(value: str, expect: str) -> bool:
    """Compare a sqlite3 result against an expectation. Numeric ops: >=N, >N, <=N, <N,
    ==N/=N; anything else is string equality against the literal (leading '=' stripped)."""
    expect = expect.strip()
    m = re.match(r"^(>=|<=|==|>|<|=)?\s*(-?\d+)$", expect)
    if m and value.strip().lstrip("-").isdigit():
        op = m.group(1) or ">="
        lhs, rhs = int(value), int(m.group(2))
        return {
            ">=": lhs >= rhs, "<=": lhs <= rhs, ">": lhs > rhs,
            "<": lhs < rhs, "==": lhs == rhs, "=": lhs == rhs,
        }[op]
    return value.strip() == expect.lstrip("=").strip()


def screen_texts(serial: str | None = None) -> tuple[list[str], str]:
    """Dump the current UI and return visible text/content-desc strings, for DB-less
    apps that assert on-screen state. Bounds are never read — coordinate digits
    must not cause a false match."""
    code, out, err = _adb(serial, "shell", "uiautomator", "dump", "/sdcard/qg_ui.xml")
    if code != 0:
        return [], f"uiautomator dump failed: {err or out}"
    code, xml, err = _adb(serial, "exec-out", "cat", "/sdcard/qg_ui.xml")
    if code != 0 or not xml:
        return [], f"could not read UI dump: {err or 'empty'}"
    texts = re.findall(r'text="([^"]*)"', xml) + re.findall(r'content-desc="([^"]*)"', xml)
    return [t for t in texts if t], "ok"


def check_ui(oracle: dict, serial: str | None = None) -> tuple[bool, str]:
    """UI oracle: pass iff `contains` appears in some visible text node."""
    needle = str(oracle.get("contains", ""))
    texts, detail = screen_texts(serial)
    if detail != "ok":
        return False, detail
    ok = any(needle in t for t in texts) if needle else False
    shown = ", ".join(repr(t) for t in texts[:12])
    return ok, f"ui contains {needle!r} → {'PASS' if ok else 'FAIL'} (screen: {shown})"


def read_prefs(oracle: dict, pkg: str, serial: str | None = None) -> tuple[str | None, str]:
    """Read a SharedPreferences XML file from the app sandbox via run-as. For DB-less
    apps that persist state in prefs (e.g. a calculator's history JSON)."""
    fname = oracle.get("file") or f"{pkg}_preferences.xml"
    remote = f"run-as {shlex.quote(pkg)} cat {shlex.quote('shared_prefs/' + fname)}"
    code, out, err = _adb(serial, "shell", remote)
    if code != 0 or err:
        return None, f"prefs read error: {err or out or 'rc=' + str(code)}"
    return out, "ok"


def check_prefs(oracle: dict, pkg: str, serial: str | None = None) -> tuple[bool, str]:
    """Prefs oracle: pass iff `contains` appears in the prefs file (e.g. a saved
    calculation result in the history JSON)."""
    needle = str(oracle.get("contains", ""))
    xml, detail = read_prefs(oracle, pkg, serial)
    if xml is None:
        return False, detail
    ok = (needle in xml) if needle else False
    return ok, f"prefs contains {needle!r} → {'PASS' if ok else 'FAIL'}"


def check_file(oracle: dict, pkg: str, serial: str | None = None) -> tuple[bool, str]:
    """Filesystem oracle for file-backed apps. `name` matches a directory listing,
    `contains` reads the file, `absent: true` inverts the check (proves a delete).
    Shared storage needs no run-as; sandbox paths fall back to run-as."""
    path = str(oracle.get("path", "")).strip()
    if not path:
        return False, "file oracle: no `path`"
    needle = oracle.get("contains")
    name = oracle.get("name")
    want_absent = bool(oracle.get("absent"))

    if needle is not None:
        cmd = f"cat {shlex.quote(path)}"
    else:
        cmd = f"ls -a {shlex.quote(path)}"
    code, out, err = _adb(serial, "shell", cmd)
    if code != 0 and not out:
        # Sandbox path: retry through the app's own uid (debug build only).
        code, out, err = _adb(serial, "shell", f"run-as {shlex.quote(pkg)} {cmd}")
    if code != 0 and not out:
        found = False
        detail = err or out or f"rc={code}"
    else:
        if needle is not None:
            found = str(needle) in out
        else:
            # Substring, not exact-token: apps decide their own extensions, so match
            # the stem and accept whatever suffix the app adds.
            found = str(name) in out
        detail = ""
    ok = (not found) if want_absent else found
    what = f"contains {needle!r}" if needle is not None else f"entry {name!r}"
    suffix = f" [{detail}]" if detail else ""
    return ok, (f"file[{path}] {'absent' if want_absent else ''} {what} → "
                f"{'PASS' if ok else 'FAIL'}{suffix}")


def check_content(oracle: dict, pkg: str, serial: str | None = None) -> tuple[bool, str]:
    """ContentProvider oracle for state OUTSIDE the sandbox (contacts, calendar,
    media) via `adb shell content query` — image-agnostic, no run-as. `contains`
    is a substring over the rows; otherwise `expect` compares the row count."""
    uri = str(oracle.get("uri", "")).strip()
    if not uri:
        return False, "content oracle: no `uri`"
    cmd = ["shell", "content", "query", "--uri", uri]
    if oracle.get("projection"):
        cmd += ["--projection", str(oracle["projection"])]
    if oracle.get("where"):
        cmd += ["--where", shlex.quote(str(oracle["where"]))]
    code, out, err = _adb(serial, *cmd)
    if code != 0:
        return False, f"content query error: {err or out or 'rc=' + str(code)}"
    if "No result found" in out:
        rows: list[str] = []
    else:
        rows = [ln for ln in out.splitlines() if ln.strip().startswith("Row:")]
    needle = oracle.get("contains")
    if needle is not None:
        ok = str(needle) in out
        return ok, f"content[{uri}] contains {needle!r} → {'PASS' if ok else 'FAIL'}"
    expect = str(oracle.get("expect", ">=1"))
    ok = compare(str(len(rows)), expect)
    return ok, f"content[{uri}] rows={len(rows)} (expect {expect}) → {'PASS' if ok else 'FAIL'}"


def check(oracle: dict, pkg: str, serial: str | None = None) -> tuple[bool, str]:
    """Run + evaluate an oracle by `kind`: db (default), prefs, file, content, or ui
    (flaky on some emulators — prefer the others). Returns (passed, detail)."""
    kind = str(oracle.get("kind", "db")).lower()
    if kind == "ui":
        return check_ui(oracle, serial)
    if kind == "prefs":
        return check_prefs(oracle, pkg, serial)
    if kind == "file":
        return check_file(oracle, pkg, serial)
    if kind == "content":
        return check_content(oracle, pkg, serial)
    value, detail = query_db(oracle, pkg, serial)
    if value is None:
        return False, detail
    expect = str(oracle.get("expect", ">=1"))
    ok = compare(value, expect)
    return ok, f"db[{oracle['query']}] = {value!r} (expect {expect}) → {'PASS' if ok else 'FAIL'}"


# ── Writing seeded rows INTO an app database (the `sql:` device_setup step) ─────
#
# The read side above never trusts an on-device `sqlite3` binary, and the write side
# must not either: Google Play system images ship none, so a fixture written as
# `run-as <pkg> sqlite3 databases/<db> "insert ..."` exits with "run-as: exec failed
# for sqlite3" and seeds NOTHING — silently, when the shell step's exit code is not
# checked. The corpus lost `medtimer-skip-logged-dose` on every arm that way: the two
# ReminderEvent rows the case taps were never inserted.
#
# `apply_sql` is the same round trip in the other direction: pull the file (+ its
# -wal), run the statements with the HOST's sqlite3 in one transaction, checkpoint so
# the main file is self-contained, write it back through the app's own uid and remove
# the stale -wal/-shm so the app opens the pushed file and nothing else.

_DEVICE_TMP = "/data/local/tmp"


class SqlFixtureError(RuntimeError):
    """A `sql:` fixture could not be applied: the sandbox refused `run-as`, the
    database is not there yet, a statement failed, or the write-back did not
    verify. The message says which; the caller turns it into a staging failure."""


def _remote_db(db: str, pkg: str) -> tuple[str, str]:
    """(path on the device, `run-as <pkg> ` prefix or "" for a shell-readable
    absolute path) — the same two shapes `query_db` reads."""
    if db.startswith("/"):
        return db, ""
    return "databases/" + db, f"run-as {shlex.quote(pkg)} "


def _pull_db(db: str, pkg: str, serial: str | None, dest_dir: str) -> str:
    """Pull the database and its -wal into `dest_dir` (main file named after the db).
    Returns "" or the classified reason the pull failed — the same prefixes
    `db_unreadable`/`db_denied` read, so a refusal is told apart from "not created
    yet". Mirrors `query_db`'s pull: exec-out folds stderr into stdout and exits 0, so
    only the SQLite magic says whether a database arrived."""
    remote, prefix = _remote_db(db, pkg)
    local = os.path.join(dest_dir, os.path.basename(db))
    for suffix in ("", "-wal"):
        code, data, err = _adb_bytes(serial, "exec-out",
                                     f"{prefix}cat {shlex.quote(remote + suffix)}")
        if suffix == "":
            if code != 0 or not data or not data.startswith(_SQLITE_MAGIC):
                shown = err or data[:80].decode(errors="replace") or "empty"
                if any(s in shown.lower() for s in _DENIED):
                    return (f"{_DENIED_PREFIX}{db} — run-as cannot enter this app's "
                            f"sandbox (release build, or wrong package): {shown}")
                return f"{_UNREADABLE_PREFIX}{db} in the app sandbox (not created yet): {shown}"
            with open(local, "wb") as fh:
                fh.write(data)
        elif code == 0 and data.startswith(b"\x37\x7f"):
            with open(local + suffix, "wb") as fh:
                fh.write(data)
    return ""


def _push_db(db: str, pkg: str, serial: str | None, local: str) -> str:
    """Write `local` over the device's copy through the app's own uid and drop the
    sidecars: `adb push` cannot enter the sandbox, but `run-as <pkg> sh -c 'cat >'`
    can (this is how every `push:` fixture already lands its seed). Returns "" or
    the reason."""
    remote, prefix = _remote_db(db, pkg)
    staging = f"{_DEVICE_TMP}/qgb-sql-{os.path.basename(db)}"
    code, out, err = _adb(serial, "push", local, staging)
    if code != 0:
        return f"push to {staging} failed: {err or out}"
    q_remote = shlex.quote(remote)
    cmd = (f"cat {shlex.quote(staging)} | {prefix}sh -c {shlex.quote(f'cat > {q_remote}')} && "
           f"{prefix}sh -c {shlex.quote(f'rm -f {q_remote}-wal {q_remote}-shm')}; "
           f"rc=$?; rm -f {shlex.quote(staging)}; exit $rc")
    code, out, err = _adb(serial, "shell", cmd)
    if code != 0 or "run-as:" in out or "run-as:" in err:
        return f"write-back of {db} refused: {err or out or 'rc=' + str(code)}"
    return ""


def _statements_of(spec: dict, repo_root: str | None) -> str:
    """The fixture's SQL as one script: `statements:` (a string or a list) and/or
    `file:` (repo-relative). Kept verbatim — the host's sqlite3 parses it."""
    parts: list[str] = []
    raw = spec.get("statements")
    if isinstance(raw, str):
        parts.append(raw)
    elif isinstance(raw, (list, tuple)):
        parts.extend(str(s) for s in raw)
    elif raw is not None:
        raise SqlFixtureError(f"sql: `statements` must be a string or a list, got {type(raw).__name__}")
    if spec.get("file"):
        path = os.path.join(repo_root or "", str(spec["file"]))
        if not os.path.exists(path):
            raise SqlFixtureError(f"sql: file not found: {path}")
        with open(path, encoding="utf-8") as fh:
            parts.append(fh.read())
    script = "\n".join(p.strip() for p in parts if p and p.strip())
    if not script:
        raise SqlFixtureError("sql: no statements (give `statements:` or `file:`)")
    return script


# Runs in a CHILD interpreter so the device's timezone can be pinned through TZ for
# this script alone: `datetime('now','localtime')` in a fixture means the DEVICE's
# local day (tasksorg's "today 18:00"), and the C library reads TZ once per process.
_APPLY_SRC = r"""
import sqlite3, sys
db, script = sys.argv[1], sys.stdin.read()
con = sqlite3.connect(db, isolation_level=None)
try:
    con.executescript("BEGIN;\n" + script + "\n;COMMIT;")
except Exception as exc:
    if con.in_transaction:
        con.execute("ROLLBACK")
    print(f"{type(exc).__name__}: {exc}")
    sys.exit(2)
changes = con.total_changes
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
ok = con.execute("PRAGMA integrity_check").fetchone()[0]
con.close()
if ok != "ok":
    print(f"integrity_check after applying: {ok}")
    sys.exit(3)
print(changes)
"""


def _apply_script(local: str, script: str, tz: str | None) -> int:
    """Apply `script` to the host copy in one transaction; returns the row count
    changed. Raises SqlFixtureError with sqlite's own message on a bad statement."""
    p = _sqlite_child(_APPLY_SRC, local, script, tz)
    out = p.stdout.decode(errors="replace").strip()
    if p.returncode != 0:
        raise SqlFixtureError(f"sql: statement failed: {out or p.stderr.decode(errors='replace')[-200:]}")
    return int(out or 0)


def apply_sql(spec: dict, serial: str | None = None, *, tz: str | None = None,
              repo_root: str | None = None) -> str:
    """Apply a `sql:` fixture — `{package, db, statements | file}` — to the app's
    database on the device, host-side. Returns a one-line summary; raises
    SqlFixtureError with the reason otherwise (the caller classifies it as staging
    failure, never as agent failure).

    Steps, each verified: force-stop the app (a live connection would ignore or
    corrupt a file swapped under it); pull main + -wal; apply in one transaction
    with `PRAGMA wal_checkpoint(TRUNCATE)` after, so the main file carries every
    row; write back via `run-as … cat >` and delete the device's -wal/-shm; pull
    again and require byte equality plus `PRAGMA integrity_check` = ok."""
    pkg = str(spec.get("package") or "").strip()
    db = str(spec.get("db") or "").strip()
    if not pkg or not db:
        raise SqlFixtureError("sql: needs `package:` and `db:`")
    script = _statements_of(spec, repo_root)
    tmp = tempfile.mkdtemp(prefix="qgb_sql_")
    try:
        _adb(serial, "shell", f"am force-stop {shlex.quote(pkg)}")
        failed = _pull_db(db, pkg, serial, tmp)
        if failed:
            raise SqlFixtureError(f"sql: {failed}")
        local = os.path.join(tmp, os.path.basename(db))
        # A fixture's 'now' is the device's pinned instant, not the host's (QUA-2781).
        changes = _apply_script(local, at_device_now(script, device_now_utc(serial)), tz)
        # After the checkpoint the -wal is empty and sqlite removed it on close; a
        # leftover would mean the checkpoint did not run, and a pushed main file
        # would then be missing the rows.
        for suffix in ("-wal", "-shm"):
            if os.path.exists(local + suffix) and os.path.getsize(local + suffix):
                raise SqlFixtureError(f"sql: {db}{suffix} still holds frames after checkpoint")
        with open(local, "rb") as fh:
            pushed = fh.read()
        failed = _push_db(db, pkg, serial, local)
        if failed:
            raise SqlFixtureError(f"sql: {failed}")
        # Read it back through the same path the oracle will use.
        check_dir = os.path.join(tmp, "verify")
        os.mkdir(check_dir)
        failed = _pull_db(db, pkg, serial, check_dir)
        if failed:
            raise SqlFixtureError(f"sql: write-back of {db} could not be read back: {failed}")
        back = os.path.join(check_dir, os.path.basename(db))
        with open(back, "rb") as fh:
            got = fh.read()
        if got != pushed:
            raise SqlFixtureError(f"sql: write-back of {db} differs from what was pushed "
                                  f"({len(got)}B on device, {len(pushed)}B pushed)")
        con = sqlite3.connect(back)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        if ok != "ok":
            raise SqlFixtureError(f"sql: {db} on the device fails integrity_check: {ok}")
        return f"{db}: {changes} row(s) changed, {len(pushed)}B written back and verified"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
