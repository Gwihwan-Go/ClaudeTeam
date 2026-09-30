#!/usr/bin/env bash
# 复活一个 ClaudeTeam 团队。
#
#   revive.sh <team> [--resume] [--max-lines N] [agent ...]
#
#   revive.sh symcli --resume              # 默认名单，从上次断点续起
#   revive.sh symcli                       # 默认名单，干净起
#   revive.sh ClaudeTeam2 --resume         # 换团队
#   revive.sh symcli --resume manager data # 只复活指定的人
#   revive.sh symcli --resume --add paper   # 往活着的队伍里补一个人
#   revive.sh symcli --resume --max-lines 8000
#                                          # 超过 8000 行的会话不 resume，
#                                          # 避免开局就 auto-compact
#
# 不带 --resume 时行为和以前完全一致：干净起 + 注入身份摘要。
set -uo pipefail

TEAMS_DIR="${CLAUDETEAM_TEAMS_DIR:-$HOME/.claudeteam-teams}"
OPS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOCKET="${CLAUDETEAM_TMUX_SOCKET:-${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/default}"

team="${1:-}"; shift || true
[ -z "$team" ] && { echo "用法: revive.sh <team> [--resume] [--max-lines N] [agent ...]"; exit 1; }

# ── 各团队默认名单 ────────────────────────────────────────────
case "$team" in
  symcli)      env_file=symcli.env
               default_agents="manager example example2 exp2eval exp2eval2 exp2eval3 data paperdata swepruner paper pm refactor" ;;
  ClaudeTeam2) env_file=team2.env
               default_agents="manager CC1 CD1 CC2 CD2 sec worker_artist" ;;
  startup)     env_file=startup.env
               default_agents="manager CC1 CD1" ;;
  skilltest)   env_file=skilltest.env
               default_agents="manager worker_cc worker_codex" ;;
  *) echo "未知团队: $team （已知: symcli ClaudeTeam2 startup skilltest）"; exit 1 ;;
esac

resume_flag=""; max_lines=""; agents=""; add_mode=0
while [ $# -gt 0 ]; do
  case "$1" in
    --resume)     resume_flag="--resume"; shift ;;
    --add)        add_mode=1; shift ;;
    --max-lines)  max_lines="--max-lines $2"; shift 2 ;;
    *)            agents="$agents $1"; shift ;;
  esac
done
# --add 必须指名道姓，否则等于把全队重开一遍
[ "$add_mode" = 1 ] && [ -z "$agents" ] && { echo "--add 必须指定 agent 名"; exit 1; }
[ -z "$agents" ] && agents="$default_agents"

# Optional site-specific preflight (for example, checking the outbound proxy).
# The original deployment starts mihomo here; keep that policy outside the repo.
if [ -n "${CLAUDETEAM_PREFLIGHT:-}" ]; then
  bash "$CLAUDETEAM_PREFLIGHT" || exit 1
fi

# ── 1. 团队身份：三样必须同时对上，少一样就串团队 ───────────
# CLAUDETEAM_STATE_DIR / CLAUDETEAM_CONFIG_FILE / pane 的 cwd。
# 2026-07-14 ClaudeTeam2:CC1 从 ~/Symcli 起，结果往另一个群发消息。
source "$TEAMS_DIR/common.env" || exit 1
source "$TEAMS_DIR/$env_file" || exit 1
export CLAUDETEAM_STATE_DIR="$CLAUDETEAM_WORKDIR/state"
export CLAUDETEAM_CONFIG_FILE="${CLAUDETEAM_CONFIG_FILE:-$CLAUDETEAM_WORKDIR/claudeteam.toml}"
# Python tmux wrappers must use the same socket as this shell wrapper.
export TMUX="$SOCKET,0,0"
cd "$CLAUDETEAM_WORKDIR" || exit 1

echo "  团队 $CLAUDETEAM_SESSION  workdir=$CLAUDETEAM_WORKDIR  resume=${resume_flag:-off}"
echo "  名单:$agents"
echo

# ── 2. 建 session + 每人一个 window ──────────────────────────
# -t= 是精确匹配；不加 = 会前缀匹配到别的 session 上去。
if [ "$add_mode" = 1 ]; then
  # 往已有 session 里补人：只给缺 window 的 agent 建 window，其余不碰。
  tmux -S "$SOCKET" has-session -t="$CLAUDETEAM_SESSION" 2>/dev/null \
    || { echo "  ❌ session $CLAUDETEAM_SESSION 不存在，--add 无从谈起"; exit 1; }
  for a in $agents; do
    if tmux -S "$SOCKET" list-windows -t "=$CLAUDETEAM_SESSION" -F '#{window_name}' \
         2>/dev/null | grep -qx "$a"; then
      echo "  ♻️  $a 已有 window，先关掉重开"
      tmux -S "$SOCKET" kill-window -t "=$CLAUDETEAM_SESSION:$a" 2>/dev/null
    fi
    tmux -S "$SOCKET" new-window -t "$CLAUDETEAM_SESSION" -c "$CLAUDETEAM_WORKDIR" -n "$a"
  done
else
  first=$(echo $agents | awk '{print $1}')
  tmux -S "$SOCKET" kill-session -t="$CLAUDETEAM_SESSION" 2>/dev/null
  tmux -S "$SOCKET" new-session -d -s "$CLAUDETEAM_SESSION" \
       -c "$CLAUDETEAM_WORKDIR" -n "$first" || exit 1
  for a in $agents; do
    [ "$a" = "$first" ] && continue
    tmux -S "$SOCKET" new-window -t "$CLAUDETEAM_SESSION" -c "$CLAUDETEAM_WORKDIR" -n "$a"
  done
fi

# ── 3. provision（resume 的分流逻辑在 revive.py 里）──────────
python3 "$OPS_DIR/revive.py" "$CLAUDETEAM_SESSION" "$CLAUDETEAM_WORKDIR" \
        $agents $resume_flag $max_lines || exit 1

# ── 4. router + watchdog ────────────────────────────────────
# 先清掉指向"不是本团队守护进程"的陈旧 pidfile。
# 重启后 PID 会回绕，上一个 boot 留下的 pid 很可能落到一个 codex pane 上；
# 而每个 pane 的 cmdline 里都带着身份提示词、提示词里写满 `claudeteam inbox`
# 之类的用法，于是 is_alive() 误判成守护进程还活着，`claudeteam up` 就跳过
# 不启动了 —— 2026-09-20 symcli 的 router 就是这么失去监管、死了没人重生的。
# （watchdog.py 那边已把判据收紧成 `claudeteam <name>`，这里是双保险，
#  也顺手处理 pid 已死的情况。）
for d in router watchdog; do
  pf="$CLAUDETEAM_STATE_DIR/$d.pid"
  [ -f "$pf" ] || continue
  pid=$(cat "$pf" 2>/dev/null)
  args=$(ps -p "${pid:-0}" -o args= 2>/dev/null)
  case "$args" in
    *"claudeteam $d") : ;;                       # 货真价实，留着
    *) echo "  🧹 清理陈旧 $d.pid (pid=$pid)"; rm -f "$pf" ;;
  esac
done

echo
claudeteam up 2>&1 | tail -3 | sed 's/^/  /' || exit 1

echo
echo "  验收:  $OPS_DIR/check.sh $CLAUDETEAM_SESSION"
