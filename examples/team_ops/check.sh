#!/usr/bin/env bash
# 逐 pane 验收一个团队。 check.sh <session>
#
# 要盯的坑（都是真踩过的）：
#   Please run /login                     claude 掉 token
#   Invalid refresh / already used        codex 撞上 refresh_token 轮换竞争
#   Update available / Press enter        更新提示挡在前面，发 2+Enter 跳过
#   timed out discarding buffered input   codex 被超长粘贴冲垮，退回 bash
#   command not found                     同上的后果：提示词被当 shell 命令执行
set -uo pipefail
SOCKET="${CLAUDETEAM_TMUX_SOCKET:-${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/default}"
s="${1:?用法: check.sh <session>}"

windows=$(tmux -S "$SOCKET" list-windows -t "=$s" -F '#{window_name}' 2>/dev/null)
[ -z "$windows" ] && { echo "  ❌ session $s 不存在"; exit 1; }

bad=0
for w in $windows; do
  out=$(tmux -S "$SOCKET" capture-pane -p -t "=$s:$w" 2>/dev/null | grep -vE '^\s*$' | tail -8)
  flag="✅"
  echo "$out" | grep -qiE "Please run /login|Invalid API key|OAuth error" && flag="⚠️ LOGIN"
  echo "$out" | grep -qiE "Invalid refresh|refresh token was already used"  && flag="⚠️ CRED"
  echo "$out" | grep -qiE "Update available|Press enter to continue"        && flag="⚠️ UPDATE"
  echo "$out" | grep -qiE "command not found|timed out discarding"          && flag="⚠️ NO-CLI"
  printf "  %-14s %s\n" "$w" "$flag"
  [ "$flag" != "✅" ] && { echo "$out" | tail -3 | sed 's/^/        | /'; bad=1; }
done

echo
# 把名字打出来而不是只报数：roster 里标了 lazy 的 agent（如 ClaudeTeam2:sec）
# 本来就等第一条消息才起 CLI，只剩 shell 是正常的，报数会让人白查一遍。
idle=""
for w in $windows; do
  pp=$(tmux -S "$SOCKET" list-panes -t "=$s:$w" -F '#{pane_pid}' 2>/dev/null | head -1)
  [ -z "$(ps --ppid "$pp" -o comm= 2>/dev/null | head -1)" ] && idle="$idle $w"
done
[ -n "$idle" ] && echo "  ⚠️ 只剩 shell 的 pane:$idle   （roster 里标 lazy 的属正常）"

echo "  daemons: $(pgrep -fc 'claudeteam (router|watchdog)' 2>/dev/null) 个进程"
exit $bad
