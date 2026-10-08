"""The journey scorer's source, as one hash (QUA-2927, closure QUA-2934).

`tests/test_scorer_version.py` pins `journey.SCORER_VERSION` to `closure_sha256()`. What is
hashed is everything a journey verdict can depend on inside the scoring modules:

* every function and class reachable from the entry points (`ENTRY_POINTS`) through the
  scoring modules (`SCORING_MODULES`): a name or `module.attr` a reached body reads,
  resolved through that module's imports, including imports inside a function body. A
  class is hashed whole (every method) and its methods are followed too;
* every module-level constant a reached body (or a reached constant) reads, in ANY
  `qualgentbench` module, so a rule table defined elsewhere (`submission.LIVENESS_MODES`
  under `journey.DEVICE_ORACLE_MODES`) still counts.

Not followed: functions and classes outside the scoring modules (the transcript parser,
pricing, the corpus loader, the result dataclasses) and method calls on an object whose
class is not named in a reached body. A change there that can move a verdict still
needs a SCORER_VERSION bump by hand.

Each item is hashed as a version-independent rendering of its AST with docstrings
removed, so comments, docstrings and formatting never move the hash, while any change to
code or to a constant does. `ast.dump` is NOT used: its output differs across Python
3.11-3.14 (3.12 added `type_params`, 3.13 stopped printing empty fields). `render` prints
node fields in name order and leaves out every field that is None or empty, so a field a
newer Python adds with an empty default renders exactly as before.

Stdlib only and import-free (it parses the files, never imports the package), so the same
hash can be checked under every interpreter: `python3.11 tests/scorer_closure.py`.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE = "qualgentbench"
SRC = Path(__file__).resolve().parent.parent / "src" / PACKAGE

#: Where a verdict is decided. Functions/classes are followed only inside these modules.
SCORING_MODULES = ("journey", "bugs", "contamination", "interactions")
#: The scoring entry points, as (module, name).
ENTRY_POINTS = (("journey", "journey_verdict"),)
#: Read by the scorer but not part of the rules: the version label itself, so a bump
#: does not move the hash it is pinned to.
EXCLUDED = (("journey", "SCORER_VERSION"),)

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


# ── rendering ──────────────────────────────────────────────────────────────────

def _strip_docstrings(node: ast.AST) -> ast.AST:
    for n in ast.walk(node):
        if isinstance(n, (*_DEFS, ast.Module)) and n.body:
            first = n.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                n.body = n.body[1:] or [ast.Pass()]
    return node


def render(value: object) -> str:
    """A deterministic text form of an AST (sub)tree: `Type(field=..., ...)`, fields in
    name order, None/empty fields omitted, no positions. Constants render as
    `type:repr`, so 1, 1.0 and True stay distinct."""
    if isinstance(value, ast.AST):
        parts = []
        for name in sorted(value._fields):
            v = getattr(value, name, None)
            if v is None or (isinstance(v, list) and not v):
                continue
            parts.append(f"{name}={render(v)}")
        return f"{type(value).__name__}({', '.join(parts)})"
    if isinstance(value, list):
        return "[" + ", ".join(render(v) for v in value) + "]"
    return f"{type(value).__name__}:{value!r}"


# ── one module's symbol table ───────────────────────────────────────────────────

@dataclass
class _Module:
    name: str                                      # dotted, relative to the package
    defs: dict[str, ast.AST] = field(default_factory=dict)
    assigns: dict[str, list[ast.stmt]] = field(default_factory=dict)
    # alias -> ("module", mod) or ("attr", mod, attr); mod is package-relative.
    imports: dict[str, tuple] = field(default_factory=dict)


def _module_path(name: str, src: Path) -> Path | None:
    base = src.joinpath(*name.split("."))
    for p in (base.with_suffix(".py"), base / "__init__.py"):
        if p.is_file():
            return p
    return None


def _relative(mod: str | None, level: int, current: str, is_pkg: bool) -> str | None:
    """The package-relative module an import names, or None if outside the package."""
    if level == 0:
        if mod == PACKAGE:
            return ""
        if mod and mod.startswith(PACKAGE + "."):
            return mod[len(PACKAGE) + 1:]
        return None
    parts = current.split(".") if current else []
    if not is_pkg:
        parts = parts[:-1]
    parts = parts[:len(parts) - (level - 1)] if level > 1 else parts
    if mod:
        parts += mod.split(".")
    return ".".join(parts)


def _record_imports(stmts, table: dict[str, tuple], current: str, is_pkg: bool,
                    src: Path) -> None:
    for st in stmts:
        if isinstance(st, ast.ImportFrom):
            base = _relative(st.module, st.level, current, is_pkg)
            if base is None:
                continue
            for a in st.names:
                sub = f"{base}.{a.name}" if base else a.name
                if _module_path(sub, src):
                    table[a.asname or a.name] = ("module", sub)
                else:
                    table[a.asname or a.name] = ("attr", base, a.name)
        elif isinstance(st, ast.Import):
            for a in st.names:
                rel = _relative(a.name, 0, current, is_pkg)
                if rel is not None and a.asname:
                    table[a.asname] = ("module", rel)


def _targets(t: ast.AST):
    if isinstance(t, ast.Name):
        yield t.id
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            yield from _targets(e)
    elif isinstance(t, ast.Starred):
        yield from _targets(t.value)


class Closure:
    """The scoring closure over the package sources under `src`. `sources` overrides a
    module's text (package-relative dotted name -> source), which is how the tests edit a
    helper without touching the tree."""

    def __init__(self, src: Path = SRC, sources: dict[str, str] | None = None,
                 scoring: tuple[str, ...] = SCORING_MODULES,
                 entry_points: tuple[tuple[str, str], ...] = ENTRY_POINTS):
        self.src, self.sources = src, dict(sources or {})
        self.scoring, self.entry_points = set(scoring), entry_points
        self._modules: dict[str, _Module | None] = {}

    # Parsing ------------------------------------------------------------------
    def module(self, name: str) -> _Module | None:
        if name not in self._modules:
            self._modules[name] = self._parse(name)
        return self._modules[name]

    def _parse(self, name: str) -> _Module | None:
        path = _module_path(name, self.src)
        text = self.sources.get(name)
        if text is None:
            if path is None:
                return None
            text = path.read_text(encoding="utf-8")
        is_pkg = path is not None and path.name == "__init__.py"
        tree = ast.parse(text)
        m = _Module(name)
        _record_imports(tree.body, m.imports, name, is_pkg, self.src)
        for st in tree.body:
            if isinstance(st, _DEFS):
                m.defs[st.name] = st
            elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                for t in targets:
                    for n in _targets(t):
                        m.assigns.setdefault(n, []).append(st)
        m.is_pkg = is_pkg
        return m

    # Resolution ---------------------------------------------------------------
    def _symbol(self, mod: str, name: str, seen: frozenset = frozenset()):
        """(kind, module, name) for `mod.name`: kind is "def" or "const"; None when it
        is not a pinned symbol (a function outside the scoring modules, a submodule,
        or anything outside the package)."""
        if (mod, name) in seen:
            return None
        m = self.module(mod)
        if m is None:
            return None
        if name in m.defs:
            return ("def", mod, name) if mod in self.scoring else None
        if name in m.assigns:
            return ("const", mod, name)
        imp = m.imports.get(name)
        if imp and imp[0] == "attr":
            return self._symbol(imp[1], imp[2], seen | {(mod, name)})
        return None

    def _references(self, mod: str, node: ast.AST):
        m = self.module(mod)
        local = dict(m.imports)
        for n in ast.walk(node):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                _record_imports([n], local, mod, m.is_pkg, self.src)
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                imp = local.get(n.value.id)
                if imp and imp[0] == "module":
                    hit = self._symbol(imp[1], n.attr)
                    if hit:
                        yield hit
            elif isinstance(n, ast.Name):
                imp = local.get(n.id)
                if imp is not None:
                    hit = self._symbol(imp[1], imp[2]) if imp[0] == "attr" else None
                elif n.id in m.defs or n.id in m.assigns:
                    hit = self._symbol(mod, n.id)
                else:
                    hit = None
                if hit:
                    yield hit

    def items(self) -> dict[tuple[str, str], list[ast.AST]]:
        """(module, name) -> the AST nodes pinned for it, the whole closure."""
        out: dict[tuple[str, str], list[ast.AST]] = {}
        todo = [("def", m, n) for m, n in self.entry_points]
        while todo:
            kind, mod, name = todo.pop()
            if (mod, name) in out or (mod, name) in EXCLUDED:
                continue
            m = self.module(mod)
            nodes = [m.defs[name]] if kind == "def" else m.assigns[name]
            out[(mod, name)] = nodes
            for node in nodes:
                todo.extend(self._references(mod, node))
        return out

    def rendered(self) -> list[str]:
        lines = []
        for (mod, name), nodes in sorted(self.items().items()):
            body = "\n".join(render(_strip_docstrings(copy.deepcopy(n))) for n in nodes)
            lines.append(f"{mod}.{name}\n{body}\n")
        return lines

    def sha256(self) -> str:
        h = hashlib.sha256()
        for chunk in self.rendered():
            h.update(chunk.encode("utf-8"))
        return h.hexdigest()


def closure_sha256(**kw) -> str:
    return Closure(**kw).sha256()


if __name__ == "__main__":
    c = Closure()
    if "--list" in sys.argv:
        for mod, name in sorted(c.items()):
            print(f"{mod}.{name}")
    print(sys.version.split()[0], c.sha256())
