"""The post-stage memory curator: proposals in, operations out, applied in code (spec §3.4, D5).

Between stage k and stage k+1 the curator runs once per (app, arm, rep). Its inputs are
M_k (a memory root), every stage-k episode of that unit with its proposals file
(`memory.proposals`) and the episode's VERIFIED outcome (the scorer's, never the
agent's own claim). It writes M_{k+1} to a new memory root and returns an operation log.

Every proposal ends in exactly one operation, ADD / UPDATE / INVALIDATE / NOOP, and the
log says who decided it (`by`):

- `gate` (deterministic, before any model call):
  - `accepted` proposals are rejected: only humans write known accepted behavior (D6).
  - A proposal from an UNVERIFIED episode can only raise `contra` on an existing live
    entry it `contradicts`; anything else from it is a NOOP.
  - `updates`/`contradicts` naming an id that is not a live entry is dropped
    (`contradicts`: NOOP; `updates`: treated as a plain proposal).
- `dedup` (deterministic): a plain verified proposal whose text is at least
  `SIM_HIGH` similar to a live entry of its type confirms that entry (UPDATE seen+1);
  one that similar to an earlier proposal of this batch is merged into it.
- `model`: everything left goes to ONE call of the fixed curator model (D12) with the
  prompt in `prompts/curator.md`. Similar entries between `SIM_LOW` and `SIM_HIGH`
  are passed as candidates ("unsure" — the model decides whether they are the same).
  The model may only pick an operation the gate allowed for that proposal; anything
  else is a NOOP. A verified `contradicts` proposal the model does not use to
  INVALIDATE or UPDATE its target still raises the target's `contra`.
- `lint`: after every write the app file is linted (`memory.lint`); a write that adds
  a lint issue is rolled back and logged as a NOOP. The output always lints clean.

The file is never rewritten as a whole: operations go through `format.MemoryFile`
(`add_entry`, `update_entry`, `bump`, `invalidate`), so ids never change and invalidated
entries move to `## Invalidated`. Nothing reads the clock: the caller passes `date`, so
the same inputs (and the same model answers, see `CachedModel`) give the same snapshot.

No proposals at all (no episodes, every file missing, every episode excluded) is a
CARRY-FORWARD: M_k is copied unchanged and the log says so (`carried_forward`).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .. import failures, pricing
from . import format as fmt
from . import proposals as props
from .lint import CorpusTerms, default_terms, lint_text, normalize
from .snapshot import memory_files, snapshot_hash, unit_files

LOG_FORMAT = 1
OPS = ("ADD", "UPDATE", "INVALIDATE", "NOOP")
AGENT_SRC = "agent"

# Similarity (0..1) between a proposal and an entry of the same type. At or above
# SIM_HIGH they are the same fact (decided in code); below SIM_LOW they are unrelated;
# in between the model is asked.
SIM_HIGH = 0.9
SIM_LOW = 0.5
MAX_CANDIDATES = 3
MAX_REASON_CHARS = 200

PROMPT_PATH = Path(__file__).parent / "prompts" / "curator.md"
MODEL_ENV = "QGB_CURATOR_MODEL"
# The provider follows the model id (`provider_for`): a Fireworks slug
# (`accounts/fireworks/models/...`) goes to Fireworks' Anthropic-compatible Messages
# endpoint with the Fireworks key, any other id to the Anthropic API with the Anthropic
# key. Both speak the same Messages API shape.
API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
FIREWORKS_KEY_ENVS = ("FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY")
FIREWORKS_URL = "https://api.fireworks.ai/inference/v1/messages"
# Room for a reasoning model's thinking plus the JSON answer.
MAX_TOKENS = 16000


class CuratorError(RuntimeError):
    pass


def prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def prompt_sha256() -> str:
    return hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()


# ── the model seam ────────────────────────────────────────────────────────────

class CuratorModel(Protocol):
    """Anything that answers a curator request. `id` names the model in the log and the
    cache key; `decide` returns `{"decisions": [...]}` (see the prompt)."""
    id: str

    def decide(self, request: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class FunctionModel:
    """A model backed by a Python function: tests, fixtures, offline replays."""
    fn: Callable[[dict[str, Any]], dict[str, Any]]
    id: str = "function"
    calls: int = 0

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return self.fn(request)


_THINK_TAG = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _answers(text: str) -> list[Any]:
    """Every JSON object in `text`: the whole text, each code fence, then each object
    that starts at a `{` (scanned with `raw_decode`, so nesting and strings are exact)."""
    out: list[Any] = []
    for chunk in [text, *_FENCE.findall(text)]:
        try:
            out.append(json.loads(chunk.strip()))
        except json.JSONDecodeError:
            pass
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        out.append(obj)
        i = text.find("{", end)
    return out


def parse_answer(text: str) -> dict[str, Any]:
    """The model's JSON answer `{"decisions": [...]}`. Tolerates what reasoning models
    wrap it in: `<think>` sections, prose before or after, and ```json fences. When
    several objects carry a `decisions` list, the LAST one is the answer (a model that
    drafts and then corrects itself ends with the correction). Anything else raises
    `CuratorError`."""
    s = _THINK_TAG.sub("", text or "").strip()
    found = _answers(s)
    answers = [o for o in found if isinstance(o, dict) and isinstance(o.get("decisions"), list)]
    if answers:
        return answers[-1]
    if not found:
        raise CuratorError("curator model answer is not JSON: no JSON object in "
                           f"{len(s)} chars of text")
    raise CuratorError("curator model answer has no `decisions` list")


def provider_for(model: str) -> str:
    """`fireworks` for a Fireworks model slug, else `anthropic`."""
    return "fireworks" if pricing.is_fireworks_model(model) else "anthropic"


@dataclass
class MessagesModel:
    """The curator model through a Messages API endpoint. The provider follows the model
    id (`provider_for`):

    - Fireworks (`accounts/fireworks/models/...`): Fireworks' Anthropic-compatible
      endpoint, authenticated with `FIREWORKS_API_KEY` (or `FIREWORKS_AI_API_KEY`) as
      both `x-api-key` and a Bearer token; temperature 0.
    - Anthropic (any other id): api.anthropic.com with `ANTHROPIC_API_KEY`. No
      `temperature` is sent: current models reject it.

    Only text blocks of the answer are read (thinking blocks are skipped). The model id
    is configuration, never a default in code: one fixed id per experiment (D12).
    `usage` keeps the provider's token counts of every call (not cached)."""
    model: str
    max_tokens: int = MAX_TOKENS
    timeout_s: float = 600.0
    api_key_env: str | None = None  # override the provider's key variable (tests)
    url: str | None = None          # override the provider's endpoint (tests)
    transport: Any = None           # an httpx transport, for tests
    usage: list[dict[str, Any]] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.model

    @property
    def provider(self) -> str:
        return provider_for(self.model)

    def _key(self) -> str:
        names = ((self.api_key_env,) if self.api_key_env else
                 FIREWORKS_KEY_ENVS if self.provider == "fireworks" else (API_KEY_ENV,))
        key = next((os.environ[n] for n in names if os.environ.get(n)), None)
        if not key:
            raise CuratorError(f"{' or '.join(names)} is not set (curator model "
                               f"{self.model!r} runs on {self.provider})")
        return key

    def request_body(self, request: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": prompt_text(),
            "messages": [{"role": "user",
                          "content": json.dumps(request, sort_keys=True, ensure_ascii=False)}],
        }
        if self.provider == "fireworks":
            body["temperature"] = 0
        return body

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        import httpx

        key = self._key()
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        url = self.url
        if self.provider == "fireworks":
            headers["authorization"] = f"Bearer {key}"
            url = url or FIREWORKS_URL
        url = url or ANTHROPIC_URL
        with httpx.Client(timeout=self.timeout_s, transport=self.transport) as c:
            r = c.post(url, json=self.request_body(request), headers=headers)
        if r.status_code != 200:
            raise CuratorError(f"curator model call failed: HTTP {r.status_code} "
                               f"from {self.provider}")
        data = r.json()
        self.usage.append(dict(data.get("usage") or {}))
        text = "".join(b.get("text", "") for b in data.get("content") or []
                       if isinstance(b, dict) and b.get("type") == "text")
        try:
            return parse_answer(text)
        except CuratorError as e:
            if data.get("stop_reason") == "max_tokens":
                raise CuratorError(f"curator model hit max_tokens={self.max_tokens} "
                                   f"before a complete answer ({e})") from e
            raise


# The QUA-3486 name. Despite it, the provider follows the model id.
AnthropicModel = MessagesModel


@dataclass
class CachedModel:
    """Answers from `cache_dir` when the same (model, prompt, request) was asked before;
    otherwise asks `inner` and stores the answer. This is what makes a re-run of the
    curator reproduce its snapshot: the cache is the record of what the model said."""
    inner: CuratorModel
    cache_dir: Path
    hits: int = 0
    misses: int = 0

    @property
    def id(self) -> str:
        return self.inner.id

    def key(self, request: dict[str, Any]) -> str:
        blob = json.dumps({"model": self.inner.id, "prompt": prompt_sha256(),
                           "request": request}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        p = Path(self.cache_dir) / f"{self.key(request)}.json"
        if p.is_file():
            self.hits += 1
            return json.loads(p.read_text(encoding="utf-8"))
        self.misses += 1
        out = self.inner.decide(request)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        tmp.replace(p)
        return out


def model_from_env(*, cache_dir: Path | None = None, model: str | None = None) -> CuratorModel:
    """The configured curator model: `model` or `$QGB_CURATOR_MODEL`, cached under
    `cache_dir` when given."""
    model = model or os.environ.get(MODEL_ENV)
    if not model:
        raise CuratorError(f"no curator model: pass --model or set {MODEL_ENV}")
    m: CuratorModel = MessagesModel(model=model)
    return CachedModel(m, Path(cache_dir)) if cache_dir else m


# ── episodes and the verified outcome ─────────────────────────────────────────

def verified_outcome(metrics: dict[str, Any] | None) -> tuple[bool, str]:
    """Did the harness verify this episode's result? Read from the scored metrics
    (journey `result.json` metrics), never from the agent's own report.

    Verified = not excluded (env/infra failure, contaminated, rate-limited), no false
    report, and either completion verified on the device (`completed is True`) or at
    least one report that matched a seeded defect (`bugs_found`)."""
    if not isinstance(metrics, dict) or not metrics:
        return False, "no scored result"
    if failures.is_excluded(metrics):
        return False, f"excluded: {failures.exclusion_reason(metrics)}"
    if metrics.get("false_reports"):
        return False, f"{metrics['false_reports']} false report(s)"
    if metrics.get("completed") is True:
        return True, "completion verified on device"
    if metrics.get("bugs_found"):
        return True, "report matched a seeded defect"
    if metrics.get("completed") is None:
        return False, "completion not scored"
    return False, "not completed and no matched report"


@dataclass
class Episode:
    """One stage-k episode of the unit being curated."""
    id: str
    verified: bool
    proposals: props.ProposalFile
    case: str | None = None
    verified_reason: str = ""

    @classmethod
    def from_files(cls, episode_id: str, proposals_path: Path | str,
                   metrics: dict[str, Any] | None, *, case: str | None = None) -> Episode:
        ok, why = verified_outcome(metrics)
        return cls(id=episode_id, verified=ok, verified_reason=why,
                   proposals=props.load(proposals_path),
                   case=case or (metrics or {}).get("case_id"))


# ── helpers ───────────────────────────────────────────────────────────────────

def similarity(a: str, b: str) -> float:
    """0..1, deterministic: the larger of the character-sequence ratio and the word-set
    Jaccard index over the lint's normalized text."""
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return 1.0
    ratio = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return round(max(ratio, jac), 4)


def _clean_text(s: object) -> str:
    t = " ".join(str(s or "").replace("<!--", " ").replace("-->", " ").split())
    return t[: props.MAX_TEXT_CHARS].rstrip()


def _clean_reason(s: object) -> str:
    t = " ".join(str(s or "").replace(";", ",").replace("<!--", " ")
                 .replace("-->", " ").split())
    return t[:MAX_REASON_CHARS].rstrip()


def _live(doc: fmt.MemoryFile, entry_id: str | None) -> fmt.Entry | None:
    if not entry_id:
        return None
    hit = doc.find(entry_id)
    if hit is None or hit[0].invalidated:
        return None
    return hit[1]


@dataclass
class _Item:
    key: str
    episode: Episode
    proposal: props.Proposal
    record: dict[str, Any] = field(default_factory=dict)
    allowed: dict[str, list[str | None]] = field(default_factory=dict)  # op -> targets
    candidates: list[dict[str, Any]] = field(default_factory=list)
    duplicate_of: str | None = None
    contra_target: str | None = None    # verified contradicts: raise contra unless used
    final: dict[str, Any] | None = None  # the decision to apply


@dataclass
class CurateResult:
    snapshot: str            # sha256 over the unit's files (org + app) in out_root
    parent: str              # the same over memory_root
    out_root: Path
    carried_forward: bool
    log: dict[str, Any]

    @property
    def counts(self) -> dict[str, int]:
        return dict(self.log["counts"])


# ── the curator ───────────────────────────────────────────────────────────────

def curate(memory_root: Path | str, out_root: Path | str, *, org_key: str, app_key: str,
           episodes: Iterable[Episode], model: CuratorModel | None, stage: str,
           date: str, terms: CorpusTerms | None = None,
           exclude_cases: Iterable[str] = (), log_path: Path | str | None = None
           ) -> CurateResult:
    """Build M_{k+1} in `out_root` (absent or empty) from M_k in `memory_root`.

    `stage` names the stage the proposals came from (written as `stage=` on new
    entries and in the log); `date` (YYYY-MM-DD) is the `confirmed`/`invalidated` date.
    `model` may be None only when nothing needs a model (the call raises otherwise).
    Episodes whose `case` is in `exclude_cases` are skipped (leakage rule, spec §5).
    """
    src, out = Path(memory_root), Path(out_root)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise CuratorError(f"date {date!r} is not YYYY-MM-DD")
    if not re.fullmatch(r"[A-Za-z0-9._=+-]+", stage):
        raise CuratorError(f"stage {stage!r} must match [A-Za-z0-9._=+-]+")
    if out.exists() and any(out.iterdir()):
        raise CuratorError(f"{out} is not empty")
    files = unit_files(org_key, app_key)
    parent = snapshot_hash(src, files)
    terms = terms if terms is not None else default_terms()
    app_rel = fmt.app_path(Path(""), org_key, app_key).as_posix()
    text0 = (src / app_rel).read_text(encoding="utf-8")

    def lint(text: str) -> list:
        return lint_text(text, layer=fmt.APP, path=app_rel, apps=[app_key], terms=terms)

    issues0 = lint(text0)
    if issues0:
        raise CuratorError("input memory fails lint: " + "; ".join(map(str, issues0[:5])))
    doc = fmt.loads(text0, fmt.APP)
    excluded = set(exclude_cases)

    eps = sorted(episodes, key=lambda e: e.id)
    if len({e.id for e in eps}) != len(eps):
        raise CuratorError("duplicate episode ids")
    log_eps: list[dict[str, Any]] = []
    items: list[_Item] = []
    for ep in eps:
        pf = ep.proposals
        rec = {"id": ep.id, "case": ep.case, "verified": ep.verified,
               "verified_reason": ep.verified_reason, "proposals": len(pf.proposals),
               "missing": pf.missing, "errors": list(pf.errors), "excluded": None}
        log_eps.append(rec)
        if ep.case is not None and ep.case in excluded:
            rec["excluded"] = "case excluded from this snapshot (leakage rule)"
            continue
        for p in pf.proposals:
            key = f"{ep.id}#{p.index}"
            items.append(_Item(key=key, episode=ep, proposal=p, record={
                "proposal": key, "episode": ep.id, "type": p.type, "text": p.text,
                "verified": ep.verified}))

    log: dict[str, Any] = {
        "format": LOG_FORMAT, "stage": stage, "date": date, "org": org_key,
        "app": app_key, "parent": parent, "snapshot": None,
        "model": getattr(model, "id", None), "prompt_sha256": prompt_sha256(),
        "model_called": False, "carried_forward": False, "carry_reason": None,
        "episodes": log_eps, "operations": [], "counts": {}, "lint": "clean",
    }

    if not items:
        why = ("no episodes" if not eps else
               "no proposals (files missing, empty, invalid or excluded)")
        return _finish(src, out, files, doc, log, parent, carried=why, log_path=log_path)

    # ── deterministic pass: gate and dedup ──
    reps: list[_Item] = []      # plain verified proposals that stand for themselves
    for it in items:
        p, rec = it.proposal, it.record
        if p.type == fmt.ACCEPTED_TYPE:
            it.final = {"op": "NOOP", "by": "gate",
                        "reason": "accepted behavior is written by humans only (D6)"}
            continue
        contra = _live(doc, p.contradicts)
        upd = _live(doc, p.updates)
        if p.contradicts and contra is None:
            it.final = {"op": "NOOP", "by": "gate",
                        "reason": f"contradicts {p.contradicts}, which is not a live entry"}
            continue
        if p.updates and upd is None:
            rec["note"] = f"updates {p.updates}, which is not a live entry; ignored"
        if not it.episode.verified:
            if contra is not None:
                it.final = {"op": "UPDATE", "by": "gate", "target": contra.id,
                            "contra": 1, "reason": "unverified episode: contra only"}
            else:
                it.final = {"op": "NOOP", "by": "gate",
                            "reason": "unverified episode: may only raise contra"}
            continue
        sims = sorted(((similarity(p.text, e.text), e.id) for e in doc.entries()
                       if e.type == p.type), key=lambda t: (-t[0], t[1]))
        it.candidates = [{"id": eid, "similarity": s} for s, eid in sims
                         if SIM_LOW <= s < SIM_HIGH][:MAX_CANDIDATES]
        cand_ids = [c["id"] for c in it.candidates]
        if contra is not None:
            it.contra_target = contra.id
            it.allowed = {"ADD": [None], "NOOP": [None], "UPDATE": [contra.id]}
            if contra.type != fmt.ACCEPTED_TYPE:
                it.allowed["INVALIDATE"] = [contra.id]
            continue
        if upd is not None:
            it.allowed = {"ADD": [None], "NOOP": [None],
                          "UPDATE": sorted({upd.id, *cand_ids})}
            continue
        if sims and sims[0][0] >= SIM_HIGH:
            it.final = {"op": "UPDATE", "by": "dedup", "target": sims[0][1], "seen": 1,
                        "reason": f"same fact as {sims[0][1]} (similarity {sims[0][0]})"}
            continue
        dup = next((r for r in reps if r.proposal.type == p.type
                    and similarity(p.text, r.proposal.text) >= SIM_HIGH), None)
        if dup is not None:
            it.duplicate_of = dup.key
            continue
        reps.append(it)
        it.allowed = {"ADD": [None], "NOOP": [None]}
        if cand_ids:
            it.allowed["UPDATE"] = cand_ids

    # ── one model call for everything the gate left open ──
    open_items = [it for it in items if it.final is None and it.duplicate_of is None]
    if open_items:
        if model is None:
            raise CuratorError("proposals need a curator model, and none was given")
        request = {
            "app": app_key,
            "memory": [{"id": e.id, "type": e.type, "text": e.text, "seen": e.seen,
                        "contra": e.contra} for e in doc.entries()],
            "proposals": [{
                "key": it.key, "type": it.proposal.type, "text": it.proposal.text,
                "evidence": it.proposal.evidence.as_dict(),
                "updates": it.proposal.updates, "contradicts": it.proposal.contradicts,
                "candidates": it.candidates,
                "allowed": {op: [t for t in ts if t] for op, ts in sorted(it.allowed.items())},
            } for it in open_items],
        }
        answer = model.decide(request)
        log["model_called"] = True
        by_key: dict[str, dict] = {}
        for d in answer.get("decisions") or []:
            if isinstance(d, dict) and isinstance(d.get("proposal"), str):
                by_key.setdefault(d["proposal"], d)
        for it in open_items:
            it.final = _check_decision(it, by_key.get(it.key))

    # ── apply, in proposal order, linting every write ──
    credited: dict[str, set[str]] = {}
    produced: dict[str, str] = {}       # proposal key -> entry id it ADDed/UPDATEd
    for it in items:
        if it.duplicate_of is not None:
            target = produced.get(it.duplicate_of)
            it.final = ({"op": "UPDATE", "by": "dedup", "target": target, "seen": 1,
                         "reason": f"same fact as proposal {it.duplicate_of}"}
                        if target else
                        {"op": "NOOP", "by": "dedup",
                         "reason": f"same fact as proposal {it.duplicate_of}, "
                                   "which was not kept"})
        assert it.final is not None
        rec = {**it.record, **it.final}
        before = doc.dumps()
        try:
            entry_id = _apply(doc, it, rec, stage=stage, date=date, credited=credited)
        except (fmt.FormatError, KeyError) as e:
            doc = fmt.loads(before, fmt.APP)
            rec.update(op="NOOP", by="apply", reason=f"could not apply: {e}")
            entry_id = None
        if doc.dumps() != before:
            new_issues = lint(doc.dumps())
            if new_issues:
                doc = fmt.loads(before, fmt.APP)
                rec.update(op="NOOP", by="lint",
                           reason="the write fails lint: " + "; ".join(
                               f"{i.rule}: {i.message}" for i in new_issues[:3]))
                entry_id = None
        if entry_id and rec["op"] in ("ADD", "UPDATE"):
            produced[it.key] = entry_id
        log["operations"].append(rec)

    final = lint(doc.dumps())
    if final:   # cannot happen: every write was linted
        raise CuratorError("curator output fails lint: " + "; ".join(map(str, final[:5])))
    return _finish(src, out, files, doc, log, parent, carried=None, log_path=log_path)


def _check_decision(it: _Item, d: dict | None) -> dict[str, Any]:
    if d is None:
        return {"op": "NOOP", "by": "model", "reason": "no decision from the model"}
    op = str(d.get("op") or "").upper()
    target = d.get("target") or None
    reason = _clean_reason(d.get("reason"))
    if op not in it.allowed:
        return {"op": "NOOP", "by": "model",
                "reason": f"model chose {op or 'nothing'}, not allowed here"}
    if op in ("UPDATE", "INVALIDATE"):
        if target not in it.allowed[op]:
            return {"op": "NOOP", "by": "model",
                    "reason": f"model chose {op} on {target!r}, not an allowed target"}
    else:
        target = None
    out: dict[str, Any] = {"op": op, "by": "model", "reason": reason}
    if target:
        out["target"] = target
    text = d.get("text")
    if op in ("ADD", "UPDATE") and isinstance(text, str) and _clean_text(text):
        out["new_text"] = _clean_text(text)
    if op == "INVALIDATE" and not reason:
        out["reason"] = "contradicted by a verified run"
    return out


def _apply(doc: fmt.MemoryFile, it: _Item, rec: dict[str, Any], *, stage: str,
           date: str, credited: dict[str, set[str]]) -> str | None:
    """Apply `rec` (an operation) to `doc`. Returns the entry id written, if any."""
    ep = it.episode.id
    op = rec["op"]

    def credit(entry_id: str) -> int:
        seen = credited.setdefault(entry_id, set())
        if ep in seen:
            return 0
        seen.add(ep)
        return 1

    def raise_contra(entry_id: str | None) -> None:
        if entry_id and _live(doc, entry_id) is not None:
            doc.bump(entry_id, contra=1)
            rec["contra_raised"] = entry_id

    if op == "NOOP":
        raise_contra(it.contra_target)
        return None

    if op == "ADD":
        text = rec.pop("new_text", None) or _clean_text(it.proposal.text)
        # A model-tightened text can land on an entry this run already added.
        twin = next((e for e in doc.entries() if e.type == it.proposal.type
                     and similarity(text, e.text) >= SIM_HIGH), None)
        if twin is not None:
            rec.update(op="UPDATE", by="dedup", target=twin.id,
                       reason=f"same fact as {twin.id}")
            n = credit(twin.id)
            if n:
                doc.bump(twin.id, seen=n, confirmed=date)
            rec["seen"] = n
            raise_contra(it.contra_target)
            return twin.id
        e = doc.add_entry(it.proposal.type, text, {
            "src": AGENT_SRC, "confirmed": date, "seen": 1, "contra": 0, "stage": stage})
        credited.setdefault(e.id, set()).add(ep)
        rec["entry"] = e.id
        rec["text"] = e.text
        raise_contra(it.contra_target)
        return e.id

    target = rec.get("target")
    entry = _live(doc, target)
    if entry is None:
        rec.update(op="NOOP", reason=f"{target} is no longer a live entry")
        return None

    if op == "INVALIDATE":
        doc.invalidate(entry.id, rec.get("reason") or "contradicted by a verified run",
                       date=date)
        return None

    # UPDATE: text and/or counters
    if rec.get("contra") and not rec.get("seen") and rec.get("by") == "gate":
        doc.bump(entry.id, contra=1)
        return entry.id
    new_text = rec.pop("new_text", None)
    if new_text and entry.type == fmt.ACCEPTED_TYPE:
        rec["note"] = "text change dropped: accepted behavior is human-only (D6)"
        new_text = None
    if new_text and new_text != entry.text:
        rec["old_text"] = entry.text
        doc.update_entry(entry.id, text=new_text)
        rec["text"] = new_text
    n = credit(entry.id)
    if n:
        doc.bump(entry.id, seen=n, confirmed=date)
    rec["seen"] = n
    return entry.id


def _finish(src: Path, out: Path, files: list[str], doc: fmt.MemoryFile,
            log: dict[str, Any], parent: str, *, carried: str | None,
            log_path: Path | str | None) -> CurateResult:
    out.mkdir(parents=True, exist_ok=True)
    for rel in memory_files(src):
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, dst)
    if carried is None:
        app_rel = next(f for f in files if f.startswith("app/"))
        (out / app_rel).write_text(doc.dumps(), encoding="utf-8")
    snap = snapshot_hash(out, files)
    counts = {op: 0 for op in OPS}
    for rec in log["operations"]:
        counts[rec["op"]] += 1
    counts["proposals"] = len(log["operations"])
    counts["episodes"] = len(log["episodes"])
    counts["episodes_verified"] = sum(1 for e in log["episodes"] if e["verified"])
    counts["episodes_missing_proposals"] = sum(1 for e in log["episodes"] if e["missing"])
    log.update(snapshot=snap, counts=counts, carried_forward=carried is not None,
               carry_reason=carried)
    if carried is not None:
        assert snap == parent
    if log_path is not None:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(log, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    return CurateResult(snapshot=snap, parent=parent, out_root=out,
                        carried_forward=carried is not None, log=log)
