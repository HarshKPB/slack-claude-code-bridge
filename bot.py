"""Slack <-> Claude Code bridge.

@mention the bot in a private channel; its text runs as a Claude Code prompt
on this machine (full tool access). Only ALLOWED_USER_ID is honored.

Features:
- Per-channel session continuity (follow-ups resume the same session).
- `cd <path>` (first line) switches the channel's working directory.
- `reset` / `new` clears the session.
- `stop` / `cancel` aborts the in-flight run for that channel.
- Lifecycle reactions: 👀 received → ✅ done / ❌ error.
- Heartbeat: posts "still working…" every HEARTBEAT_SEC during long runs.
- One run per channel at a time (lock prevents session races).
- Long output is uploaded as a .md file instead of many text chunks.
"""

import asyncio
import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

load_dotenv()

BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
ALLOWED_USER = os.environ["ALLOWED_USER_ID"]
DEFAULT_DIR = os.environ.get("DEFAULT_PROJECT_DIR", os.path.expanduser("~"))
MODEL = os.environ.get("CLAUDE_MODEL") or None

SLACK_LIMIT = 2900       # safe chunk size under Slack's 3000-char block limit
FILE_THRESHOLD = 8000    # above this, upload a file instead of chunking
HEARTBEAT_SEC = 90       # interval for "still working…" pings

# --- logging (rotating, so bot.log can't grow unbounded) ----------------------
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
log = logging.getLogger("slackbot")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)
log.addHandler(logging.StreamHandler())  # also to stdout (launchd startup log)

# Keep replies proportional to the ask: don't blow a quick question into a
# heavy multi-agent pipeline. Replies land in Slack, so stay concise.
SCOPE_GUARD = (
    "You are answering inside a Slack thread. Match effort to the request. "
    "By default, answer directly and concisely from your own knowledge in a "
    "single response. Do NOT launch multi-agent workflows, audits, or "
    "long-running research pipelines unless the user explicitly says 'deep "
    "research', 'full audit', or similar. If a task genuinely needs a long "
    "run, say so first and ask for confirmation before starting. "
    "\n\nFORMAT FOR SLACK (mrkdwn, NOT GitHub Markdown):\n"
    "- Bold uses single asterisks: *bold*. Never use **double**.\n"
    "- Italic: _text_. Strike: ~text~. Inline code: `code`.\n"
    "- NO Markdown tables (pipes |---| render as raw text in Slack). "
    "Present tabular data as a numbered or bulleted list instead, e.g. "
    "`1. *Zomato* - 2M+ IG, meme-native`.\n"
    "- Bullets: use a leading '- ' or '• '. No '#' headings (use *bold* lines).\n"
    "- Keep it short, lead with the answer."
)

app = App(token=BOT_TOKEN)

# Per-channel state.
sessions: dict[str, str] = {}            # channel -> last Claude session id
cwds: dict[str, str] = {}                # channel -> working dir
channel_locks: dict[str, asyncio.Lock] = {}   # channel -> serialize runs
channel_tasks: dict[str, asyncio.Task] = {}   # channel -> in-flight run (for cancel)

MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
CD_RE = re.compile(r"^\s*cd\s+(\S+)\s*\n?", re.IGNORECASE)

# --- dedicated asyncio loop in a background thread ----------------------------
# Bolt listeners are sync; Claude work + heartbeat + cancel live on this loop.
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()


def chunk(text: str):
    for i in range(0, len(text), SLACK_LIMIT):
        yield text[i : i + SLACK_LIMIT]


def react(client, channel, ts, name):
    """Best-effort reaction; log (not crash) if scope/permission missing."""
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception as e:
        log.warning("reaction '%s' failed: %s", name, e)


def get_lock(channel: str) -> asyncio.Lock:
    lock = channel_locks.get(channel)
    if lock is None:
        lock = asyncio.Lock()
        channel_locks[channel] = lock
    return lock


def deliver(text, channel, thread_ts, say, client):
    """Post the result. Short -> text; medium -> chunks; long -> .md upload."""
    if len(text) <= SLACK_LIMIT:
        say(text=text, thread_ts=thread_ts)
        return
    if len(text) <= FILE_THRESHOLD:
        for part in chunk(text):
            say(text=part, thread_ts=thread_ts)
        return
    try:
        client.files_upload_v2(
            channel=channel,
            thread_ts=thread_ts,
            filename="response.md",
            title="Claude response",
            content=text,
            initial_comment=":page_facing_up: Full response attached.",
        )
    except Exception as e:
        log.warning("file upload failed (%s); falling back to chunks", e)
        for part in chunk(text):
            say(text=part, thread_ts=thread_ts)


async def run_claude(prompt: str, channel: str) -> str:
    """Run prompt via Claude Code; return final text. Records session id."""
    opts = ClaudeAgentOptions(
        cwd=cwds.get(channel, DEFAULT_DIR),
        permission_mode="bypassPermissions",
        resume=sessions.get(channel),
        model=MODEL,
        setting_sources=["user", "project", "local"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": SCOPE_GUARD},
    )
    final = ""
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    final += block.text
        elif isinstance(msg, ResultMessage):
            sessions[channel] = msg.session_id
            if msg.result:
                final = msg.result
    return final.strip() or "(no text output)"


async def heartbeat(thread_ts, say):
    start = time.monotonic()
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            mins = (time.monotonic() - start) / 60
            say(text=f":hourglass_flowing_sand: still working… ({mins:.0f}m)",
                thread_ts=thread_ts)
    except asyncio.CancelledError:
        pass


async def job(prompt, channel, msg_ts, thread_ts, say, client):
    """One run for a channel: lock, react, heartbeat, deliver."""
    lock = get_lock(channel)
    if lock.locked():
        say(text=":warning: Busy with another request here. Send `stop` to cancel it.",
            thread_ts=thread_ts)
        return
    async with lock:
        channel_tasks[channel] = asyncio.current_task()
        react(client, channel, msg_ts, "eyes")
        hb = asyncio.create_task(heartbeat(thread_ts, say))
        try:
            result = await run_claude(prompt, channel)
        except asyncio.CancelledError:
            say(text=":octagonal_sign: Cancelled.", thread_ts=thread_ts)
            react(client, channel, msg_ts, "x")
            raise
        except Exception as e:
            log.exception("run failed")
            say(text=f":x: Error: `{e}`", thread_ts=thread_ts)
            react(client, channel, msg_ts, "x")
            return
        finally:
            hb.cancel()
            channel_tasks.pop(channel, None)
        react(client, channel, msg_ts, "white_check_mark")
        deliver(result, channel, thread_ts, say, client)


@app.event("app_mention")
def on_mention(event, say, client):
    user = event.get("user")
    channel = event["channel"]
    msg_ts = event["ts"]
    thread_ts = event.get("thread_ts") or msg_ts

    if user != ALLOWED_USER:
        react(client, channel, msg_ts, "no_entry")
        return

    text = MENTION_RE.sub("", event.get("text", "")).strip()

    # Cancel the in-flight run for this channel.
    if text.lower() in ("stop", "cancel", "/stop"):
        task = channel_tasks.get(channel)
        if task and not task.done():
            _loop.call_soon_threadsafe(task.cancel)
            say(text=":octagonal_sign: Stopping…", thread_ts=thread_ts)
        else:
            say(text="Nothing running here.", thread_ts=thread_ts)
        return

    # "cd <path>" prefix switches working dir for this channel.
    m = CD_RE.match(text)
    if m:
        new_dir = os.path.expanduser(m.group(1))
        cwds[channel] = new_dir
        text = CD_RE.sub("", text, count=1).strip()
        if not text:
            say(text=f":file_folder: cwd → `{new_dir}`", thread_ts=thread_ts)
            return

    if not text:
        say(text="Empty prompt. Mention me with a command.", thread_ts=thread_ts)
        return

    if text.lower() in ("reset", "new", "/reset"):
        sessions.pop(channel, None)
        say(text=":broom: Session reset. Next mention starts fresh.", thread_ts=thread_ts)
        return

    # Hand off to the worker loop; return fast so Bolt acks the event.
    asyncio.run_coroutine_threadsafe(
        job(text, channel, msg_ts, thread_ts, say, client), _loop
    )


if __name__ == "__main__":
    log.info("Bot up. cwd default=%s. Listening (Socket Mode)…", DEFAULT_DIR)
    SocketModeHandler(app, APP_TOKEN).start()
