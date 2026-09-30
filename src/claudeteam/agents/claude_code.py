"""Anthropic Claude Code adapter."""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from claudeteam.runtime import paths

from .base import CliAdapter, SPINNER_CHARS


def _read_oauth_token(agent: str) -> str | None:
    """Read the access token from the per-agent .credentials.json.

    Returns None if the file is missing or its shape doesn't match what
    claude writes. Best-effort: we'd rather spawn claude without the env
    var (and let it fall back to keychain) than crash the pane on a
    parse error.
    """
    cred = Path(agent_home(agent)) / ".claude" / ".credentials.json"
    if not cred.exists():
        return None
    try:
        data = json.loads(cred.read_text())
        token = data.get("claudeAiOauth", {}).get("accessToken")
        return token if isinstance(token, str) and token else None
    except (OSError, json.JSONDecodeError):
        return None


def agent_home(agent: str) -> str:
    """Per-agent HOME for an isolated ~/.claude.json.

    Container deploys (Dockerfile mounts /data writable) use
    /data/agent-home/<agent>. Host deploys (macOS firmlink read-only;
    Linux without that mount) fall back to <state_dir>/agent-home/<agent>.

    Probe writability rather than just existence: a Linux server might
    have /data as a read-only data disk mount where mkdir would fail,
    and macOS Big Sur+ has /data as a firmlink that exists() reports
    True for in some setups but rejects writes. Cache the probe result
    so we don't pay an os.access call per spawn.
    """
    if _data_writable():
        return f"/data/agent-home/{agent}"
    return str(paths.state_dir() / "agent-home" / agent)


_DATA_WRITABLE: bool | None = None


def _data_writable() -> bool:
    """Cached probe of whether /data/agent-home is a real writable dir."""
    global _DATA_WRITABLE
    if _DATA_WRITABLE is None:
        base = Path("/data/agent-home")
        try:
            base.mkdir(parents=True, exist_ok=True)
            _DATA_WRITABLE = os.access(base, os.W_OK)
        except OSError:
            _DATA_WRITABLE = False
    return _DATA_WRITABLE


class ClaudeCodeAdapter(CliAdapter):
    def spawn_cmd(self, agent: str, model: str) -> str:
        # Full silent-launch recipe — bypass-permissions confirm, theme picker, etc.
        # - IS_SANDBOX=1: claude allows --dangerously-skip-permissions
        # - CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1 / DISABLE_AUTOUPDATER=1:
        #   silence survey + autoupdate banners.
        # - HOME=<agent_home>: per-agent home so each pane has its own
        #   ~/.claude.json — multiple panes sharing one config raced into
        #   "JSON Parse error" on restart. Lifecycle materialises the
        #   per-agent .claude/ from the live keychain (regular file, not
        #   symlink — claude's atomic-write replaces a symlink anyway).
        # The deployment supplies CLAUDE_CODE_OAUTH_TOKEN through
        # lifecycle.pane_env_prefix(). Do not replace it with a short-lived
        # access token from the operator's interactive credential file.
        return (
            f"HOME={agent_home(agent)} "
            f"CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1 DISABLE_AUTOUPDATER=1 "
            f"IS_SANDBOX=1 claude --dangerously-skip-permissions "
            f"--model {model} --name {agent}"
        )

    def ready_markers(self) -> list[str]:
        return ["bypass permissions on", "? for shortcuts"]

    def busy_markers(self) -> list[str]:
        return [
            *SPINNER_CHARS,
            "◐", "◑", "◒", "◓",
            "Thinking", "Running tool",
        ]

    def process_name(self) -> str:
        return "claude"

    def native_memory_reloads(self) -> bool:
        # claude re-reads ~/.claude/CLAUDE.md after /compact, so an on-disk
        # anchor rewrite reaches the running agent without a re-inject.
        return True

    def native_memory_path(self, agent: str) -> str | None:
        # ~/.claude/CLAUDE.md inside the agent's isolated HOME. claude
        # loads this as user-level memory on every session start and
        # re-reads it after /compact, so the agent's identity + remember
        # policy + memory digest survive context compaction. The per-agent
        # HOME means each agent gets its own file — zero cross-agent
        # collision and no project-repo pollution.
        return f"{agent_home(agent)}/.claude/CLAUDE.md"
