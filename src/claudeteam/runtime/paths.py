"""Single source of truth for runtime filesystem paths.

All paths derive from `$CLAUDETEAM_STATE_DIR` (re-read on every call so
tests get isolation by setting the env, not by monkey-patching).  When
not set, falls back to `~/.claudeteam`.

Layout:
    $CLAUDETEAM_STATE_DIR/
        facts/             ← inbox.json, status.json, logs.jsonl, heartbeats.json
        agents/<name>/     ← per-agent business state: identity.md + memory.jsonl
        agent-home/<name>/ ← claude-code's per-agent HOME (adapter detail)
        router.pid         ← daemon pid files
        watchdog.pid
        router.cursor      ← catchup replay state
"""
from __future__ import annotations

from pathlib import Path

from claudeteam.util import env_path


def state_dir() -> Path:
    """Top-level directory for all runtime state."""
    return env_path("CLAUDETEAM_STATE_DIR") or Path.home() / ".claudeteam"


def facts_dir() -> Path:
    """Where local_facts stores inbox / status / log / heartbeats."""
    return state_dir() / "facts"


def agent_dir(agent: str) -> Path:
    """Per-agent business-state dir: identity.md + memory.jsonl.

    CLI-agnostic — the same location for every agent regardless of which
    CLI it runs under. Deliberately distinct from claude-code's per-agent
    HOME (`agent-home/<name>`, an adapter runtime detail where claude
    looks for ~/.claude): this is where ClaudeTeam's own durable per-agent
    state lives, and the native CLAUDE.md under agent-home is a projection
    of it."""
    return state_dir() / "agents" / agent


def state_file(name: str) -> Path:
    """A file under state_dir. Caller is responsible for mkdir before writing
    — pure path resolution, no I/O side effects."""
    return state_dir() / name


def router_pid_file() -> Path:
    return state_file("router.pid")


def router_cursor_file() -> Path:
    return state_file("router.cursor")


def router_log_file() -> Path:
    return state_file("router.log")


def router_seen_file() -> Path:
    return state_file("router.seen")


def config_file() -> Path:
    """Path to the unified TOML config file (replaces team.json +
    runtime_config.json). Resolution order: CLAUDETEAM_CONFIG_FILE env →
    `./claudeteam.toml` relative to cwd (if it exists) → `<state_dir>/..`/
    claudeteam.toml. The last fallback is cwd-INDEPENDENT: CLAUDETEAM_STATE_DIR
    is always propagated into every spawned pane / router / send subprocess, and
    the unified TOML lives one level above the state dir. Without it, any
    claudeteam subprocess invoked from a non-project cwd (router inject path, a
    worker's `claudeteam send`) silently fell back to the legacy team.json,
    which doesn't exist → empty team → 'agent <x> not in team.json' and a failed
    best-effort pane wake."""
    from claudeteam.util import env_path
    explicit = env_path("CLAUDETEAM_CONFIG_FILE")
    if explicit:
        return explicit
    cwd_cfg = Path.cwd() / "claudeteam.toml"
    if cwd_cfg.exists():
        return cwd_cfg
    return state_dir().parent / "claudeteam.toml"


def watchdog_pid_file() -> Path:
    return state_file("watchdog.pid")


def watchdog_log_file() -> Path:
    return state_file("watchdog.log")


def ensure_state_dir() -> Path:
    """Create state_dir if missing and return it. Use when about to write."""
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    return sd
