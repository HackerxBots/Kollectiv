# Kollektiv mission control (static dashboard)

A dependency-free dashboard for a running Kollektiv API: health, projects with
live progress, connectors with probes, agent pool and shared-drive quota.

```
web/
├── index.html            markup and hash routes (#/overview, #/projects, #/connectors, #/agents)
├── assets/
│   ├── styles.css        design tokens, components, hover/focus/motion states
│   ├── app.js            API client, renderers, command palette (⌘K), SSE live view
│   └── favicon.svg       logo
├── _headers              Cloudflare Pages CSP + cache headers
├── _redirects            Cloudflare Pages redirects (optional API proxy)
└── README.md             this file
```

`_headers` sets a strict CSP (`script-src 'self'`, `connect-src *` for your own
API origin), `nosniff`, `no-referrer` and short asset caching; `_redirects`
explains why hash routing needs no SPA fallback and how to proxy the API through
Pages with `?api=/api` to avoid CORS entirely.

No build step, no framework, no CDN, no telemetry: plain ES modules and CSS.
The API serves this folder itself at `/ui` (and `/` redirects there), so a
single-origin deployment needs no CORS configuration.

## Features

- **Overview**: subsystem health, configuration warnings with the variable that
  fixes each one, six stat cards.
- **Projects**: table with status pills, progress bars and per-row actions
  (Open, Run, Handoff) plus a create form (plan-only or plan-and-run).
- **Project drawer**: live task table, artifacts with copy-URL, event history,
  streamed over Server-Sent Events (`/projects/{id}/events/stream`) — the dot in
  the header shows the stream state.
- **Connectors**: catalogue with readiness, per-service probe, and an action
  runner that asks for JSON parameters and confirms dangerous actions.
- **Agents & storage**: pool status, per-account drive usage and health.
- **Command palette** (⌘K / Ctrl-K): navigate, run projects, copy handoffs,
  probe connectors, toggle theme. Keyboard shortcuts: `g p`, `g c`, `g a`,
  `r` refresh, `t` theme, `esc` close.
- **Accessibility**: real buttons and labels, `aria-current` navigation,
  focus-visible rings, `prefers-reduced-motion` support, light and dark themes
  (system default, toggle persisted).
- **Hover craft**: cards lift with a spotlight, buttons sweep a sheen, table rows
  highlight with an accent edge, pills reveal tooltips, the brand mark tilts.

## Deploy on Cloudflare Pages (free)

1. **Pages → Create application → Connect to Git** and pick this repository.
2. Framework preset **None**, build command empty, **output directory `web`**.
3. Deploy — you get `https://<project>.pages.dev`.

Point the page at your API: type the URL in the header field (remembered in
`localStorage`), or pass `?api=https://kollektiv.example.com` once. Same-origin
setups (API behind the same host) are detected automatically.

| Setup | What to do |
| --- | --- |
| API on a public URL | set `CORS_ORIGINS=https://<project>.pages.dev` in the API's `.env` |
| Same origin | add a Pages `_redirects`: `/api/* https://your-api-host/:splat 200`, then open with `?api=/api` |
| Behind Clerk auth | sign in through your Clerk frontend, then `localStorage.setItem("kollektiv.token", token)` — sent as `Authorization: Bearer …` |

Without `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` the same folder is
published by `.github/workflows/pages.yml` to GitHub Pages instead.

## Local preview

```bash
kollektiv serve-api                          # http://localhost:8000/ui
python -m http.server 8088 --directory web   # or standalone while editing
```

## Regenerating the UI

`docs/ui-prompt.md` contains the prompt used to produce this dashboard (and to
iterate on it with an AI website builder). Keep it in sync: if the API surface
changes there, update the prompt and `assets/app.js` together.
