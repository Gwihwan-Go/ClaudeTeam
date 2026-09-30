"""OpenAI Codex CLI adapter.

Codex only accepts OpenAI-native model names (gpt-/o1/o3/o4/codex prefixes);
other aliases (sonnet/opus/haiku) are silently dropped so Codex falls back
to its configured default.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from .base import CliAdapter, MULTILINE_SUBMIT_KEYS, SPINNER_CHARS
from .claude_code import agent_home


def codex_home(agent: str) -> str:
    """Per-agent CODEX_HOME: `<agent_home>/.codex`. Isolates each pane's
    trust config and AGENTS.md memory so sibling codex panes don't clobber
    one shared ~/.codex.
    """
    return f"{agent_home(agent)}/.codex"


def ensure_workdir_trusted(workdir: Path,
                           config_path: Path | None = None) -> None:
    """Pre-trust `workdir` in CODEX_HOME/config.toml so the first-run
    "Do you trust this directory?" prompt doesn't block a freshly-spawned
    pane. Idempotent: a no-op if the entry already exists.

    `config_path` is injectable for tests (and per-agent provisioning).
    """
    cfg = config_path or (Path.home() / ".codex" / "config.toml")
    entry = f'[projects."{workdir}"]\ntrust_level = "trusted"\n'
    if cfg.exists():
        existing = cfg.read_text(encoding="utf-8")
        if f'[projects."{workdir}"]' in existing:
            return
        cfg.write_text(existing.rstrip() + "\n\n" + entry, encoding="utf-8")
    else:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(entry, encoding="utf-8")


_OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4", "codex")


class CodexCliAdapter(CliAdapter):
    def spawn_cmd(self, agent: str, model: str) -> str:
        args = ["--dangerously-bypass-approvals-and-sandbox"]
        if model and any(model.startswith(p) for p in _OPENAI_PREFIXES):
            args += ["--model", model]
        quoted = " ".join(shlex.quote(a) for a in args)
        # Clear OPENAI_API_KEY / OPENAI_ADMIN_KEY at spawn time so codex
        # falls back to ~/.codex/auth.json's auth_mode (chatgpt OAuth via
        # ChatGPT-Plus). Without this, the operator's shell-level
        # OPENAI_API_KEY (set in ~/.env.sh for unrelated tooling) overrides
        # auth.json and forces codex into apikey mode → OpenAI quota
        # exceeded, even though the subscription path was the intended one.
        return (f"OPENAI_API_KEY= OPENAI_ADMIN_KEY= "
                f"CODEX_HOME={shlex.quote(codex_home(agent))} "
                f"CODEX_AGENT={shlex.quote(agent)} codex {quoted}")

    def display_model(self, model: str) -> str:
        # Only OpenAI-prefixed models reach codex via --model; anything
        # else is dropped and codex runs its own configured default, so
        # don't label the agent with a model it isn't running.
        if model and any(model.startswith(p) for p in _OPENAI_PREFIXES):
            return model
        return "codex 自身配置"

    def native_memory_path(self, agent: str) -> str:
        # Codex reads $CODEX_HOME/AGENTS.md as global memory at session
        # start (AGENTS.override.md wins if present; we don't write it).
        # It does NOT re-read from disk after its own context compaction,
        # so a mid-session anchor change still needs a reidentify inject.
        return f"{codex_home(agent)}/AGENTS.md"

    def ready_markers(self) -> list[str]:
        # Banner lines after CLI 0.124+ becomes interactive.  Avoids matching
        # the spawn-command echo that includes "gpt-5".
        return ["OpenAI Codex", "permissions: YOLO"]

    def busy_markers(self) -> list[str]:
        return ["esc to interrupt", "Booting MCP server", *SPINNER_CHARS]

    def process_name(self) -> str:
        return "codex"

    def submit_keys(self) -> list[str]:
        return list(MULTILINE_SUBMIT_KEYS)
