"""Pane provisioning shared between `start` and `hire`.

`provision_pane(agent, target)` writes identity, handles lazy panes,
spawns the configured CLI, waits for the ready banner, injects the
identity init prompt, and updates the agent's status row. Both
`commands/start.py` (looping over the team) and `commands/hire.py`
(single agent) call into this so the spawn-and-init contract lives in
one place.

Returns one of five outcome strings (callers render differently):
  LAZY            agent has `lazy: true` in team.json; no spawn attempted,
                  status set to 待命
  READY           CLI spawned + ready marker seen + identity init injected
  READY_NO_INIT   CLI spawned but ready marker didn't appear in 20s;
                  identity init skipped (caller surfaces a warning)
  SPAWN_FAILED    `tmux.spawn_agent` returned False (tmux send-keys failed)
  CONFIG_ERROR    bad `cli` value (typo, dropped adapter) caught as
                  KeyError on adapter lookup; caller logs + skips this
                  agent, keeps going for the rest of the team rather
                  than aborting the whole `claudeteam start`.

Also home for `pane_env_prefix()` — the shell env-var prefix prepended
to every spawn_cmd so worker agents inherit `CLAUDETEAM_STATE_DIR` and
the Feishu env into their `claudeteam say` shell-outs.
"""
from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path

from claudeteam.agents import get_adapter, identity
from claudeteam.agents.codex_cli import ensure_workdir_trusted
from claudeteam.runtime import config, paths, tmux, wake
from claudeteam.store import local_facts
from claudeteam.util import env_str


# env vars to propagate from the operator's shell into every spawned pane
# so worker agents' shell-out calls (via Bash tool) see the deployment's
# state dir instead of falling back to ~/.claudeteam.
#
# FEISHU_APP_*/LARKSUITE_CLI_APP_* added 2026-05-08 (bringup B5): when
# tmux server was started by an earlier checkout's `claudeteam up`, new
# panes inherit *its* global env (no FEISHU_APP_ID/SECRET). lark.py's
# tenant_token_from_env() returned None and fell back to the saved
# lark-cli profile — a different app — yielding HTTP 400 "Bot/User can
# NOT be out of the chat" on every `claudeteam say`. Embedding the creds
# in the spawn-cmd prefix sidesteps the tmux-server-env quirk entirely.
_PROPAGATED_ENV = (
    "LARK_CLI_PROFILE",
    "LARK_CLI_NO_PROXY",
    "CLAUDETEAM_LARK_SEND_AS",
    "CLAUDETEAM_TEAM_FILE",
    "CLAUDETEAM_RUNTIME_CONFIG",
    # CLAUDETEAM_CONFIG_FILE pins the unified TOML per pane. Without it,
    # paths.config_file() falls back to `<cwd>/claudeteam.toml` — a pane
    # whose shell happened to sit in another team's workdir (2026-07-14:
    # ClaudeTeam2:CC1 spawned from ~/Symcli) resolves the WRONG team's
    # chat_id/profile and posts to the other team's Feishu chat.
    "CLAUDETEAM_CONFIG_FILE",
    "CLAUDETEAM_DEFAULT_MODEL",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "LARKSUITE_CLI_APP_ID",
    "LARKSUITE_CLI_APP_SECRET",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "NODE_EXTRA_CA_CERTS",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "no_proxy",
)


def _path_readable(p: Path) -> bool:
    """Returns True iff `p` can be stat'd. False on PermissionError /
    not-found / any OSError. deploy-issues 2026-05-08 #1: on Linux host
    where /root is mode 700, Path("/root/...").exists() raised
    PermissionError instead of returning False (Python <3.13 behavior),
    killing `claudeteam up` for non-root deployers. Three /root probes
    in this module need the soft semantic."""
    try:
        return p.exists()
    except OSError:
        return False


def _pick_claude_seed(candidates: list[Path]) -> bytes | None:
    """Choose the bytes to seed a fresh agent ~/.claude.json with.

    claude needs the `oauthAccount` key here or it pops the OAuth login
    dialog (credentials.json alone isn't enough). Walk `candidates` in
    priority order and return the first that carries an account; if none
    do, return the first readable one anyway so onboarding/migration
    flags still land. None when nothing is readable.

    deploy-issues 2026-06-08: the Dockerfile's `claude --version` leaves
    a stub /root/.claude.json with no oauthAccount. Seeding from it blind
    (it sorts before the real /root/host-claude.json mount) put every
    agent at the login screen despite a valid CLAUDE_CODE_OAUTH_TOKEN.
    """
    seed = None
    for src in candidates:
        if not _path_readable(src):
            continue
        try:
            data = src.read_bytes()
        except OSError:
            continue
        if seed is None:
            seed = data
        if b'"oauthAccount"' in data:
            return data
    return seed


def _mark_project_trusted(claude_json: Path, workdir: Path) -> None:
    """Pre-accept claude's per-folder trust dialog for `workdir` by
    setting projects[workdir].hasTrustDialogAccepted in the agent's
    ~/.claude.json. The seed source (host account) only lists the
    operator's own project paths, so a fresh container agent-home blocks
    at the interactive "Is this a project you trust?" gate on first
    spawn — which stalls wait_until_ready and skips the identity init
    prompt (2026-06-08 docker smoke). Mirrors codex's
    ensure_workdir_trusted. Idempotent + best-effort: a malformed
    claude.json or read-only home never aborts `claudeteam start`.
    """
    key = str(workdir)
    try:
        data = json.loads(claude_json.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    entry = data.setdefault("projects", {}).setdefault(key, {})
    if entry.get("hasTrustDialogAccepted") is True:
        return  # already trusted — skip the (148 KB) rewrite
    entry["hasTrustDialogAccepted"] = True
    try:
        claude_json.write_text(json.dumps(data))
    except OSError:
        pass


def _ensure_claude_agent_home(agent: str) -> None:
    """Materialise a per-agent claude state dir at /data/agent-home/<agent>.

    Each claude pane spawns with `HOME=/data/agent-home/<agent>` so
    each agent has its own `~/.claude.json` (avoids the shared-file
    write-race that corrupts a single-mount setup). The directory
    contains:
      .claude/settings.json     — silent-launch flags (theme, perms)
      .claude/.credentials.json — symlink to /root/.claude/.credentials.json
                                  so OAuth tokens stay bind-mount shared
      .claude/projects          — symlink to /root/.claude/projects
                                  so ccusage in /usage finds session logs
    Best-effort: if /data isn't writable (host tests where the path
    doesn't exist), silently skip and let claude fall back to its
    default `$HOME` discovery.
    """
    from claudeteam.agents.claude_code import agent_home as _agent_home
    home = Path(_agent_home(agent))
    claude_dir = home / ".claude"
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    # Host fallback: claude on macOS keys keychain lookup by $HOME, so a
    # per-agent HOME with no .credentials.json gets "Not logged in" even
    # though the keychain entry exists for the user. Export it to a file
    # the first time so each pane has working OAuth.
    cred_link = claude_dir / ".credentials.json"
    # macOS host: prefer the live keychain over a (potentially-stale) host
    # ~/.claude/.credentials.json. Claude refreshes OAuth into the keychain
    # but only writes the file occasionally, so a symlink to the host file
    # can hand the pane a `refreshToken` the server has already revoked.
    # 2026-05-07 caught: pane symlinked to stale host file, refresh
    # round-tripped 401, claude blanked the field, pane logged "401
    # Invalid auth credentials". Re-extract on every provision and write
    # a *regular file* — not a symlink — because claude's atomic-write
    # of credentials replaces the symlink target with a plain file on
    # first refresh anyway, defeating the original sharing intent.
    import platform
    keychain_extracted = False
    if platform.system() == "Darwin":
        import subprocess
        try:
            out = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                if cred_link.is_symlink() or cred_link.exists():
                    cred_link.unlink()
                cred_link.write_text(out.stdout)
                keychain_extracted = True
        except (OSError, subprocess.TimeoutExpired):
            # `security` missing / keychain locked / subprocess timeout →
            # silent skip and fall through to the host-file branch below.
            pass
    # Host-file cred copy RETIRED 2026-07-13. Deployment policy: claude
    # panes authenticate ONLY via the long-lived CLAUDE_CODE_OAUTH_TOKEN
    # env var (threaded through _PROPAGATED_ENV into every spawn_cmd).
    # Copying the operator's ~/.claude/.credentials.json here gave every
    # pane an 8h-TTL file that claude prefers over the env token; when it
    # expired, the whole fleet demanded /login (2026-07-09 incident). The
    # host file belongs to the operator's interactive sessions only.
    # (keychain_extracted branch above is macOS-only and unused on this
    # Linux deploy; left intact for upstream compatibility.)
    _ = keychain_extracted  # noqa: F841
    settings = claude_dir / "settings.json"
    if not settings.exists():
        settings.write_text(
            '{\n'
            '  "skipDangerousModePermissionPrompt": true,\n'
            '  "hasCompletedOnboarding": true,\n'
            '  "theme": "dark",\n'
            '  "permissions": {\n'
            '    "allow": ["Bash", "Edit", "Read", "Write"]\n'
            '  }\n'
            '}\n'
        )
    cred_link = claude_dir / ".credentials.json"
    cred_target = Path("/root/.claude/.credentials.json")
    if _path_readable(cred_target) and not cred_link.exists():
        try:
            cred_link.symlink_to(cred_target)
        except OSError:
            pass
    projects_link = claude_dir / "projects"
    projects_target = Path("/root/.claude/projects")
    if _path_readable(projects_target) and not projects_link.exists():
        try:
            projects_link.symlink_to(projects_target)
        except OSError:
            pass
    # Seed ~/.claude.json once. Without the `oauthAccount` + `userID`
    # keys claude pops the OAuth login dialog (credentials.json alone
    # isn't enough — claude checks ~/.claude.json for "login complete"
    # state). Two candidate sources, in priority order: the explicit
    # Docker mount (/root/host-claude.json) and the invoking user's own
    # ~/.claude.json (host deployment). The Dockerfile's `claude
    # --version` leaves a stub /root/.claude.json with no oauthAccount,
    # so prefer whichever source actually carries an account; only fall
    # back to an account-less file if that's all we have. After the copy
    # the per-agent file is writable so claude can update its own
    # session counters without affecting other agents.
    claude_json = home / ".claude.json"
    if not claude_json.exists():
        seed = _pick_claude_seed(
            [Path("/root/host-claude.json"), Path.home() / ".claude.json"])
        if seed is not None:
            try:
                claude_json.write_bytes(seed)
            except OSError:
                pass
    # Pre-trust the pane's working dir so claude doesn't block on the
    # folder-trust dialog. Path.cwd() is the spawn cwd inherited by the
    # tmux pane (same assumption as codex's ensure_workdir_trusted).
    if claude_json.exists():
        _mark_project_trusted(claude_json, Path.cwd())


# Per-CLI OAuth/credential file to copy from the operator HOME into the
# agent's isolated HOME so HOME isolation (Wave 2) doesn't log the CLI out.
# Keyed by the adapter's process_name() so the package-name aliases
# (kimi-cli / qwen-cli) collapse onto one entry. Value:
#   (rel  — path of the cred file under HOME, identical on both sides,
#    skip_env — env var whose presence means the CLI authenticates by API
#               key and needs no seeded OAuth file; None = always seed)
#
# claude-code is absent — its richer seeding (macOS keychain, ~/.claude.json,
# folder trust) lives in _ensure_claude_agent_home. kimi is absent on
# purpose: its adapter does NOT set HOME=<agent_home> (Plan B keeps cwd=repo
# with no native file), so the pane already inherits the operator's
# ~/.kimi/config.toml — there is nothing to isolate and nothing to seed.
# (expert K3 confirmed in the prod container: worker_kimi has no agent-home
# at all.) If kimi ever gains HOME isolation, add its seed entry here.
_CLI_CRED_SEEDS: dict[str, tuple[str, str | None]] = {
    "codex":  (".codex/auth.json", None),
    "gemini": (".gemini/oauth_creds.json", "GEMINI_API_KEY"),
    "qwen":   (".qwen/oauth_creds.json", "OPENAI_API_KEY"),
}


def _seed_cli_credentials(agent: str, cli: str) -> None:
    """Copy the operator's OAuth credential file for `cli` into the agent's
    isolated HOME, so the per-agent `HOME=<agent_home>` (codex: `CODEX_HOME`)
    doesn't strand the CLI at a fresh, logged-out state dir.

    Best-effort throughout — any of these silently skips, never aborting the
    provision:
      • CLI not in `_CLI_CRED_SEEDS` (claude handled elsewhere; kimi not
        isolated)
      • the skip-env is set (API-key auth → OAuth file unnecessary)
      • the operator source file is absent / unreadable (never logged in)
      • the dest already exists (don't clobber a refreshed per-agent token)
      • the dest HOME isn't writable

    Only the credential file is touched: codex's per-agent config.toml
    (written by `ensure_workdir_trusted`) lives beside auth.json and is
    never overwritten here."""
    try:
        adapter = get_adapter(cli)
    except KeyError:
        return
    spec = _CLI_CRED_SEEDS.get(adapter.process_name())
    if spec is None:
        return
    rel, skip_env = spec
    if skip_env and env_str(skip_env):
        return
    from claudeteam.agents.claude_code import agent_home as _agent_home
    src = Path.home() / rel
    dst = Path(_agent_home(agent)) / rel
    if dst.exists() or not _path_readable(src):
        return
    try:
        data = src.read_bytes()
    except OSError:
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    except OSError:
        pass


def _ensure_agent_home(agent: str, cli: str) -> None:
    """Provision the per-agent HOME for any CLI before spawn.

    claude-code gets the full seeding (keychain creds, settings.json,
    ~/.claude.json, folder trust) via `_ensure_claude_agent_home`. Every
    other CLI just needs its per-agent HOME *directory* to exist so the
    spawn's `HOME=<agent_home>` (and codex's `CODEX_HOME`) lands the CLI's
    own config / cache / native-memory file in an isolated dir instead of
    racing the operator HOME across panes. The native memory file itself
    (AGENTS.md / GEMINI.md / QWEN.md) is written by `identity.write` via
    `atomic_write_text`, which creates its own parent dir — so all we owe
    here is the home root.

    For the HOME-isolated non-claude CLIs we additionally seed the
    operator's OAuth credential into the isolated HOME (see
    `_seed_cli_credentials`), otherwise the fresh state dir would leave the
    CLI logged out.

    Best-effort: an unwritable path must not fail the whole provision (the
    init-prompt memory injection still delivers identity + digest)."""
    if cli == "claude-code":
        _ensure_claude_agent_home(agent)
        return
    from claudeteam.agents.claude_code import agent_home as _agent_home
    try:
        Path(_agent_home(agent)).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    _seed_cli_credentials(agent, cli)


def _seed_lark_profile(agent: str) -> None:
    """Give an isolated agent HOME the team's bot profile for Docs/Drive.

    `claudeteam say` can use propagated app env vars, but a direct
    `lark-cli --profile ... docs/drive` command needs a config.json under
    the agent's own HOME. Seed only this team's bot identity, never the
    operator's user OAuth tokens or other app profiles.
    """
    profile = config.lark_profile()
    app_id = env_str("FEISHU_APP_ID") or env_str("LARKSUITE_CLI_APP_ID")
    app_secret = (env_str("FEISHU_APP_SECRET")
                  or env_str("LARKSUITE_CLI_APP_SECRET"))
    if not (profile and app_id and app_secret):
        return
    from claudeteam.agents.claude_code import agent_home
    dest = Path(agent_home(agent)) / ".lark-cli" / "config.json"
    temp_path = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if dest.exists():
            data = json.loads(dest.read_text(encoding="utf-8"))
            apps = data.get("apps")
            if not isinstance(apps, list):
                return  # leave an agent-managed config untouched
            for app in apps:
                if isinstance(app, dict) and app.get("name") == profile:
                    if (app.get("appId") == app_id
                            and app.get("appSecret") == app_secret):
                        return
                    apps.remove(app)
                    break
        else:
            data = {"apps": []}
            apps = data["apps"]
        apps.append({"name": profile, "appId": app_id,
                     "appSecret": app_secret, "brand": "feishu",
                     "lang": "zh_cn", "users": []})
        # Keep the secret private even on hosts with a permissive umask.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=dest.parent, prefix=".config-",
                                         delete=False) as tmp:
            json.dump(data, tmp, ensure_ascii=False)
            temp_path = tmp.name
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, dest)
        os.chmod(dest, 0o600)
    except (OSError, ValueError, TypeError):
        # Profile seeding is best-effort; a read-only HOME must not stop
        # the agent's primary CLI from launching.
        return
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def pane_env_prefix() -> str:
    """Build a shell env prefix that, prepended to a spawn_cmd, makes the
    spawned process inherit CLAUDETEAM_STATE_DIR and the Feishu env so
    worker agents calling `claudeteam say` write to the project state
    dir, not `~/.claudeteam`.
    """
    parts = [f"CLAUDETEAM_STATE_DIR={shlex.quote(str(paths.state_dir()))}"]
    for var in _PROPAGATED_ENV:
        val = env_str(var)
        if val:
            parts.append(f"{var}={shlex.quote(val)}")
    return " ".join(parts)


# Outcome strings returned by provision_pane. Callers print/log differently
# (start uses loop-style "  → spawned", hire uses "✅ hired") so the helper
# stays I/O-free and lets the caller render.
LAZY = "lazy"
READY = "ready"
READY_NO_INIT = "ready_no_init"
SPAWN_FAILED = "spawn_failed"
CONFIG_ERROR = "config_error"


def provision_pane(agent: str, target: tmux.Target) -> str:
    """Provision a freshly-created pane for `agent`.

    Pre-conditions: tmux window for `target` already exists and is empty
    (a shell prompt). Caller is responsible for window creation.

    Steps:
      1. Render + persist agent's identity.md (`agents/<name>/identity.md`).
      2. If agent is `lazy` in team.json: set status 待命, return LAZY.
      3. For codex CLI: ensure cwd is trusted in ~/.codex/config.toml.
      4. Spawn the adapter's CLI in the pane (with pane_env_prefix).
      5. Wait up to 20s for the adapter's ready marker to appear.
      6. Inject the identity init prompt so the agent reads identity.md
         and reports for duty.
      7. Set status 进行中.

    Returns one of:
      LAZY            — status set to 待命, no CLI spawn attempted
      READY           — CLI spawned + identity init injected
      READY_NO_INIT   — CLI spawned but ready marker didn't appear in 20s
      SPAWN_FAILED    — tmux.spawn_agent returned False
      CONFIG_ERROR    — agent's `cli` value isn't registered (typo /
                        missing adapter); caller should warn + continue
                        with the rest of the team, NOT kill the whole start.
    """
    # Load team config once. start.py loops over N agents calling this
    # helper, so paying 3-4 disk reads here per agent (one for cfg, one
    # for adapter resolution, one for model fallback) compounds. Cache
    # locally and derive cfg / cli / model from the same dict.
    team = config.load_team()
    cfg = team.get("agents", {}).get(agent)
    if cfg is None:
        import sys
        print(f"  ⚠️ {agent}: agent {agent!r} not in team.json", file=sys.stderr)
        return CONFIG_ERROR
    cli = cfg.get("cli", "claude-code")
    # Inline agent_model resolution: per-agent override → env var →
    # team default → "opus". Mirrors `config.agent_model` but uses the
    # already-loaded `team` dict for the default_model fallback.
    model = (cfg.get("model")
             or env_str("CLAUDETEAM_DEFAULT_MODEL")
             or team.get("default_model", "opus"))
    # Pass resolved fields to identity.write so its internal render()
    # skips a redundant config.agent_config() fallback. `role`
    # defaulting to `agent` matches render's own fallback so the
    # rendered file is byte-identical.
    identity.write(agent, role=cfg.get("role") or agent, cli=cli, model=model)
    _ensure_agent_home(agent, cli)
    _seed_lark_profile(agent)
    if cfg.get("lazy"):
        local_facts.upsert_status(agent, "待命", "lazy: CLI starts on first message")
        return LAZY
    if cli == "codex-cli":
        from claudeteam.agents.codex_cli import codex_home
        ensure_workdir_trusted(
            Path.cwd(), config_path=Path(codex_home(agent)) / "config.toml")
    try:
        adapter = get_adapter(cli)
    except KeyError as e:
        # Bad `cli` value in team.json — typo, dropped adapter, etc. One
        # bad agent shouldn't kill `claudeteam start` for the rest of
        # the team. Caller logs + skips.
        import sys
        print(f"  ⚠️ {agent}: {e}", file=sys.stderr)
        return CONFIG_ERROR
    cmd = f"{pane_env_prefix()} {adapter.spawn_cmd(agent, model)}"
    if not tmux.spawn_agent(target, cmd):
        return SPAWN_FAILED
    # 60s ready timeout (was 20s): fresh container claude panes go
    # through up to 3 first-launch dialogs (theme picker / auth-method
    # picker / bypass-permissions confirm) before the ready marker
    # appears. The poll loop auto-Enters each dialog at ~1Hz, so a
    # 3-dialog chain plus boot time can run 30-40s; 60s gives headroom.
    from claudeteam.runtime import tunables
    ready_timeout = float(tunables.tunable("wake.ready_marker_timeout_s", 60.0))
    if wake.wait_until_ready(target, adapter, timeout_s=ready_timeout):
        tmux.inject(target, identity.init_prompt(agent),
                    submit_keys=adapter.submit_keys())
        outcome = READY
    else:
        outcome = READY_NO_INIT
    local_facts.upsert_status(agent, "进行中", "initializing")
    return outcome
