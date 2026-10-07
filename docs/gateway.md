# The MCP gateway

`kollektiv-mcp` serves one client. The gateway serves a team: one MCP URL that
Claude Code, Codex, Cursor, Zed, a dashboard and a cron job can all point at,
each with its own token and its own rules, and a local audit log that records
what happened without recording what was said.

It is **optional**. `GATEWAY_ENABLED=false` by default, and the plain MCP server
and the HTTP API stay first-class forever.

[← Back to the README](../README.md) · [Kollektiv documentation](README.md)

## Why it exists

| Problem | What the gateway does |
| --- | --- |
| Your editor, your teammate's editor and your scripts all need Kollektiv's tools | One endpoint, many clients |
| A shared token means everyone can do everything | Per-client tokens, stored encrypted (`TokenStore`), shown once, revocable |
| An agent should read but never run | Named policies (`read-only`, `worker`, `messenger`, `dashboard`, `admin`) plus per-tool globs |
| "Who called `projects.run` at 02:14?" | A local audit log: tool, client, duration, result — never arguments |
| Vendor lock-in | Your tools, served to whatever client you already pay for. Nothing is proxied or resold |

## Quickstart

```bash
kollektiv gateway init --name laptop --role dashboard   # prints a kgw_… token ONCE
kollektiv gateway serve                                 # http://0.0.0.0:8010, MCP at /mcp
```

Then point a client at it. Claude Code takes one command:

```bash
claude mcp add kollektiv --transport http http://localhost:8010/mcp \
  --header "Authorization: Bearer kgw_…"
```

Any other MCP client wants the same two things — the URL and the header:

```json
{
  "mcpServers": {
    "kollektiv": {
      "type": "http",
      "url": "http://localhost:8010/mcp",
      "headers": { "Authorization": "Bearer kgw_…" }
    }
  }
}
```

`kollektiv gateway init` prints that JSON snippet with your token filled in, plus
the equivalent Claude Code command.

## The four moving parts

### Tokens

```
kollektiv gateway init --name laptop --role dashboard   # issue (rotate with --rotate)
kollektiv gateway clients                               # who exists, how much they used it
kollektiv gateway revoke --name laptop                  # remove the client and its token
```

Tokens are `kgw_` + 32 bytes of `secrets.token_urlsafe`, encrypted at rest with
the same Fernet `TokenStore` every other credential uses, and compared in
constant time. Rotation takes effect immediately in the running process.

### Policies

A policy is four lists of globs matched against namespaced tool names, plus one
flag:

```json
{
  "allow":     ["*"],
  "deny":      ["connectors.github.*"],
  "confirm":   ["*.run", "*.create*", "connectors.*.send*"],
  "read_only": false
}
```

* `allow` — what the client may call. Empty means **nothing**; a policy that
  fails open is not a policy.
* `deny` — evaluated after `allow`, so a narrow deny always beats a broad allow.
* `confirm` — those tools additionally need `"confirm": true` in the call. The
  second yes is deliberate: it is the difference between "the model may do this"
  and "the model may do this without asking".
* `read_only` — refuse every tool the catalogue marks dangerous.

Five presets ship, and the preset name is also a role, so there is only one
vocabulary to learn:

| Preset | Allows | Notes |
| --- | --- | --- |
| `read-only` | `*` | every dangerous tool is refused |
| `dashboard` | `*` | writes need `confirm=true` |
| `worker` | `projects.*`, `storage.*`, `agents.*` | no connectors, no gateway admin |
| `messenger` | `connectors.*`, `projects.list`, `projects.status` | cannot touch GitHub; every connector call needs `confirm=true` |
| `admin` | `*` | no confirm rules; for the operator's own token |

```bash
kollektiv gateway presets                                  # the table above
kollektiv gateway policy --name laptop --preset read-only  # apply one
kollektiv gateway policy --name laptop --deny "*.upload" --read-only
kollektiv gateway policy --name laptop                     # inspect
```

Policies live on the client row, and a JSON file (`GATEWAY_POLICY_PATH`) can
override them — keep it in git and you can review permission changes like code:

```json
{ "laptop": { "allow": ["projects.*"], "read_only": true }, "*": { "deny": ["gateway.*"] } }
```

### The catalogue

Every tool is namespaced, so two services can never collide:

```
projects.list    projects.create   projects.run      projects.status
projects.replan  projects.handoff  projects.files    projects.upload_file
storage.status   agents.status     sync.run
connectors.list  connectors.probe  connectors.telegram.send_message
connectors.discord.send_message    connectors.slack.post_message
connectors.linear.create_issue     connectors.whatsapp.send_message   … 43 connectors in all
gateway.health   gateway.tools     gateway.audit
```

```bash
kollektiv gateway tools            # the human map (⚠ marks writes)
curl -H "Authorization: Bearer kgw_…" http://localhost:8010/toolkits
```

`GET /toolkits` returns only what *that* client may use, split into `toolkits`
(ready), `confirm_required` (one extra flag) and `blocked_tools` (with the rule
that decided). That makes it a real discovery surface for a model, not a doc.

### The audit log

One row per call: client, tool, namespace, ok, denied, milliseconds, the
**names** of the arguments that were passed, and a short failure reason. Never
the values — the values are the project's data.

```bash
kollektiv gateway audit                 # recent calls + totals
kollektiv gateway audit --clear         # delete the log
```

It is local, never uploaded, never aggregated, and readable only by the client
itself (plus `admin`/`dashboard` roles, which may read everyone's).

## The HTTP surface

| Route | Auth | What it is |
| --- | --- | --- |
| `GET /health` | none | liveness, client count, whether tokens are required |
| `GET /toolkits` | token | the catalogue this client may use |
| `POST /call` | token | `{"tool": "projects.list", "params": {}, "confirm": false}` |
| `GET /audit` | token | recent calls (`?client=` widens it for admin/dashboard roles) |
| `POST /mcp` | token | the MCP endpoint itself |
| `GET /docs` | none | OpenAPI, for the REST half |

Errors are the same ones the REST API returns, so a client behaves identically
against either surface: 400 for bad parameters or an unconfigured connector, 403
for a policy refusal (with the rule that decided), 404 for an unknown tool or a
missing project, 502 when an upstream service failed.

`GATEWAY_REQUIRE_TOKENS=false` makes every call anonymous as the `admin` role.
That exists for local development and prints a warning at startup; it is not a
deployment mode.

## Running it for real

* **Behind a tunnel, not a port.** `cloudflared tunnel --url http://localhost:8010`
  or `tailscale serve` gives you HTTPS without opening anything. The token is
  the gate, so HTTPS matters — it is what keeps the token off the wire.
* **Set `GATEWAY_ALLOWED_HOSTS`** (e.g. `gw.example.com`) when a browser you do
  not control can reach the port. Empty (the default) disables the MCP SDK's
  DNS-rebinding check, which is the right call for a bearer-token endpoint.
* **Per person, per device.** One token per teammate, per laptop, per agent.
  Revoking one should never mean rotating everyone's.
* **Watch the log.** `kollektiv gateway audit` is the first place to look when
  something behaves differently than you expected.

## Design notes

* **No vendor seat games.** The gateway serves *our* tools to *your* client. It
  does not proxy, resell or evade anyone's subscription, and your client's
  credentials never touch Kollektiv.
* **The token is the boundary.** One header, verified once per request, then
  policy. There is no session state to steal and no cookie to confuse a browser.
* **Fail closed.** Unknown tool → 404. Empty policy → nothing allowed. A
  malformed stored policy is labelled `(unreadable)` in `gateway clients`
  instead of being quietly ignored.
* **The plain server stays.** `python -m src.api.mcp_server` is the single-user
  path, unchanged. The gateway is an addition, not a dependency.

## Related

* [api.md](api.md) — the REST API, the MCP server and the CLI
* [connectors.md](connectors.md) — the services the gateway exposes
* [monetization.md](monetization.md) — why the gateway exists in the business
  sense (it is the thing that can be paid for without taking anything away)
* [configuration.md](configuration.md) — every `GATEWAY_*` variable
