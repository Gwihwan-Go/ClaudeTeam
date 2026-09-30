"""`claudeteam watchdog`

Long-running supervisor that keeps the router (and any future daemons)
alive. Runs `runtime.watchdog.supervise` every
`watchdog.check_interval_s` seconds (claudeteam.toml; default 30) until
SIGTERM / Ctrl-C.

Self-locks via state_dir/watchdog.pid so two watchdogs can't fight.

Cooldown alerts:
- When a supervised daemon enters cooldown (max_retries respawns
  failed), the watchdog posts to Feishu chat so the boss sees the
  death without tailing the watchdog log.
- The alert is a red Feishu card with a 3-step recovery checklist
  (`claudeteam health` / read daemon log / `claudeteam down && up`
  after fix). Falls back to plain `send_text` on card schema
  rejection so the alert still lands.
- alert_fn is None when chat_id is unset — alerts are pointless
  without a delivery target; boot banner says "no chat alerts" so
  the operator knows.

Claude OAuth keep-alive:
- Bind-mounted claude .credentials.json expires during idle and
  the in-pane claude only refreshes on API call (not idle), which
  killed boss-message routing after long silences. Watchdog now
  proactively reads `expiresAt` every
  `watchdog.cred_check_interval_s` seconds (default 300); if
  the token's < `watchdog.cred_refresh_ahead_s` (default 1800) from
  expiry, run `claude -p "Return only OK"` once. That triggers
  claude to refresh the token in-place (file is bind-mounted RW so
  the new token persists back to host). All agent panes share the
  same file via per-agent symlink, so one refresh covers the whole
  team.

All alert paths are best-effort: chat send / card send failures are
swallowed at the alert_fn level (and runtime/watchdog's supervise
also try/excepts alert_fn). A broken alert path mustn't kill the
supervisor.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from claudeteam.feishu import chat as _chat
from claudeteam.feishu.cards import simple_card
from claudeteam.runtime import config, paths, pidlock, tunables, watchdog
from claudeteam.util import maybe_print_help


_CRED_PATH = Path.home() / ".claude" / ".credentials.json"
# Resolves to /root/.claude/.credentials.json in Docker (HOME=/root) — same
# path the host-keychain bind-mount lands on — and to ~/.claude/... on host.
# Hardcoding /root broke host non-root deploys: Path("/root/...").exists()
# raised PermissionError (Linux /root is 700) instead of returning False
# under Python 3.10–3.12, killing `claudeteam up`. Caught 2026-05-08.


def _make_alert_fn():
    """Build the alert callable handed to `supervise`. Captures chat_id +
    profile at construction time (cheap reads of runtime_config.json)
    so each cooldown event sends without re-reading config.

    Returns None when chat_id is unset — alerts are pointless without a
    delivery target, and a None alert_fn is the supervise default.

    Sends as a red Feishu card so the cooldown event is visually
    distinct from normal /team / /health cards. Falls back to plain
    text if send_card raises (schema mismatch on older lark builds).
    """
    chat_id = config.chat_id()
    if not chat_id:
        return None
    profile = config.lark_profile()

    def alert(name: str, failed_at: int, cooldown_secs: int) -> None:
        title = f"🚨 watchdog: {name} entered cooldown"
        body = (
            f"daemon **{name}** entered **{cooldown_secs}s cooldown** "
            f"after **{failed_at}** failed respawns.\n\n"
            f"- `claudeteam health` for current state\n"
            f"- check daemon log for root cause\n"
            f"- after fix: `claudeteam down && claudeteam up`"
        )
        from claudeteam.runtime import tunables
        alarm_color = str(tunables.tunable("router.alarm_card_color", "red"))
        card = simple_card(title, body, color=alarm_color)
        try:
            _chat.send_card(chat_id, card, profile=profile, as_user=False)
        except Exception as e:
            # Card delivery shouldn't kill the watchdog. Fall back to
            # plain text so the alert still lands somehow; if THAT also
            # fails the supervise outer try/except logs it.
            print(f"  ⚠️ watchdog: card alert send failed ({e}); falling back to text")
            _chat.send_text(chat_id,
                            f"🚨 watchdog: {name} entered {cooldown_secs}s cooldown "
                            f"after {failed_at} failed respawns",
                            profile=profile, as_user=False)

    return alert


def main(argv: list[str]) -> int:
    if maybe_print_help(argv, "usage: claudeteam watchdog"):
        return 0
    pid_file = paths.watchdog_pid_file()
    if not pidlock.acquire(pid_file, name="watchdog"):
        return 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    specs = watchdog.default_specs()
    states: dict = {}
    alert_fn = _make_alert_fn()
    alert_msg = "with chat alerts" if alert_fn else "no chat alerts (chat_id unset)"
    check_interval_s = int(tunables.tunable("watchdog.check_interval_s", 30))
    cred_check_interval_s = int(tunables.tunable("watchdog.cred_check_interval_s", 300))
    print(f"🐕 watchdog supervising {[s.name for s in specs]} every {check_interval_s}s ({alert_msg})")

    last_cred_check = 0.0
    try:
        while True:
            watchdog.supervise(specs, states, alert_fn=alert_fn)
            now = time.time()
            if now - last_cred_check >= cred_check_interval_s:
                # Claude cred-propagate RETIRED 2026-07-09. All claude
                # panes auth via the long-lived CLAUDE_CODE_OAUTH_TOKEN
                # env var (threaded through lifecycle._PROPAGATED_ENV);
                # agent-homes carry no .credentials.json. Propagating the
                # host file re-introduced file-based auth with a rotating
                # refresh_token race: one pane refreshes+rotates, the next
                # propagate stomps every OTHER pane's rotated token with
                # the host's now-stale bytes → fleet-wide 401. The host
                # file also belongs to the operator's own interactive
                # claude sessions — it must never be treated as a
                # ClaudeTeam-owned artifact.
                #   _maybe_refresh_claude_oauth(now)   # retired with it
                #   _propagate_host_creds_if_newer()
                # codex is different: no env-var auth path exists, panes
                # MUST use auth.json files, so fleet-sync (newest wins)
                # remains the least-bad option for its rotating token.
                _sync_codex_creds_across_fleet()
                last_cred_check = now
            time.sleep(check_interval_s)
    except KeyboardInterrupt:
        print("watchdog stopped")
        return 0
    finally:
        pidlock.release(pid_file)


def _maybe_refresh_claude_oauth(now: float) -> None:
    """If the bind-mounted claude .credentials.json expires within
    `watchdog.cred_refresh_ahead_s` (claudeteam.toml; default 1800),
    force-refresh by spawning a brief `claude -p "Return only OK"`.
    That subprocess hits the Anthropic API which makes claude rotate
    the access token in-place. File is bind-mounted RW so the new
    token persists to host.

    Best-effort: any failure (file missing, parse error, claude crashes,
    network down) logs a warning but doesn't kill the watchdog. Worst
    case the boss still sees expired-token errors next cycle and runs
    `make creds` manually.
    """
    if not _CRED_PATH.exists():
        # Host deploy (macOS): no /root mount, claude OAuth lives in
        # keychain not file. Silent skip — printing every 5min spams
        # watchdog.log with hundreds of false alarms.
        return
    try:
        oauth = json.loads(_CRED_PATH.read_text())["claudeAiOauth"]
        expires_ms = int(oauth.get("expiresAt", 0))
    except (OSError, ValueError, KeyError) as e:
        print(f"  ⚠️ cred-refresh: read {_CRED_PATH} failed: {e}")
        return
    remaining = expires_ms / 1000 - now
    cred_refresh_ahead_s = int(tunables.tunable("watchdog.cred_refresh_ahead_s", 1800))
    if remaining > cred_refresh_ahead_s:
        return  # plenty of time; skip
    print(f"  🔑 claude token expires in {int(remaining)}s — forcing refresh")
    try:
        r = subprocess.run(
            ["claude", "-p", "Return only OK"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  ⚠️ cred-refresh: `claude -p` failed: {e}")
        return
    if r.returncode != 0:
        snippet = (r.stderr or r.stdout or "").strip()[:120]
        print(f"  ⚠️ cred-refresh: claude rc={r.returncode}: {snippet}")
        return
    print("  ✅ claude token refreshed")
    _propagate_host_creds_to_agents()


def _propagate_host_creds_if_newer() -> None:
    """Sync host ~/.claude/.credentials.json to agent-home snapshots
    when the host file is newer (mtime-based). Quieter than the always-
    log variant — only prints when a copy actually happens, so the
    watchdog log doesn't bloat with no-op cycles."""
    try:
        host_mtime = _CRED_PATH.stat().st_mtime
    except OSError:
        return
    agent_root = paths.state_dir() / "agent-home"
    if not agent_root.is_dir():
        return
    stale = []
    for agent_dir in agent_root.iterdir():
        if not agent_dir.is_dir():
            continue
        target = agent_dir / ".claude" / ".credentials.json"
        try:
            if not target.exists() or target.stat().st_mtime < host_mtime - 1:
                stale.append(target)
        except OSError:
            continue
    if not stale:
        return
    _propagate_host_creds_to_agents()


def _propagate_host_creds_to_agents() -> None:
    """Copy the host ~/.claude/.credentials.json to every per-agent
    HOME under <state_dir>/agent-home/<agent>/.claude/. Without this,
    panes use their stale snapshot taken at provision time and hit
    401 after ~6h. Best-effort; logs each per-agent failure but does
    not raise."""
    try:
        host_bytes = _CRED_PATH.read_bytes()
    except OSError as e:
        print(f"  ⚠️ cred-propagate: read host creds failed: {e}")
        return
    agent_root = paths.state_dir() / "agent-home"
    if not agent_root.is_dir():
        return
    copied = 0
    for agent_dir in agent_root.iterdir():
        if not agent_dir.is_dir():
            continue
        target = agent_dir / ".claude" / ".credentials.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(host_bytes)
            copied += 1
        except OSError as e:
            print(f"  ⚠️ cred-propagate: {agent_dir.name} failed: {e}")
    if copied:
        print(f"  ✅ propagated fresh creds to {copied} agent-home(s)")


_CODEX_AUTH_REL = Path(".codex/auth.json")


def _sync_codex_creds_across_fleet() -> None:
    """Sync codex ChatGPT-OAuth auth.json across all codex agent-homes AND
    the operator's host `~/.codex/auth.json`.

    Unlike claude which has a stable long-lived token stored in a file,
    codex's ChatGPT OAuth uses a **rotating refresh_token**: every refresh
    invalidates the previous refresh_token and writes a new one back to
    the caller's auth.json. When N codex panes each spawned with an
    identical auth.json byte-copy race to refresh, the first wins and
    rotates; the remaining N-1 die with "refresh token was already used"
    the next time they try to refresh.

    Strategy: at each cycle, scan every candidate (host + per-agent) for
    the newest `last_refresh` timestamp, then rewrite the older peers to
    match it. Operator only ever needs to `codex login` in ONE place
    (host OR any codex pane); within ≤ cred_check_interval_s the whole
    fleet inherits the fresh refresh_token.

    Best-effort: silently swallows read/parse/write errors so a single
    bad JSON file doesn't kill the whole cycle.
    """
    host = Path.home() / _CODEX_AUTH_REL
    agent_root = paths.state_dir() / "agent-home"
    candidates: list[Path] = []
    if host.exists():
        candidates.append(host)
    if agent_root.is_dir():
        for agent_dir in agent_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
                continue
            af = agent_dir / _CODEX_AUTH_REL
            if af.exists():
                candidates.append(af)
    if len(candidates) < 2:
        return

    newest: Path | None = None
    newest_ts = ""
    for c in candidates:
        try:
            d = json.loads(c.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        ts = d.get("last_refresh") or ""
        # Reject future timestamps (clock skew, or a stray test fixture).
        # 2026-07-13 incident: a mock auth.json with last_refresh=2027-01-01
        # permanently won the newest-wins contest and re-stomped every real
        # login within one sync cycle. ISO-8601 sorts lexicographically, so
        # compare against "now + 1h" rendered in the same format.
        import datetime as _dt
        horizon = (_dt.datetime.now(_dt.timezone.utc)
                   + _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        if isinstance(ts, str) and ts > newest_ts and ts[:19] <= horizon:
            newest_ts = ts
            newest = c
    if not newest or not newest_ts:
        return

    try:
        src_bytes = newest.read_bytes()
    except OSError:
        return
    synced = 0
    for dst in candidates:
        if dst == newest:
            continue
        try:
            if dst.read_bytes() == src_bytes:
                continue  # already matches, no-op
        except OSError:
            pass
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: temp file + rename, so codex mid-read never
            # sees a half-written auth.json.
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            tmp.write_bytes(src_bytes)
            tmp.chmod(0o600)
            tmp.replace(dst)
            synced += 1
        except OSError as e:
            print(f"  ⚠️ codex-sync: {dst.parent.parent.name} failed: {e}")
    if synced:
        src_name = newest.parent.parent.name if newest != host else "host"
        print(f"  ✅ codex auth synced from {src_name} (last_refresh {newest_ts[:19]}) → {synced} peer(s)")
