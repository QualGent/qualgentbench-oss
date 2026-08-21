"""Device-state oracle for clean (task-completion) tasks: scored on GROUND TRUTH read
from the app's own storage, not self-report — blind guessing cannot fake real state.
Debug builds let `run-as` read the sandbox; a KNOWN-named artifact keeps the query deterministic."""

from __future__ import annotations

import re
import shlex
import os
import shutil
import sqlite3
import subprocess
import tempfile


def _adb(serial: str | None, *args: str, timeout: int = 30) -> tuple[int, str, str]:
    base = ["adb"] + (["-s", serial] if serial else [])
    p = subprocess.run([*base, *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _adb_bytes(serial: str | None, *args: str, timeout: int = 60) -> tuple[int, bytes, str]:
    """Like _adb but returns raw bytes (for pulling binary DB files via exec-out)."""
    base = ["adb"] + (["-s", serial] if serial else [])
    p = subprocess.run([*base, *args], capture_output=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr.decode(errors="replace").strip()


_SQLITE_MAGIC = b"SQLite format 3\x00"


def query_db(oracle: dict, pkg: str, serial: str | None = None,
             _attempt: int = 0) -> tuple[str | None, str]:
    """Evaluate the oracle's SQL against the app's SQLite DB; returns (value, detail).
    Never relies on an on-device sqlite3 binary (many images lack it): `run-as cat`
    pulls the DB plus its -wal so WAL writes are seen, then queries host-side."""
    db = oracle["db"]
    sql = oracle["query"]

    # Let the app flush before reading, and do NOT kill it first: apps write on
    # background executors, so the row can still be in flight when the episode ends —
    # a force-stop here once killed a pending insert and left no DB at all.
    import time as _time
    _time.sleep(1.5 if _attempt == 0 else 3.0)

    tmp = tempfile.mkdtemp(prefix="qgb_oracle_")
    try:
        # -shm is shared memory, rebuildable from -wal, and a stale copy can make SQLite
        # reject an otherwise-good pair. Pull the main file and the WAL only.
        for suffix in ("", "-wal"):
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
                    return None, (
                        f"no readable {db} in the app sandbox "
                        f"(not created, or app not debuggable): {shown}"
                    )
                with open(os.path.join(tmp, db), "wb") as fh:
                    fh.write(data)
            elif code == 0 and data.startswith(b"\x37\x7f"):  # WAL magic (big/little endian)
                with open(os.path.join(tmp, db + suffix), "wb") as fh:
                    fh.write(data)
        con = sqlite3.connect(os.path.join(tmp, db))  # host copy; WAL applied from sidecars
        try:
            row = con.execute(sql).fetchone()
        finally:
            con.close()
        return ("" if row is None else str(row[0])), "ok"
    except sqlite3.DatabaseError as exc:
        # Keep reporting what we actually pulled — this diagnostic is what identified the
        # "cat: databases/DB.db: No such file" payload masquerading as a database.
        try:
            _p = os.path.join(tmp, db)
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
