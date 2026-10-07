# Frontend prompt

The prompt used to build (and iterate on) the dashboard in `web/`. It is written
for an AI website builder — v0, Bolt, Lovable, Claude Artifacts, ChatGPT canvas —
or for a human designer. Keep it in sync with the API: **if an endpoint changes,
change both this file and `web/assets/app.js`.**

Two hard requirements, learned the hard way:

1. **Not one file.** Ship `index.html` plus an `assets/` folder (CSS + JS +
   favicon). A single 2 000-line HTML file is not maintainable and cannot be
   cached or diffed properly.
2. **Hover states are part of the design, not a garnish.** Every interactive
   element needs a defined default, hover, active, focus-visible and disabled
   state, with real transitions (and `prefers-reduced-motion` respected).

---

## Prompt

> **Build "Kollektiv mission control" — a multi-file static dashboard for a
> self-hosted, open-source AI dev-team orchestrator.**
>
> ### Deliverables
>
> ```
> index.html            markup, semantic, hash routes
> assets/styles.css     design system: tokens, layout, components, all states
> assets/app.js         API client, renderers, command palette, SSE
> assets/favicon.svg    small logo
> ```
>
> No build step, no framework, no CDN, no external fonts or icon packs, no
> telemetry of any kind. Plain ES modules. Must run from `python -m http.server`
> and from Cloudflare Pages unchanged.
>
> ### Data (a Kollektiv API; base URL from `?api=`, else `localStorage`,
> else same origin; optional `Authorization: Bearer <localStorage token>`)
>
> | Endpoint | Purpose |
> | --- | --- |
> | `GET /health` | `{status, environment, subsystems:{storage:{backend,accounts,healthy}, agents:{agents,busy}, brain:{configured,provider,calls,failures}, github:{configured,repo}, connectors:{count,configured[],actions}}, warnings[]}` |
> | `GET /projects` | `{count, projects:[{project_id,name,status,created_at}]}` |
> | `POST /projects` | `{name, description, n_agents}` → project + `plan.tasks[]` |
> | `POST /projects/{id}/run` | `{status, tasks_dispatched, completed, errors[]}` |
> | `POST /projects/{id}/replan` | `{revision, new_tasks[]}` |
> | `GET /projects/{id}/status` | `{project_name,status,tasks:[{id,title,status,assigned_agent,score,error}],files[],history[],last_commit,queued_tasks,pool}` |
> | `GET /projects/{id}/handoff` | resume briefing: `progress`, `next_actions[]`, `blockers[]`, `markdown` |
> | `GET /projects/{id}/events/stream` | **SSE**: `event: state` frames with `{status,tasks,files,history,last_commit,queued_tasks}` + keep-alive comments |
> | `GET /projects/{id}/files/{path}/url` | `{url}` (presigned download) |
> | `GET /connectors` | `{count, configured[], connectors:[{name,category,description,configured,detail,actions[],dangerous_actions[]}]}` |
> | `POST /connectors/{name}/call` | `{action, params, confirm}` |
> | `POST /connectors/{name}/probe` | `{ok, seconds, action, detail|error}` |
> | `GET /agents/status` | `{agents:[{account_id,label,busy,status,tasks_done,tasks_failed,last_error}]}` |
> | `GET /storage/status` | `{total_gb, used_gb, free_gb, accounts, healthy, per_account:[{label,healthy,used_gb,free_gb,bucket}]}` |
> | `POST /sync` | trigger the GitHub sync, returns a summary |
>
> ### Layout
>
> - **Sidebar** (collapses to a top bar under 1000px): logo, nav for Overview,
>   Projects, Connectors, Agents & storage; footer with two live status dots and
>   a theme toggle. Active route marked with `aria-current="page"` and an accent
>   bar that grows on hover. Show `g p` / `g c` / `g a` shortcut hints.
> - **Topbar**: page title with a muted "connected host" chip, API URL field +
>   Connect, and a search button that opens the command palette.
> - **Overview**: warning banner (each warning names the env var that fixes it),
>   then six stat cards (API, storage, agents, brain, GitHub, connectors) —
>   the whole card is clickable where it makes sense.
> - **Projects**: a create form (name, agent count, plan-only vs plan-and-run,
>   description) and a table: project + id, status pill, tasks done/total,
>   animated progress bar, row actions (Open, Run, Handoff).
> - **Project drawer** (right side, slide-in, scrim, `esc` closes): description,
>   progress bar, four stat cards, task table with status pills and scores,
>   artifact chips that copy a download URL, and an event-history log — all
>   updated live from the SSE stream, with a dot showing stream health.
> - **Connectors**: table of services; readiness pill with the missing setting in
>   a tooltip; action `<select>`; **Call** (prompt for JSON params, confirm
>   dangerous ones in a modal) and **Probe** (shows latency or the error).
> - **Agents & storage**: two tables — pool (busy/idle, done/failed, last error)
>   and drive accounts (healthy, used, free, bucket).
> - Footer: MIT, source link, and the privacy line: *no telemetry, no analytics,
>   no tracking — this page talks only to your API.*
>
> ### Interaction requirements (do not skip)
>
> - **Hover states everywhere**, with transitions in the 140–320 ms range and a
>   shared easing curve: cards lift 3px with a soft shadow and a radial spotlight
>   that follows the pointer; buttons rise 2px, glow with an accent ring, and
>   sweep a diagonal sheen; icon-only buttons scale their glyph; table rows tint
>   and grow a 3px accent edge on the first cell; pills lift slightly and reveal
>   a tooltip (pure CSS, `data-tip`); the logo mark tilts; nav items slide 2px
>   and grow their accent bar; inputs brighten their border on hover and show a
>   3px focus ring on `:focus-visible`.
> - **Active/pressed** states: 1px dip or 0.985 scale — never a jump.
> - **Command palette** on ⌘K / Ctrl-K: fuzzy filter over commands *and*
>   projects (open, run, copy handoff), ↑/↓ to move, ↵ to run, `esc` to close,
>   focus trapped, `role="dialog"`.
> - **Toasts** bottom-right: success/warning/error tones, auto-dismiss (~5 s),
>   click to dismiss, `aria-live="polite"`, slide-in animation.
> - **Skeletons** (shimmer) while loading, empty states that say what to do next,
>   and inline errors that never blank the page.
> - **Theme**: dark first, light via `prefers-color-scheme`, plus a persisted
>   manual toggle; all colours from CSS custom properties.
> - **Accessibility**: semantic landmarks, labelled inputs, keyboard reachable
>   everywhere, visible focus, `prefers-reduced-motion` disables movement,
>   contrast ≥ 4.5:1 for text.
> - **Performance**: no render-blocking resources, no images except the inline
>   SVG logo, total < 120 KB uncompressed, first paint < 1 s on a laptop.
>
> ### Tone of the copy
>
> Precise and calm, never chirpy. Say what happened and what to do next
> ("No projects yet — describe what to build above and press Create"), name the
> env var when something is unconfigured, and never claim success for an action
> that returned an error.

---

## Iterating

When the API changes, update three places together:

1. the endpoint table above;
2. `web/assets/app.js` (the `api()` calls and the relevant renderer);
3. `web/README.md`'s feature list, and the test
   `test_dashboard_calls_the_documented_endpoints`.

When you redesign the look, keep the file split: `styles.css` for anything
visual, `app.js` for behaviour, `index.html` for structure.
