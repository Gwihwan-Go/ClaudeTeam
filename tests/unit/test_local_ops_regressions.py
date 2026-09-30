"""Regressions from the local multi-team deployment, with temporary state only."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

from helpers import attr_patch, env_patch, isolated_env, tmux_patch
from claudeteam.agents import claude_code, codex_cli, identity
from claudeteam.commands import watchdog as command_watchdog
from claudeteam.feishu import lark, slash
from claudeteam.runtime import lifecycle, paths, watchdog


def _recovery_module():
    path = Path(__file__).resolve().parents[2] / "examples/team_ops/revive.py"
    spec = importlib.util.spec_from_file_location("team_ops_revive", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_file_falls_back_to_team_state_parent_after_chdir():
    with isolated_env() as tmp, env_patch(CLAUDETEAM_CONFIG_FILE=None):
        (tmp / "claudeteam.toml").write_text("[team]\nsession = 'demo'\n")
        elsewhere = tmp / "nested"
        elsewhere.mkdir()
        with attr_patch(Path, cwd=lambda: elsewhere):
            assert paths.config_file() == tmp / "claudeteam.toml"


def test_explicit_config_wins_over_another_team_in_cwd():
    with isolated_env() as tmp:
        other = tmp / "other"
        other.mkdir()
        (other / "claudeteam.toml").write_text("[team]\nsession = 'other'\n")
        with attr_patch(Path, cwd=lambda: other):
            assert paths.config_file() == tmp / "claudeteam.toml"


def test_pane_environment_keeps_team_config_token_and_proxy():
    with isolated_env(), env_patch(
            CLAUDE_CODE_OAUTH_TOKEN="fixture-token", HTTPS_PROXY="http://proxy:7890"):
        prefix = lifecycle.pane_env_prefix()
        assert "CLAUDETEAM_CONFIG_FILE=" in prefix
        assert "CLAUDE_CODE_OAUTH_TOKEN=fixture-token" in prefix
        assert "HTTPS_PROXY=http://proxy:7890" in prefix


def test_claude_spawn_does_not_read_or_override_operator_credentials():
    def forbidden(*args):
        raise AssertionError("spawn must not load a file-based access token")
    with isolated_env(), attr_patch(claude_code, _read_oauth_token=forbidden,
                                    _DATA_WRITABLE=False):
        cmd = claude_code.ClaudeCodeAdapter().spawn_cmd("manager", "opus")
        assert "CLAUDE_CODE_OAUTH_TOKEN=" not in cmd


def test_linux_agent_home_does_not_copy_host_claude_credentials():
    import subprocess
    with isolated_env() as tmp, env_patch(HOME=str(tmp / "host")), \
            attr_patch(claude_code, _DATA_WRITABLE=False):
        host = tmp / "host/.claude/.credentials.json"
        host.parent.mkdir(parents=True)
        host.write_text('{"private": "operator-only"}')
        def no_keychain(*args, **kwargs):
            raise FileNotFoundError("no macOS keychain")
        with attr_patch(subprocess, run=no_keychain):
            lifecycle._ensure_claude_agent_home("manager")
        target = Path(claude_code.agent_home("manager")) / ".claude/.credentials.json"
        assert not target.exists()
        assert host.read_text() == '{"private": "operator-only"}'


def test_codex_spawn_clears_shell_api_keys():
    with isolated_env(), attr_patch(claude_code, _DATA_WRITABLE=False):
        cmd = codex_cli.CodexCliAdapter().spawn_cmd("worker", "gpt-5.5")
        assert cmd.startswith("OPENAI_API_KEY= OPENAI_ADMIN_KEY= ")
        assert "CODEX_HOME=" in cmd


def test_codex_sync_uses_latest_valid_timestamp_and_ignores_future():
    # HOME and state_dir are BOTH temporary: never seed the real operator fleet.
    with isolated_env() as tmp, env_patch(HOME=str(tmp / "host")):
        host = tmp / "host/.codex/auth.json"
        peer = paths.state_dir() / "agent-home/worker/.codex/auth.json"
        future = paths.state_dir() / "agent-home/bad-clock/.codex/auth.json"
        for p, ts, token in ((host, "2020-01-01T00:00:00Z", "old"),
                             (peer, "2020-01-02T00:00:00Z", "new"),
                             (future, "2999-01-01T00:00:00Z", "invalid-future")):
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"last_refresh": ts, "tokens": token}))
        command_watchdog._sync_codex_creds_across_fleet()
        assert json.loads(host.read_text())["tokens"] == "new"
        assert host.read_bytes() == peer.read_bytes() == future.read_bytes()
        assert host.stat().st_mode & 0o777 == 0o600


def test_daemon_pid_reuse_does_not_accept_agent_identity_prompt():
    with isolated_env():
        for spec in watchdog.all_known_specs():
            assert not watchdog.is_alive(
                spec, read_pid=lambda _: 123, pid_alive=lambda _: True,
                read_cmdline=lambda _: "codex resume session use claudeteam inbox and claudeteam say")
            assert watchdog.is_alive(
                spec, read_pid=lambda _: 123, pid_alive=lambda _: True,
                read_cmdline=lambda _: f"/venv/bin/python /venv/bin/claudeteam {spec.name}")


def test_feishu_cache_is_separate_for_each_app():
    with isolated_env() as tmp, attr_patch(
            lark, _TENANT_TOKEN_CACHE=str(tmp / "token_{app_id}.json")), \
            env_patch(LARKSUITE_CLI_TENANT_ACCESS_TOKEN=None, FEISHU_APP_SECRET="fixture"):
        for app in ("app_a", "app_b"):
            with env_patch(FEISHU_APP_ID=app):
                token = lark._ensure_tenant_token(
                    fetch=lambda *args: {"token": app, "expire_at": 9999999999})
                assert token == app
        assert (tmp / "token_app_a.json").exists()
        assert (tmp / "token_app_b.json").exists()


def test_toml_proxy_policy_overrides_legacy_no_proxy_flag():
    with isolated_env() as tmp, env_patch(
            LARK_CLI_NO_PROXY="1", HTTPS_PROXY="http://proxy:7890"), \
            attr_patch(lark, _ensure_tenant_token=lambda: None):
        (tmp / "claudeteam.toml").write_text("[feishu]\nno_proxy = false\n")
        env = lark.subprocess_env()
        assert env["HTTPS_PROXY"] == "http://proxy:7890"
        assert "LARK_CLI_NO_PROXY" not in env


def test_slash_aliases_resolve_unique_prefix_without_guessing_ambiguous_names():
    team = {"session": "demo", "agents": {"manager": {}, "worker": {}, "worker2": {}}}
    injected, captured = [], []
    with isolated_env(team=team), tmux_patch(
            inject=lambda target, text: injected.append((target.window, text)) or True,
            capture_pane=lambda target, **kw: captured.append(target.window) or "pane"):
        ctx = slash.SlashContext(team_agents=list(team["agents"]), session="demo")
        slash.dispatch("/s m hello", ctx)
        slash.dispatch("/s worker exact", ctx)
        slash.dispatch("/t m 80", ctx)
        ambiguous = slash.dispatch("/s w ambiguous", ctx)
        assert injected == [("manager", "hello"), ("worker", "exact")]
        assert captured == ["manager"]
        assert "歧义" in ambiguous


def test_resume_skips_recent_stillborn_claude_session():
    revive = _recovery_module()
    with isolated_env() as tmp:
        folder = tmp / ".claude/projects/-work-demo"
        folder.mkdir(parents=True)
        good, bad = folder / "valid.jsonl", folder / "crashed.jsonl"
        good.write_text("{}\n" * 12)
        bad.write_text("{}\n")
        os.utime(good, (1, 1))
        os.utime(bad, (2, 2))
        assert revive._claude_sid(tmp, "/work/demo") == ("valid", 12)


def test_resume_reads_both_codex_session_metadata_shapes_and_filename_fallback():
    revive = _recovery_module()
    sid = "12345678-1234-1234-1234-123456789abc"
    with isolated_env() as tmp:
        folder = tmp / ".codex/sessions/2026/09/20"
        folder.mkdir(parents=True)
        f = folder / f"rollout-2026-09-20T10-00-00-{sid}.jsonl"
        for payload in ({"id": sid}, {"session_id": sid}, {}):
            f.write_text(json.dumps({"payload": payload}) + "\n" + "{}\n" * 10)
            assert revive._codex_sid(tmp) == (sid, 11)


def test_codex_revival_uses_short_file_command_and_propagates_spawn_failure():
    revive = _recovery_module()
    team = {"session": "demo", "agents": {"worker": {"cli": "codex-cli", "model": "gpt-5.5"}}}
    prompt = "identity " * 4000
    commands = []
    with isolated_env(team=team) as tmp, \
            env_patch(HOME=str(tmp / "host")), \
            attr_patch(sys, argv=["revive.py", "demo", str(tmp), "worker"]), \
            attr_patch(claude_code, _DATA_WRITABLE=False), \
            attr_patch(claude_code.ClaudeCodeAdapter,
                       spawn_cmd=claude_code.ClaudeCodeAdapter.spawn_cmd), \
            attr_patch(lifecycle, _ensure_agent_home=lambda *a: None), \
            attr_patch(codex_cli, ensure_workdir_trusted=lambda *a, **kw: None), \
            attr_patch(identity, write=lambda *a, **kw: None, init_prompt=lambda _: prompt), \
            tmux_patch(spawn_agent=lambda target, cmd: commands.append(cmd) or False):
        assert revive.main() == 1
        pf = paths.state_dir() / "agents/worker/init_prompt.txt"
        assert pf.read_text() == prompt
        assert "$(cat " in commands[0]
        assert len(commands[0]) < 3000
        assert prompt not in commands[0]
