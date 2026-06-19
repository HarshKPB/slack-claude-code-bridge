"""Slack <-> Claude Code bridge.

@mention the bot in a private channel; its text is run as a Claude Code prompt
on this machine (full tool access). Only ALLOWED_USER_ID is honored.
Per-channel session continuity: follow-up mentions resume the same session.
Prefix a message with "cd <path>" (on its own first line) to switch the
working directory for that channel.
"""

import asyncio
import os
import re

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

SLACK_LIMIT = 2900  # safe chunk size under Slack's 3000-char block limit

app = App(token=BOT_TOKEN)

# Per-channel state: last session id (for resume) and current working dir.
sessions: dict[str, str] = {}
cwds: dict[str, str] = {}

MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
CD_RE = re.compile(r"^\s*cd\s+(\S+)\s*\n?", re.IGNORECASE)


def chunk(text: str):
    for i in range(0, len(text), SLACK_LIMIT):
        yield text[i : i + SLACK_LIMIT]


def react(client, channel, ts, name):
    """Best-effort reaction; ignore if scope/permission missing."""
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception as e:
        print(f"reaction '{name}' failed: {e}", flush=True)


async def run_claude(prompt: str, channel: str) -> tuple[str, float]:
    """Run prompt via Claude Code, return (final_text, cost_usd)."""
    opts = ClaudeAgentOptions(
        cwd=cwds.get(channel, DEFAULT_DIR),
        permission_mode="bypassPermissions",  # full power, no interactive prompts
        resume=sessions.get(channel),
        model=MODEL,
        # load real Claude Code config: global CLAUDE.md, skills, MCP servers, settings
        setting_sources=["user", "project", "local"],
    )
    final = ""
    cost = 0.0
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    final += block.text
        elif isinstance(msg, ResultMessage):
            sessions[channel] = msg.session_id  # remember for continuity
            cost = msg.total_cost_usd or 0.0
            if msg.result:
                final = msg.result
    return final.strip() or "(no text output)", cost


@app.event("app_mention")
def on_mention(event, say, client):
    user = event.get("user")
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]

    if user != ALLOWED_USER:
        react(client, channel, event["ts"], "no_entry")
        return

    text = MENTION_RE.sub("", event.get("text", "")).strip()

    # Optional "cd <path>" prefix switches working dir for this channel.
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

    # Reset session: start fresh.
    if text.lower() in ("reset", "new", "/reset"):
        sessions.pop(channel, None)
        say(text=":broom: Session reset. Next mention starts fresh.", thread_ts=thread_ts)
        return

    react(client, channel, event["ts"], "eyes")
    try:
        result, _ = asyncio.run(run_claude(text, channel))
    except Exception as e:  # surface failures into the thread
        say(text=f":x: Error: `{e}`", thread_ts=thread_ts)
        return

    for part in chunk(result):
        say(text=part, thread_ts=thread_ts)


if __name__ == "__main__":
    print(f"Bot up. cwd default={DEFAULT_DIR}. Listening (Socket Mode)…")
    SocketModeHandler(app, APP_TOKEN).start()
