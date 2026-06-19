# Slack ⇄ Claude Code bridge

@mention a Slack bot → it runs your text as a Claude Code prompt on this Mac
(full tool access) → replies in-thread. Push-based (Socket Mode, no public URL).

## How it works

```
You @mention in #claude-control
        │  (Slack websocket, Socket Mode)
        ▼
  bot.py (always-on, launchd)
        │  claude-agent-sdk → headless Claude Code
        ▼
  runs on your Mac (read/edit/run, any tool)
        │
        ▼
  reply posted back to the thread
```

- **User lock:** only `ALLOWED_USER_ID` is obeyed; anyone else gets a 🚫 reaction.
- **Session continuity:** each channel keeps one Claude session. Follow-up
  mentions resume it. Say `reset` to start fresh.
- **Switch dir:** start a message with `cd <path>` (first line) to change the
  working directory for that channel.
- **Auth:** uses your existing Claude Code login (the `claude` CLI). No API key.

## Setup

### 1. Slack app (one-time)
Create at api.slack.com/apps → From scratch:
- **Socket Mode** → enable → generate App-level token (`connections:write`) → `xapp-…`
- **OAuth & Permissions → Bot Token Scopes:** `app_mentions:read`, `chat:write`,
  `groups:history`, `reactions:write` (and `im:history` for DMs)
- **Event Subscriptions** → enable → bot event `app_mention`
- **Install to Workspace** → copy Bot token `xoxb-…`
- Your member ID: profile → ⋯ More → Copy member ID → `U…`
- Create private `#claude-control`, `/invite` the bot.

### 2. Config
```bash
cp .env.example .env
# fill SLACK_BOT_TOKEN, SLACK_APP_TOKEN, ALLOWED_USER_ID, DEFAULT_PROJECT_DIR
```

### 3. Run (foreground test)
```bash
source .venv/bin/activate
python bot.py          # prints "Bot up. Listening…"
```
In Slack: `@claude-code what's in the default dir?`

### 4. Always-on (launchd)
```bash
cp com.harsh.claude-slack-bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.harsh.claude-slack-bot.plist
launchctl list | grep claude-slack-bot      # running?
tail -f bot.log                             # logs
```
Stop / reload:
```bash
launchctl unload ~/Library/LaunchAgents/com.harsh.claude-slack-bot.plist
```

## Commands (in Slack, after @mention)
| You type | Effect |
|---|---|
| `status of the audit project` | passed straight to Claude |
| `cd ~/Developer/foo` | set channel's working dir (alone, or prefix a command) |
| `reset` / `new` | clear session, next mention starts fresh |

## Security
- **Full tool access** (`bypassPermissions`): whoever is `ALLOWED_USER_ID` can run
  any command on this Mac via Slack. Keep the channel private; never widen the lock.
- `.env` is git-ignored. Never commit tokens.
- Bot only runs while this Mac is awake and the launchd job is loaded.
