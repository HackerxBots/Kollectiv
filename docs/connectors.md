# Connectors

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

## Connect your services

Connectors are small, typed adapters that expose a service as **actions**. The
brain sees them as tools, `GET /connectors` lists them, the MCP server exposes
them to Claude/Cursor/etc., and the CLI can call them by hand:

```bash
kollektiv connectors                 # what exists, what is configured, which actions
kollektiv call github recent_commits --params '{"limit": 5}'
kollektiv call notion search --params '{"query": "roadmap"}'
kollektiv call webhook notify --params '{"text": "deploy finished"}' --confirm
```

```bash
curl -s localhost:8000/connectors | jq '.configured, .connectors[] | {name, detail}'
curl -s -X POST localhost:8000/connectors/github/call \
  -H 'content-type: application/json' \
  -d '{"action": "open_pull_requests"}'
```

| Connector | Env | Actions | Notes |
| --- | --- | --- | --- |
| **GitHub** | `GITHUB_TOKEN`, `GITHUB_REPO` | `recent_commits`, `commit_diff`, `open_pull_requests`, `pull_request_diff`, `file`, `repo_tree`, `open_issues`, `comment_on_pull_request`* | the repository the team works on |
| **Google Workspace** | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | `gmail_search`, `gmail_read`, `gmail_send`*, `calendar_events`, `calendar_create_event`*, `drive_search`, `drive_export` | one OAuth token covers Gmail + Calendar + Drive; the refreshed access token is cached in memory and in the encrypted store |
| **Notion** | `NOTION_TOKEN` | `search`, `get_page`, `query_database`, `create_page`*, `append_text`* | share each page/database with the integration |
| **Telegram** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `get_me`, `get_updates`, `send_message`*, `send_document`* | talk to [@BotFather](https://t.me/BotFather); the Bot API's `{ok, result}` envelope is unwrapped, so an HTTP 200 that failed is still an error |
| **Discord** | `DISCORD_BOT_TOKEN` **or** `DISCORD_WEBHOOK_URL` | `get_me`, `list_channels`, `send_message`*, `send_webhook`* | either route works alone; messages are chunked at Discord's 2000-character limit |
| **Slack** | `SLACK_BOT_TOKEN` **or** `SLACK_WEBHOOK_URL` | `auth_test`, `list_channels`, `post_message`*, `send_webhook`* | needs the `chat:write` scope for `post_message`; Slack answers HTTP 200 with `{"ok": false}`, so the envelope decides |
| **Linear** | `LINEAR_API_KEY`, `LINEAR_TEAM_ID` | `viewer`, `list_teams`, `list_issues`, `create_issue`*, `comment_issue`* | GraphQL; an out-of-range `priority` is refused rather than clamped |
| **WhatsApp** | `WA_BACKEND` + Cloud API **or** bridge credentials | `status`, `send_message`* | two backends, one of them unofficial — read [the section below](#whatsapp-two-backends-one-of-them-unofficial) first |
| **Webhooks** | `EVENT_WEBHOOKS` | `notify`, `list_targets` | run summaries are broadcast after every orchestrated run; point it at Slack, Discord, n8n, Activepieces, Zapier or your own service |
| **Any REST API** | `CUSTOM_CONNECTORS` | whatever you declare | one JSON entry per service, one action per endpoint — no code |

Nine connectors, 41 actions, and **every one of them is optional**: an
unconfigured connector is still listed (`kollektiv connectors` says what is
missing) and never blocks startup.

### WhatsApp: two backends, one of them unofficial

WhatsApp is the one connector where the *route* matters more than the code, so
Kollektiv ships both and makes you choose:

| | `WA_BACKEND=cloud` | `WA_BACKEND=bridge` |
| --- | --- | --- |
| What it is | Meta's official **WhatsApp Business Cloud API** | A **linked-device bridge** (the OpenClaw route: [Baileys](https://github.com/WhiskeySockets/Baileys) or [`whatsapp-web.js`](https://github.com/pedroslopez/whatsapp-web.js)) that pairs *your* number by QR, once |
| Needs | A Meta business account, a permanent token (`WA_CLOUD_TOKEN`), a phone number id (`WA_PHONE_NUMBER_ID`) | A tiny local Node gateway (`WA_BRIDGE_URL`) holding the session | 
| Message limits | 24-hour customer-service window; templates for anything outside it | Whatever the linked device can send |
| Honest downsides | Business verification, per-conversation pricing, template approval | **Unofficial.** It breaks when WhatsApp changes the protocol, the session can expire, and Meta can flag or ban the number |
| Kollektiv's stance | Supported, documented, default | Supported and **off by default**: `WA_ALLOW_UNOFFICIAL=true` is required, an explicit "my number, my risk" |

The bridge contract is deliberately tiny, so any of the usual Node gateways can
serve it (the HTTP shape is two endpoints; see `src/connectors/whatsapp.py` for
the exact payloads and the `TODO` about the wppconnect/wa-automate variants):

```
GET  {WA_BRIDGE_URL}{WA_BRIDGE_STATUS_PATH}   -> {"connected": true, "me": "2348…"}
POST {WA_BRIDGE_URL}{WA_BRIDGE_SEND_PATH}     <- {"to": "2348…", "text": "…"}
                                              -> {"sent": true, "id": "…"}
Authorization: Bearer {WA_BRIDGE_TOKEN}       (optional)
```

Until the flag is set, the connector reports `not configured` and
`detail()` explains the risk in one sentence — nobody enables it by accident.

`*` = **dangerous**: it changes something outside Kollektiv, so it requires an
explicit confirmation (`--confirm`, `"confirm": true` or `confirm=True` in MCP).

### Declaring your own connector (no code)

```jsonc
// CUSTOM_CONNECTORS in .env
[{
  "name": "slack", "category": "chat",
  "description": "Post to Slack",
  "base_url": "https://slack.com/api",
  "auth": "bearer", "token": "xoxb-…",
  "actions": [
    {"name": "post_message", "method": "POST", "path": "/chat.postMessage",
     "description": "Send a message", "dangerous": true,
     "params": {"channel": "Channel id", "text": "Message body"}},
    {"name": "channel_history", "method": "GET", "path": "/conversations.history",
     "params": {"channel": "Channel id", "limit": "Max messages"}}
  ]
}]
```

`{placeholders}` in `path` are filled from the parameters; the rest become the
query string (GET/DELETE) or the JSON body (POST/PUT/PATCH). `auth` is one of
`bearer`, `header` (`header_name`, default `Authorization`), `query`
(`query_name`, default `api_key`) or `none`.

### Calling connectors from an MCP client

Add Kollektiv to your MCP client (`claude_desktop_config.json`, Cursor, …) and
every connector action becomes a tool next to the project tools:

```json
{
  "mcpServers": {
    "kollektiv": {
      "command": "python",
      "args": ["-m", "src.api.mcp_server"],
      "env": { "KOLLEKTIV_ENV_FILE": "/etc/kollektiv.env" }
    }
  }
}
```

Tools: `list_projects`, `get_project_status`, `create_project`, `run_project`,
`replan_project`, `list_files`, `upload_file`, `get_agent_pool_status`,
`get_storage_status`, `trigger_sync`, **`list_connectors`**, **`call_connector`**,
plus OpenAI built-in web search when run with `--with-search`.

### Making connectors reliable (design for "no support tickets")

The catalog above is deliberately shaped so a wrong call fails *early, cheaply
and legibly*:

| Guarantee | How it works | What a user sees |
| --- | --- | --- |
| **Known actions only** | every connector publishes its action list; unknown names are rejected | `Connector 'webhook' has no action 'notifyy' (available: notify, list_targets)` |
| **Known parameters only** | parameters are validated against the action's declared params before any request | `webhook.notify does not accept tex; accepted parameters: text, url, event` |
| **Safe by default** | read actions never mutate; anything that sends/creates/comments is `dangerous` and needs `confirm` | the call refuses to run with an explicit message |
| **Reachability, on demand** | `kollektiv connectors --probe`, `POST /connectors/{name}/probe` run the cheapest read action and time it | `[ok] github 0.42s reachable` / `[FAIL] notion not configured (NOTION_TOKEN)` |
| **Secrets never leak** | tokens are encrypted at rest, masked in logs, `/health` and `redacted()` | `***redacted***` in every dump |
| **Failures are typed** | transport/5xx/429 retry with backoff and honour rate limits; 4xx fail fast with the service's own message | a 401 says "rejected the token", a 403 says which scope is missing |
| **One service cannot break a run** | every action is isolated; failures are logged, reported and skipped | the run finishes, `/health` shows the degradation |
| **Response caps** | bodies are truncated to a sane size before they reach an agent's context | no accidental 20 MB diff in a prompt |

The probe is the first thing to run when a connector looks broken — it separates
"the credential is wrong" from "the service is down" from "the action name is a
typo" without reading a log.

### Credentials and safety

- Connector secrets are masked in logs, in `/health` and in `redacted()`.
- Tokens can live in the encrypted token store (`service="google"`,
  `account="default"`), which takes precedence over `.env` — refreshed OAuth
  tokens are written back automatically.
- Every connector is optional and every failure is isolated: an unreachable
  service returns an error for that action, logs it, and leaves the run alone.
- Read actions are safe by default; anything that sends, creates or comments is
  flagged `dangerous` and gated behind `confirm`.
- Through the [MCP gateway](gateway.md) a connector can be *narrowed* without
  touching `.env`: give that client a token whose policy denies
  `connectors.github.*`, or `read_only: true`, and the tools disappear from its
  catalogue instead of failing at call time.

---
