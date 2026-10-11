"""Router catchup-on-restart.

When the router daemon dies (Ctrl-C, OOM, host reboot), the live
`event +subscribe` stream resumes from the moment we re-attach — any
messages the boss sent during the gap are silently lost.

This module bridges that gap:

* `read_cursor` / `write_cursor` persist the last successfully-classified
  message into `state_dir/router.cursor`.
* `pending_lines` calls `chat-messages-list`, filters to messages newer
  than the cursor, and emits NDJSON lines in the same shape the live
  subscribe loop produces — so `subscribe.process_lines` replays them
  without caring whether the source was a Popen pipe or this catchup.

Cursor advances on every classified Decision (route or drop), so a
crash mid-apply means we re-encounter the message and lean on
process_lines' dedup set to skip duplicates.

Two response shapes seen in the wild from `lark-cli im +chat-messages-list`
Specifically:
  - older / fixture: `{body: {content: "..."}, create_time: "<epoch-ms>"}`
  - lark-cli 1.0.21 live: `{content: "...", create_time: "2026-05-03 18:53"}`
The shape-normalisation helpers below accept both.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Callable, Iterable

from claudeteam.feishu import chat as _chat
from claudeteam.feishu.router import Decision
from claudeteam.runtime import paths
from claudeteam.util import env_str, read_json, write_json


# ── cursor persistence ─────────────────────────────────────────


def read_cursor() -> dict:
    """Return the persisted cursor or {} (missing / corrupt / blank file)."""
    try:
        return read_json(paths.router_cursor_file(), {})
    except json.JSONDecodeError:
        return {}


def write_cursor(message_id: str, create_time: str) -> None:
    """Persist the last-seen message marker. No-op if either field is empty.

    Also stores `create_time_ms`, the epoch-ms reading of `create_time`
    taken NOW — i.e. while this process still shares env (TZ) with the
    lark-cli child that rendered the string. lark-cli renders
    "YYYY-MM-DD HH:MM" in process-local time (verified 2026-06-11: the
    same message lists as 03:55 under TZ=Asia/Shanghai and 19:55 under
    TZ=UTC), so the string alone is ambiguous: parsing it at *catchup*
    time under a different TZ (container UTC vs host +8, operator env
    change across a restart) shifts the cutoff by hours — silently
    dropping the whole gap or replaying hours of history. Epoch ms is
    TZ-free; readers prefer it and only fall back to parsing the legacy
    string for cursors written before this field existed."""
    if not message_id or not create_time:
        return
    write_json(paths.router_cursor_file(),
               {"message_id": message_id, "create_time": str(create_time),
                "create_time_ms": _to_epoch_ms(create_time)})


def record_decision(decision: Decision) -> None:
    """Advance cursor from a classified Decision (drop or route)."""
    write_cursor(decision.msg_id, decision.create_time)


# ── replay ────────────────────────────────────────────────────


def _extract_content(fei_msg: dict) -> str:
    """Pick content out of either lark-cli response shape:

    Live (lark-cli 1.0.21+): `{"content": "<text>"}`
    Older / fixtures: `{"body": {"content": "<text>"}}`

    Falls back to "" if neither is present."""
    body = fei_msg.get("body") or {}
    return body.get("content") or fei_msg.get("content") or ""


def _msg_to_event_line(fei_msg: dict) -> str:
    """Convert a chat-messages-list row into one NDJSON line matching
    `lark-cli event +subscribe --compact` shape.

    Carries sender.id_type into the event so subscribe._normalise can
    surface sender_type to classify_event — without it bot-self
    detection misses bot-sent cards on the catchup path and forwards
    manager's own ack cards back into manager's inbox every restart
    (host_smoke 2026-05-06: 7 loops in one session)."""
    sender = fei_msg.get("sender") or {}
    payload = {
        "event": {
            "message": {
                "message_id": fei_msg.get("message_id", ""),
                "chat_id": fei_msg.get("chat_id", ""),
                "message_type": fei_msg.get("msg_type", "text"),
                "content": _extract_content(fei_msg),
                "create_time": fei_msg.get("create_time", ""),
            },
            "sender": {
                "sender_id": {"open_id": sender.get("id", "")},
                "sender_type": sender.get("sender_type")
                                or sender.get("id_type", ""),
            },
        }
    }
    return json.dumps(payload, ensure_ascii=False)


def _to_epoch_ms(create_time: object) -> int:
    """Coerce a chat-messages-list create_time into epoch ms.

    Accepts:
      - int / numeric str: passed through (already epoch ms)
      - "YYYY-MM-DD HH:MM" or "YYYY-MM-DD HH:MM:SS" (lark-cli 1.0.21
        live shape): parsed as local time → epoch ms
    Returns 0 when uninterpretable so `_newer_than` treats the row
    as older than any non-zero cursor (i.e. "skip safely")."""
    if not create_time:
        return 0
    s = str(create_time).strip()
    if s.isdigit():
        return int(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(_dt.datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return 0


# Reorder margin stepped back from the cursor minute before filtering.
# The cursor is a MONOTONIC high-water mark (max create_time of any
# classified message), but lark's WebSocket can deliver out of order and,
# on macOS hosts, silently drops for ~120s at a time. So a message can be
# *missed* live while a LATER message gets classified and pushes the cursor
# past it; on the next catchup that earlier message is older than the
# high-water cursor and a bare minute-floor cutoff filters it out forever
# — it never reaches `seen` (so dedup can't help) and the minute floor is
# too coarse to save a cross-minute miss. Stepping the cutoff back by at
# least the disconnect window keeps those earlier-but-unprocessed messages
# in the replay set; the persisted `router.seen` dedup (seeded into
# process_lines across restarts) drops any we already applied, so a wider
# window costs a few dedup-drops, never a re-fire.
_DEFAULT_CATCHUP_LOOKBACK_MS = 120_000  # ~ the observed macOS WS silent-drop window


def _newer_than(messages: Iterable[dict], cursor_create_time: str, *,
                cursor_ms: int = 0,
                lookback_ms: int = _DEFAULT_CATCHUP_LOOKBACK_MS) -> list[dict]:
    """Filter `messages` to those at-or-after `cursor minute − lookback`.

    Two precision realms collide here. Cursor is set from the LIVE event
    `create_time` (lark-cli WebSocket → millisecond precision string).
    `messages` come from `chat-messages-list` REST (minute precision
    string like "2026-05-06 14:08", parses to the floor of that minute).
    A strict `>` or even bare `>=` comparison loses the minute the
    cursor is in: cursor 14:08:32.107 vs REST 14:08:00 → REST < cursor
    → every message that shares the cursor's minute is dropped.

    Floor the cutoff to the minute boundary (so same-minute REST messages
    survive) AND step it back by `lookback_ms` (so an out-of-order message
    a few minutes older than the high-water cursor — the cross-minute
    silent-drop miss — survives too). Re-applied messages are harmless:
    `router.seen` (persisted across restarts) and the in-process
    `seen_msg_ids` dedup them; the worst case is replaying ~a window of
    already-seen rows that immediately dedup-drop.

    The lookback is deliberately bounded (not the whole window) so a fresh
    deploy into an active chat doesn't reach back over pre-birth history
    the empty-cursor guard already excludes on first up.

    `cursor_ms` (when non-zero) is the TZ-free epoch reading persisted by
    write_cursor at event time; it wins over re-parsing the rendered
    string, which is only correct while this process's TZ matches the
    one the string was rendered under (see write_cursor). The REST rows
    themselves are safe to parse here: they were rendered seconds ago by
    a lark-cli child sharing this process's env.

    Bad/missing create_time (parses to 0) gets dropped — never include
    rows we can't timestamp, even when there's no cursor.
    """
    raw_cutoff = cursor_ms or _to_epoch_ms(cursor_create_time)
    minute_floor = (raw_cutoff // 60_000) * 60_000  # floor to minute
    cutoff = minute_floor - max(0, lookback_ms)     # step back the reorder margin
    def keep(m: dict) -> bool:
        ts = _to_epoch_ms(m.get("create_time"))
        return ts > 0 and ts >= cutoff
    fresh = [m for m in messages if keep(m)]
    fresh.sort(key=lambda m: _to_epoch_ms(m.get("create_time")))
    return fresh


def pending_lines(chat_id: str, *,
                  profile: str = "",
                  page_size: int = 50,
                  list_fn: Callable | None = None,
                  paginate: bool = False) -> list[str]:
    """Return NDJSON lines for messages newer than the saved cursor.

    Oldest-first so process_lines applies them in chronological order.
    `list_fn` is injectable for tests; in production it goes through
    `feishu.chat.list_recent`.
    """
    cursor = read_cursor()
    cursor_ct = str(cursor.get("create_time") or "")
    try:
        cursor_ms = int(cursor.get("create_time_ms") or 0)
    except (TypeError, ValueError):
        cursor_ms = 0   # corrupt field → fall back to string parsing
    # Fresh deploy (no cursor): don't replay arbitrary chat history.
    # Otherwise a fresh `claudeteam up` would re-fire every message in
    # the recent 50 — including dispatches from a previous team that
    # would now have manager re-doing tasks the boss already cleared.
    # The live subscribe stream picks up from "now" forward; the first
    # real event writes a cursor so subsequent restarts correctly catch
    # up just the gap between cursor and now.
    if not cursor_ct:
        return []
    if list_fn is None:
        # Honor send_as cascade so bot-only deployments don't trip
        # `need_user_authorization` from `chat-messages-list --as user`
        # (chat.list_recent's historical default). Mirrors `say`'s resolver:
        # legacy env CLAUDETEAM_LARK_SEND_AS first, then tunables
        # `feishu.send_as`, default "user" (preserve pre-tunables behaviour
        # where bare deployments without env had user-OAuth ready).
        legacy = env_str("CLAUDETEAM_LARK_SEND_AS").lower()
        if legacy:
            as_value = legacy
        else:
            from claudeteam.runtime import tunables
            as_value = str(tunables.tunable("feishu.send_as", "user")).lower()
        as_user = as_value != "bot"
        if paginate:
            cutoff = ((cursor_ms or _to_epoch_ms(cursor_ct)) // 60_000) * 60_000
            cutoff -= max(0, _catchup_lookback_ms())
            msgs: list[dict] = []
            token = ""
            visited: set[str] = set()
            for _ in range(200):
                page = _chat.list_recent_page(
                    chat_id, profile=profile, page_size=page_size,
                    page_token=token, as_user=as_user)
                batch = list(page.get("messages") or [])
                msgs.extend(batch)
                # History pages are newest-first. Once a full page crosses
                # the cutoff, no older page can hold a pending message.
                valid_times = [_to_epoch_ms(m.get("create_time")) for m in batch]
                if not page.get("has_more") or any(0 < ts < cutoff for ts in valid_times):
                    break
                next_token = str(page.get("page_token") or "")
                if not next_token or next_token in visited:
                    raise RuntimeError("Feishu history pagination stalled")
                visited.add(next_token)
                token = next_token
            else:
                raise RuntimeError("Feishu history exceeded 200 pages")
            fresh = _newer_than(msgs, cursor_ct, cursor_ms=cursor_ms,
                                lookback_ms=_catchup_lookback_ms())
            return [_msg_to_event_line(m) for m in fresh]
        def list_fn():
            return _chat.list_recent(chat_id, profile=profile,
                                     page_size=page_size, as_user=as_user)
    msgs = list_fn() or []
    fresh = _newer_than(msgs, cursor_ct, cursor_ms=cursor_ms,
                        lookback_ms=_catchup_lookback_ms())
    return [_msg_to_event_line(m) for m in fresh]


def _catchup_lookback_ms() -> int:
    """Reorder margin (ms) the catchup cutoff steps back from the cursor
    minute. Tunable so ops can widen it if a worse out-of-order skew is
    ever observed; defaults to the macOS WS silent-drop window."""
    try:
        from claudeteam.runtime import tunables
        return int(tunables.tunable("router.catchup_lookback_ms",
                                    _DEFAULT_CATCHUP_LOOKBACK_MS))
    except Exception:
        return _DEFAULT_CATCHUP_LOOKBACK_MS
