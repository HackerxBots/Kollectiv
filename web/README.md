# Kollektiv mission control (Cloudflare Pages)

A single static page — no build step, no framework, no server — that talks
straight to a running Kollektiv API. It shows subsystem health, projects and
their tasks, the worker pool and the shared-storage quota, and it can create,
run, inspect and replan projects.

```
web/
├── index.html      the whole app (HTML + CSS + vanilla JS)
└── README.md       this file
```

## Deploy on Cloudflare Pages (free)

1. **Pages → Create application → Connect to Git** and pick this repository.
2. Build settings:
   - Framework preset: **None**
   - Build command: *(leave empty)*
   - Build output directory: `web`
3. Deploy. You get `https://<project>.pages.dev`.

Then point the page at your API: either type the URL in the header field (it is
remembered in `localStorage`), or pass `?api=https://kollektiv.example.com` once.
If the API is served from the same origin (for example behind a Pages Function
or a reverse proxy on `/api`), the page auto-detects it.

### Making the API reachable

The page is static, so the browser calls the API directly. Two supported setups:

| Setup | What to do |
| --- | --- |
| API on a public URL | Set `CORS_ORIGINS=https://<project>.pages.dev` in the API's `.env`, then reload the page with `?api=https://your-api-host`. |
| Same origin | Add a Pages `_redirects` file with `/api/* https://your-api-host/:splat 200` (Cloudflare proxies it, so no CORS at all) and open the page with `?api=/api`. |

When `AUTH_REQUIRED=true` (Clerk), the page needs a session token: sign in
through your Clerk-powered frontend, then call
`localStorage.setItem("kollektiv.token", token)`. The script sends it as
`Authorization: Bearer …` on every request. Without a token a protected API
answers `401` and the page says so.

## Local preview

The API serves this folder itself — no second server, no CORS:

```bash
kollektiv serve-api
# dashboard: http://localhost:8000/ui   (the API's "/" redirects there too)
```

Or serve the folder standalone (useful while editing the page):

```bash
python -m http.server 8088 --directory web   # http://localhost:8088
```

## GitHub Pages instead?

The same folder works on GitHub Pages (workflow in
`.github/workflows/pages.yml` when `CLOUDFLARE_API_TOKEN` is not configured):
enable Pages with "GitHub Actions" as the source and the workflow publishes
`web/`.
