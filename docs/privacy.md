# Privacy

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

## Privacy: no telemetry, no accounts, no data collection

Kollektiv has no analytics, no crash reporting, no phoned-home pings, no
"anonymous usage statistics" and no accounts of its own. There is nothing to opt
out of, because nothing is collected.

- **The only network traffic is what you configure.** Every outbound request
  goes to an endpoint you put in `.env`: your LLM provider, your GitHub
  repository, your R2/TeraBox drive, your connectors. Nothing else leaves the
  process — grep the source for `httpx` and the list of hosts is exactly that.
- **Your code and prompts stay yours.** Plans, artifacts and the state document
  are written to your database and your storage. The maintainers never see them.
- **No identifiers.** No install IDs, no device fingerprinting, no email
  collection. `GET /health`, `kollektiv check` and the dashboard read the local
  configuration only.
- **Self-hosted by default** (see [Self-hosting checklist](deployment.md#self-hosting-checklist)):
  the free hosted tiers in this README (Cloudflare, Neon, Clerk, Resend) are
  conveniences you can replace with something you run, one at a time.
- **Auditable in one command.** `rg "httpx|requests" src/` lists every place a
  request can be made; the connector catalogue (`GET /connectors`) shows every
  service currently configured.

## Bring your own key does not change any of this

Workers call the model providers you configure, with your own keys, and nothing
else:

- **Keys stay on your machine.** `kollektiv login` encrypts them with
  `SECRET_KEY` in your database. Nothing is sent to Kollektiv, because there is
  no Kollektiv server.
- **Your code and prompts go only to the provider you chose**, and to GitHub if
  you configured it. A local model (Ollama, LM Studio) keeps them on your machine.
- **No telemetry, no analytics, no usage reporting** and no advertising. The
  product has no account system, so there is nothing to sign in to and no
  server that can see your activity.
- Connectors, storage and the MCP gateway are optional. Each one is inert until
  you add its credentials.
