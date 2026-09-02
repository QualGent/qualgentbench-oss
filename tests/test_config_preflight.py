"""Config shape (config.py) and runnability (preflight.py), without a device."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from qualgentbench import preflight as pf
from qualgentbench.config import BenchConfig, ConfigError, load_config

GOOD = {
    "agent": "claude-code", "model": "claude-opus-4-8",
    "scope": {"tiers": ["easy"], "mode": "hunt", "trials": 2},
    "devices": {"avds": ["Pixel_8_A", "Pixel_8_B"]},
    "runs_dir": "runs",
}


def _write(tmp_path: Path, data) -> Path:
    p = tmp_path / "bench.yaml"
    p.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data)
    return p


# ── config ─────────────────────────────────────────────────────────────────────

def test_good_config_loads(tmp_path):
    cfg = load_config(_write(tmp_path, GOOD))
    assert cfg.agent == "claude-code" and cfg.scope.trials == 2
    assert cfg.devices.lane_count() == 2


def test_unknown_keys_are_refused_with_their_path(tmp_path):
    bad = {**GOOD, "scope": {**GOOD["scope"], "tier": ["easy"]}, "agnet": "x"}
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, bad))
    assert "scope.tier: unknown key" in exc.value.problems
    assert "agnet: unknown key" in exc.value.problems


def test_every_problem_is_reported_at_once(tmp_path):
    bad = {"agent": "claude-code", "scope": {"mode": "hunting", "trials": 0},
           "devices": {"avds": ["A", "A"]}}
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, bad))
    joined = "\n".join(exc.value.problems)
    assert "model: required" in joined
    assert "scope.mode" in joined and "scope.trials" in joined
    assert "devices.avds" in joined and "more than once" in joined


def test_scope_must_select_something(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, {**GOOD, "scope": {"mode": "hunt"}}))
    assert any("nothing is selected" in p for p in exc.value.problems)


def test_not_yaml_and_not_a_mapping(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "- just\n- a list\n"))
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")


def test_max_lanes_caps_lane_count():
    cfg = BenchConfig.model_validate({**GOOD, "devices": {"avds": ["a", "b", "c"], "max_lanes": 2}})
    assert cfg.devices.lane_count() == 2


# ── preflight ──────────────────────────────────────────────────────────────────

def _cfg(**over) -> BenchConfig:
    data = {**GOOD, **over}
    return BenchConfig.model_validate(data)


def test_unknown_agent_names_the_valid_ones():
    [r] = pf.check_agent(_cfg(agent="claude-cod"))
    assert not r.passed and "claude-code" in r.fix


def test_claude_auth_accepts_env_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    r = pf._check_auth(_cfg())
    assert r.passed and "CLAUDE_CODE_OAUTH_TOKEN" in r.detail


def test_claude_auth_fails_with_nothing(monkeypatch, tmp_path):
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    r = pf._check_auth(_cfg())
    assert not r.passed and "CLAUDE_CODE_OAUTH_TOKEN" in r.fix


def test_fireworks_model_needs_claude_code_and_a_key(monkeypatch):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("FIREWORKS_AI_API_KEY", raising=False)
    fw = "accounts/fireworks/models/x"
    assert not pf.check_model(_cfg(agent="codex-cli", model=fw)).passed
    assert not pf.check_model(_cfg(model=fw)).passed
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw")
    assert pf.check_model(_cfg(model=fw)).passed


def test_scope_selects_ready_tier_apps():
    selected, checks = pf.select_apps(_cfg())
    assert selected and all(s["app"]["difficulty"] == "easy" for s in selected)
    assert all(c.passed for c in checks)


def test_unknown_tier_and_unready_tier_fail(monkeypatch):
    _, checks = pf.select_apps(_cfg(scope={"tiers": ["eazy"], "mode": "hunt"}))
    assert any(not c.passed and "eazy" in c.detail for c in checks)
    # Every shipped tier is hunt-ready since hard joined (2026-08-31); pin one back
    # to unready so the refusal path stays covered.
    monkeypatch.setattr(pf, "_READY_TIERS", {"easy", "medium"})
    _, checks = pf.select_apps(_cfg(scope={"tiers": ["hard"], "mode": "hunt"}))
    assert any(not c.passed and "not hunt-ready" in c.detail for c in checks)
    _, checks = pf.select_apps(_cfg(scope={"tiers": ["hard"], "mode": "guided"}))
    assert all(c.passed for c in checks if c.name == "Tiers")


def test_unknown_app_and_off_tier_app_fail():
    _, checks = pf.select_apps(_cfg(scope={"apps": ["birdday"], "mode": "hunt"}))
    assert any(not c.passed and "birdday" in c.detail and "Available" in c.fix for c in checks)
    selected, _ = pf.select_apps(_cfg(scope={"apps": ["birday"], "mode": "hunt"}))
    assert [s["app"]["id"] for s in selected] == ["birday"]
    medium = next(s["app"]["id"] for s in pf.bugmod.load_apps()
                  if s["app"].get("difficulty") == "medium")
    _, checks = pf.select_apps(_cfg(scope={"tiers": ["easy"], "apps": [medium], "mode": "hunt"}))
    assert any(not c.passed and "not in tier" in c.detail for c in checks)


def test_apk_check_distinguishes_present_fetchable_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("QGB_CACHE_DIR", str(tmp_path / "cache"))
    apk = tmp_path / "a.apk"
    apk.write_bytes(b"x")
    monkeypatch.setenv("QUALGENTBENCH_APK_PRESENT", str(apk))
    specs = [
        {"app": {"id": "present"}},
        {"app": {"id": "fetchable"}, "apk": {"repo": "r", "filename": "f.apk", "sha256": "0"}},
    ]
    r = pf.check_apks(specs)
    assert r.passed and r.warning and "fetchable" in r.detail
    r = pf.check_apks(specs + [{"app": {"id": "nowhere"}}])
    assert not r.passed and "nowhere" in r.detail


async def test_devices_check_only_touches_named_serials():
    r = await pf.check_devices(_cfg(), list_devices=None)
    assert r.passed and "AVD" in r.detail

    async def online():
        return ["emulator-5554"]
    r = await pf.check_devices(_cfg(devices={"serials": ["emulator-5554", "emulator-5556"]}),
                               list_devices=online)
    assert not r.passed and "emulator-5556" in r.detail
    r = await pf.check_devices(_cfg(devices={"serials": ["emulator-5554"]}), list_devices=online)
    assert r.passed


def test_env_file_must_exist_when_named(tmp_path):
    assert not pf.check_env_file(_cfg(env_file=".env"), tmp_path).passed
    (tmp_path / ".env").write_text("")
    assert pf.check_env_file(_cfg(env_file=".env"), tmp_path).passed
    assert pf.check_env_file(_cfg(), tmp_path).passed


async def test_run_preflight_collects_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("QGB_CACHE_DIR", str(tmp_path / "cache"))
    results, selected = await pf.run_preflight(_cfg(env_file=".env"), config_dir=tmp_path)
    names = [r.name for r in results]
    assert names[:2] == ["Agent CLI: claude-code", "Claude auth"]
    assert "Tiers" in names and "APKs" in names and "Devices" in names and "env_file" in names
    assert selected
    assert any(r.name == "env_file" and not r.passed for r in results)
    assert pf.failed(results)   # env_file missing is a hard failure
