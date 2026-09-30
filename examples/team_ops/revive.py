#!/usr/bin/env python3
"""复活一个 ClaudeTeam 团队的 pane，可选从上次会话断点续起。

用法（由 revive.sh 调用，团队 env 已 source 好）：
    python3 revive.py <session> <workdir> <agent> [<agent> ...] [--resume] [--max-lines N]

--resume 的两条路径是不一样的，别合并：

  claude  →  spawn_cmd 里加 `--resume <sid>`，其余走 lifecycle.provision_pane
             不动。等 ready marker + tmux 注入身份块都正常工作。

  codex   →  必须绕开 provision_pane。两个原因：
             1) codex 的 ready_markers 是启动横幅 ["OpenAI Codex",
                "permissions: YOLO"]，而 resume 的会话不打横幅、直接打旧记录，
                ready 检测会误判（旧记录里碰巧有这些字就假 ready）。
             2) 误判之后 tmux 注入抢在 TUI 起来之前，身份块掉进 tty 缓冲区
                再也出不来 —— pane 看着活着，实际 0% CPU、0 连接、什么都不做。
                2026-09-17 20:45 实测就是这个现象。
             解法：`codex resume <sid> <PROMPT>`，身份块走命令行位置参数，
             完全不用 tmux 粘贴。顺带也躲开了 codex 被超长粘贴冲垮时的
             "timed out discarding buffered terminal input"。

会话挑选：按 mtime 倒序扫该 agent 自己 HOME 下的会话，跳过行数 < MIN_LINES 的。
崩溃循环会留下几秒就退的死胎会话，`--last` / `--continue` 会一头撞上去，
把真正的历史埋掉；自己挑 id 才躲得开。
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

MIN_LINES = 10          # 低于这个基本是启动即死的死胎会话


def _claude_sid(home: Path, workdir: str) -> tuple[str | None, int]:
    slug = "-" + workdir.strip("/").replace("/", "-")
    d = home / ".claude" / "projects" / slug
    if not d.is_dir():
        return None, 0
    for f in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        n = sum(1 for _ in f.open(errors="ignore"))
        if n >= MIN_LINES:
            return f.stem, n
    return None, 0


def _codex_sid(home: Path) -> tuple[str | None, int]:
    d = home / ".codex" / "sessions"
    if not d.is_dir():
        return None, 0
    for f in sorted(d.rglob("rollout-*.jsonl"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        n = sum(1 for _ in f.open(errors="ignore"))
        if n < MIN_LINES:
            continue
        sid = None
        try:
            payload = json.loads(f.open(errors="ignore").readline())["payload"]
            # codex 0.130 及更早写的是 payload.id，新版是 payload.session_id
            sid = payload.get("session_id") or payload.get("id")
        except (json.JSONDecodeError, KeyError, OSError):
            pass
        if not sid:
            # 兜底：rollout-<ISO时间戳>-<uuid>.jsonl
            m = re.search(r"-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$", f.name)
            sid = m.group(1) if m else None
        if sid:
            return sid, n
    return None, 0


def main() -> int:
    args = sys.argv[1:]
    resume = "--resume" in args
    args = [a for a in args if a != "--resume"]
    max_lines = 0
    if "--max-lines" in args:
        i = args.index("--max-lines")
        max_lines = int(args[i + 1])
        del args[i:i + 2]
    session, workdir, *agents = args

    from claudeteam.runtime import tmux, lifecycle, config
    from claudeteam.store import local_facts
    from claudeteam.agents import identity, claude_code, codex_cli

    team = config.load_team()
    agent_cfg = team.get("agents", {})

    # —— 挑 session id ——
    sids: dict[str, str] = {}
    if resume:
        print("  ── 会话挑选 ──")
        for a in agents:
            cli = agent_cfg.get(a, {}).get("cli", "claude-code")
            home = Path(claude_code.agent_home(a))
            sid, n = (_claude_sid(home, workdir) if cli == "claude-code"
                      else _codex_sid(home))
            if not sid:
                print(f"    {a:<14} 无够格会话 → 干净起")
                continue
            if max_lines and n > max_lines:
                print(f"    {a:<14} {n} 行 > 上限 {max_lines} → 干净起（避免开局即压缩）")
                continue
            sids[a] = sid
            print(f"    {a:<14} {n:>6} 行  {sid}")
        print()

    # —— claude: 把 --resume 塞进 spawn_cmd ——
    _orig = claude_code.ClaudeCodeAdapter.spawn_cmd

    def _patched(self, agent, model):
        cmd = _orig(self, agent, model)
        sid = sids.get(agent)
        if sid:
            cmd = cmd.replace("claude --dangerously-skip-permissions",
                              f"claude --dangerously-skip-permissions --resume {sid}")
        return cmd

    claude_code.ClaudeCodeAdapter.spawn_cmd = _patched

    def provision_codex(agent: str, target: tmux.Target, sid: str | None) -> str:
        """codex 专用路径：身份块走命令行位置参数，永不用 tmux 粘贴。

        resume 时必须这样（见模块 docstring）。干净起时也这样，因为身份块
        动辄 9000 字符，tmux 粘贴会把 codex 冲垮：
            Error: timed out discarding buffered terminal input
        codex 直接退出，剩下的提示词被 bash 当命令执行，满屏 command not
        found —— 2026-09-17 example2 和 ClaudeTeam2:CD2 都是这么死的。
        `codex [PROMPT]` 和 `codex resume <SID> [PROMPT]` 都收位置参数。
        """
        cfg = agent_cfg.get(agent, {})
        model = (cfg.get("model") or team.get("default_model", "opus"))
        identity.write(agent, role=cfg.get("role") or agent,
                       cli="codex-cli", model=model)
        lifecycle._ensure_agent_home(agent, "codex-cli")
        codex_cli.ensure_workdir_trusted(
            Path(workdir),
            config_path=Path(codex_cli.codex_home(agent)) / "config.toml")
        base = codex_cli.CodexCliAdapter().spawn_cmd(agent, model)
        # 提示词落盘、命令里只放 "$(cat 文件)"，而不是把 12000+ 字符直接
        # 塞进命令行。spawn 走的是 tmux send-keys，实测上限在 16000~20000
        # 字符之间（8000/12000/16000 过，20000 失败），shlex.quote 之后
        # exp2eval2 的 12737 字符身份块就顶穿了 —— 2026-09-20 表现为
        # SPAWN_FAILED，pane 停在裸 shell。走文件则 tmux 只看到一行短命令，
        # 展开由 pane 里的 bash 完成，argv 照样拿到完整提示词。
        pf = Path(lifecycle.paths.state_dir()) / "agents" / agent / "init_prompt.txt"
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(identity.init_prompt(agent), encoding="utf-8")
        prompt = f'"$(cat {shlex.quote(str(pf))})"'
        cmd = (base.replace(" codex ", f" codex resume {sid} {prompt} ")
               if sid else f"{base} {prompt}")
        cmd = f"{lifecycle.pane_env_prefix()} {cmd}"
        if not tmux.spawn_agent(target, cmd):
            return "SPAWN_FAILED"
        local_facts.upsert_status(agent, "进行中",
                                  "resumed" if sid else "initializing")
        return "ready (resumed)" if sid else "ready"

    print("  ── provision ──")
    rc = 0
    for a in agents:
        target = tmux.Target(session, a)
        cli = agent_cfg.get(a, {}).get("cli", "claude-code")
        sid = sids.get(a)
        try:
            if cli == "codex-cli" and not agent_cfg.get(a, {}).get("lazy"):
                out = provision_codex(a, target, sid)
            else:
                out = lifecycle.provision_pane(a, target)
                if sid:
                    out += " (resumed)"
        except Exception as e:                      # 一个人挂了不拖累全队
            out = f"❌ {type(e).__name__}: {e}"
            rc = 1
        if out.split()[0] in {"SPAWN_FAILED", lifecycle.SPAWN_FAILED,
                             lifecycle.CONFIG_ERROR}:
            rc = 1
        print(f"    {a:<14} {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
