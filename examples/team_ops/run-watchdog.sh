#!/usr/bin/env bash
# systemd user-service entry point for a single ClaudeTeam watchdog.
set -eo pipefail
team="${1:-}"
[[ "$team" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo 'usage: run-watchdog.sh <team>' >&2; exit 2; }
teams_dir="${CLAUDETEAM_TEAMS_DIR:-$HOME/.claudeteam-teams}"
source "$teams_dir/common.env"
source "$teams_dir/$team.env"
export CLAUDETEAM_STATE_DIR="$CLAUDETEAM_WORKDIR/state"
export CLAUDETEAM_CONFIG_FILE="$CLAUDETEAM_WORKDIR/claudeteam.toml"
socket="${CLAUDETEAM_TMUX_SOCKET:-${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/default}"
export TMUX="$socket,0,0"
cd "$CLAUDETEAM_WORKDIR"
exec claudeteam watchdog
