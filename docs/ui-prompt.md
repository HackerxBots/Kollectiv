# Frontend prompt

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

The prompt used to build (and iterate on) the dashboard in `web/`. It is written
for an AI website builder — v0, Bolt, Lovable, Claude Artifacts, ChatGPT canvas —
or for a human designer. Keep it in sync with the API: **if an endpoint changes,
change both this file and `web/assets/app.js`.**

Four hard requirements, learned the hard way:

1. **Not one file.** Ship `index.html` plus an `assets/` folder (CSS + JS +
   favicon + web manifest). A single 2 000-line HTML file is not maintainable and
   cannot be cached or diffed properly.
2. **Hover states are part of the design, not a garnish.** Every interactive
   element needs a defined default, hover, active, focus-visible and disabled
   state, with real transitions (and `prefers-reduced-motion` respected).
3. **Not plain.** Neutral grey dashboards are the thing this page must not be.
   Colour, depth, glass and glow are the design — see the visual language below.
4. **Everything is still zero-dependency.** No CDN, no npm, no build step, no
   remote font, no image file. The aurora is gradients, the glass is
   `backdrop-filter`, the icons are characters, the logo is one local SVG.

---

## The look, in one paragraph

**Aurora glass, candy accents.** A dark, near-black canvas lit by four blurred
colour fields that drift slowly behind everything (grape, cyan, pink, lime — a
touch of peach and sun in the mix). On top of it, frosted panels — real
`backdrop-filter` blur with a 1px inner highlight, so the aurora smears through
the surfaces instead of sitting behind them. Everything is generously rounded
(14 / 22 / 32 px and full pills), generously spaced, and colour-coded: each
subsystem owns a hue and keeps it everywhere it appears. Cards tilt a few
degrees toward the pointer in 3D with a spotlight that follows it. Buttons lift,
glow and sweep a diagonal sheen. The terminal-flavoured bits (model names, ids,
commands) are monospace and tinted like a code block in a good editor. Take the
boldness of a Shopify landing page, the candy terminals of Freebuff, the soft
parallax light of vibefree.dev, and the saturated, chip-heavy visual language of
Pinterest — then make it a *tool*, so nothing gets in the way of reading a table.

**Anti-goals:** flat grey cards, 1px "subtle" everything, all-neutral palettes,
tiny 8px radii, hover states that only change a border colour, drop shadows that
look like 2014 Bootstrap, and any animation over ~350 ms.

---

## Prompt

> **Build "Kollektiv mission control" — a multi-file static dashboard for a
> self-hosted, open-source AI dev-team orchestrator. It must feel like a premium
> 2026 product (colourful, luminous, glassy, tactile) while staying a serious
> instrument: dense tables, precise numbers, no decoration that costs a reading.**
>
> ### Deliverables
>
> ```
> index.html                     markup, semantic, hash routes, onboarding, tab bar
> assets/styles.css              design system: tokens, aurora, glass, components, states
> assets/app.js                  API client, renderers, 3D tilt, onboarding, palette, SSE
> assets/favicon.svg             the brand mark (keep it: the user loves this mark)
> assets/manifest.webmanifest    installable, offline-friendly shell
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
> ### Visual language (this is the part that makes it not-plain)
>
> **1. Aurora backdrop.** Two fixed pseudo-element layers, each a stack of
> `radial-gradient`s (grape → cyan → pink → lime, plus sky/sun in the second
> layer), blurred 38–60 px, `saturate(120–140%)`, drifting on a 46 s and 64 s
> `alternate` transform loop (a few vw/vh, plus a 1.02–1.06 scale). Behind
> everything: `z-index: -2/-1`. On top, a static CSS grain layer (`repeating-linear-gradient`
> at ~4% opacity, `mix-blend-mode: overlay`) so the gradients never band.
> Light mode swaps the aurora for a milkier version of the same hues.
>
> **2. Glass panels.** Every surface (sidebar, topbar, cards, tables, drawer,
> palette, toasts, dialogs, tab bar) is:
> `background: linear-gradient(150deg, panel 100%, panel-2 70%)` with panels
> defined as `rgba(..., 0.5–0.6)`, `backdrop-filter: blur(16–30px) saturate(140–165%)`,
> `border: 1px solid rgba(255,255,255,0.10)`, plus a `rgba(255,255,255,0.16)`
> 1px **inner** highlight so the edge reads as glass. Never a flat opaque slab.
>
> **3. Shape.** Radii: `--kv-radius-xl: 32px` (topbar, drawer, dialogs, onboarding
> card), `--kv-radius: 22px` (cards, tables), `--kv-radius-sm: 14px` (inputs,
> rows), `999px` for buttons, pills, nav items, tab bar, search field. Nothing
> under 10px. Padding is generous (18–30px in cards, 13–16px in table cells).
>
> **4. Colour is information.** Each subsystem owns a hue and keeps it in its
> stat card tint, its spark icon, its hover glow and its chart/progress accents:
> API `cyan`, storage `lime`, agents `grape`, brain `pink`, GitHub `peach`,
> connectors `sky`. Semantic colours are separate and never reused for
> decoration: ok `#34d399`, warn `#fbbf24`, bad `#fb7185`. Text is
> `#f2f4ff` / `#b9c0e4` / `#8b93bd` — three levels, contrast ≥ 4.5:1.
>
> **5. 3D and light.** Cards carry a `perspective(1000px)` transform driven by
> `--kv-rx/--kv-ry` (max ±7°), computed from the pointer position, plus
> `--kv-mx/--kv-my` for a radial spotlight; both reset on `pointerout`. Spark
> icons sit at `translateZ(28px)` and rotate on hover. Disabled entirely for
> coarse pointers and `prefers-reduced-motion`. Text stays on the plane: tilt
> never skews reading.
>
> **6. Type.** System sans for everything, `tabular-nums` on numbers. The page
> title and onboarding headlines use a gradient text fill
> (`linear-gradient(96deg, text → cyan → pink)`, `background-clip: text`).
> Section headings are 13px uppercase, 0.16em tracking, with a 20×6px gradient
> bar in front of them. Terminal content (project ids, commands, env vars,
> JSON) is monospace at 0.92em.
>
> ### Layout
>
> - **Sidebar** (268px, sticky, frosted rail): logo + wordmark, four nav pills
>   with a gradient active bar, footer with two live status dots (API,
>   connectors), a "Show me around" button that reopens onboarding, and a theme
>   toggle. Under 900px it becomes a slide-in panel (spring easing, rounded right
>   edge, scrim-free, `esc` closes, closes on navigation).
> - **Mobile tab bar**: fixed bottom, floating (10px inset), frosted pill with
>   4 items (icon above a 10.5px label), active item filled with the accent
>   gradient and a glow. Under 560px the topbar's host chip hides and toasts move
>   above the tab bar (`bottom: 84px`) so they never cover it.
> - **Topbar**: menu button (mobile), gradient page title with a host chip, API
>   URL field + Connect, and the command-palette button with a `⌘K` key cap.
> - **Overview**: warning banner (each warning names the env var that fixes it),
>   then six stat cards with tinted spark icons, big tabular numbers and a
>   one-line sub label — the whole card is clickable where that makes sense.
> - **Projects**: a create form (name, agent count, plan-only vs plan-and-run,
>   description) inside a tinted glass card, then a table: project + id, status
>   pill, tasks done/total, animated gradient progress bar, row actions
>   (Open, Run, Handoff).
> - **Project drawer** (right side, 560px, slide-in with spring easing, scrim,
>   `esc` closes): description, progress bar, four stat cards, task table with
>   status pills and scores, artifact chips that copy a download URL, and an
>   event-history log — all updated live from the SSE stream, with a dot showing
>   stream health.
> - **Connectors**: table of services; readiness pill with the missing setting in
>   a tooltip; action `<select>`; **Call** (prompt for JSON params, confirm
>   dangerous ones in a dialog) and **Probe** (shows latency or the error).
> - **Agents & storage**: two tables — pool (busy/idle, done/failed, last error)
>   and drive accounts (healthy, used, free, bucket).
> - **Footer**: MIT, source link, and the privacy line: *no telemetry, no
>   analytics, no tracking — this page talks only to your API.*
>
> ### Onboarding (first run, and on demand)
>
> A modal overlay over a heavily blurred scrim, three steps, one card, animated
> in with a spring pop:
>
> 1. **Welcome** — what Kollektiv does, in three bullet cards with tinted icon
>    tiles (plan → parallel work; nothing lost between sessions; your tools).
> 2. **Point it at your API** — one input for the base URL (prefilled from the
>    guess), plus the sentence that matters: the only requests this page makes
>    are to that address.
> 3. **Keyboard first** — `⌘K` palette, `g` + `p/c/a` navigation, `t` theme,
>    `r` refresh.
>
> A living gradient tile (a 132px mosaic that slowly hue-rotates) sits at the top
> of every step. Footer: progress dots (the active one grows into a 30px
> gradient pill), *Skip*, *Back* (disabled on step 1), *Next* → *Start building*.
> Finishing fires a 34-piece CSS confetti burst, stores
> `kollektiv.onboarded=yes` and toasts a next action. `esc` skips; `Enter`
> advances. Reopen any time from **Show me around** in the sidebar footer.
> Onboarding is skippable, local, and never blocks the dashboard.
>
> ### Interaction requirements (do not skip)
>
> - **Hover states everywhere**, transitions 140–350 ms on a shared easing curve
>   (`cubic-bezier(0.22, 0.61, 0.36, 1)`) with a springy
>   `cubic-bezier(0.34, 1.56, 0.64, 1)` for entrances: cards lift 5px + tilt +
>   spotlight + tinted shadow; buttons rise 3px, scale 1.02, saturate and sweep a
>   diagonal sheen; nav pills slide 3px with a growing gradient bar; table rows
>   wash with a two-stop accent gradient and grow a 4px accent edge; pills lift
>   and reveal a CSS-only tooltip (`data-tip`); the logo tile tilts -9° and
>   saturates; icon buttons rotate -6°; inputs brighten toward the cyan accent
>   and take a 3px focus ring on `:focus-visible`.
> - **Active/pressed**: 0.97 scale or a 1–3px dip — never a jump.
> - **Loading**: shimmer skeletons with a cyan sweep, never a blank page.
> - **Empty states**: a floating conic-gradient tile, a bold title and a sentence
>   that says what to do next.
> - **Command palette** on ⌘K / Ctrl-K: fuzzy filter over commands *and*
>   projects, ↑/↓ to move, ↵ to run, `esc` to close, focus trapped,
>   `role="dialog"`, selected row filled with the accent gradient.
> - **Toasts** bottom-right (above the tab bar on phones): tone-coloured left
>   border, spring slide-in, auto-dismiss ~5 s, click to dismiss,
>   `aria-live="polite"`.
> - **Theme**: dark first, light via `prefers-color-scheme`, plus a persisted
>   manual toggle; all colours from CSS custom properties; light mode keeps every
>   hue (it is a palette swap, not a desaturation).
> - **Accessibility**: semantic landmarks, labelled inputs, `data-label` on every
>   table cell so the mobile card layout keeps its labels, keyboard reachable
>   everywhere, visible focus, `prefers-reduced-motion` kills aurora drift, tilt,
>   shimmer and confetti, contrast ≥ 4.5:1 for text.
> - **Performance**: no render-blocking resources, no images beyond the local
>   SVG, no remote anything; total < 200 KB uncompressed; first paint < 1 s on a
>   laptop. Blur layers are `position: fixed` and never animate layout.
>
> ### Responsive behaviour
>
> - `> 900px`: sidebar rail + content, tables as tables.
> - `≤ 900px`: sidebar becomes a slide-in panel, a floating frosted tab bar
>   appears, tables become cards — the header row hides and each cell shows its
>   `data-label` above the value.
> - `≤ 560px`: host chip hides, cards padding tightens, toasts sit above the tab
>   bar.
>
> ### Tone of the copy
>
> Precise and calm, never chirpy — the colour is allowed to be loud, the words
> are not. Say what happened and what to do next ("No projects yet — describe
> what to build above and press Create"), name the env var when something is
> unconfigured, and never claim success for an action that returned an error.
> The onboarding may be warm (it is the one place a first-time user meets the
> product); everything after it is an instrument panel.

---

## Iterating

When the API changes, update three places together:

1. the endpoint table above;
2. `web/assets/app.js` (the `api()` calls and the relevant renderer);
3. `web/README.md`'s feature list, and the test
   `test_dashboard_calls_the_documented_endpoints`.

When you change the look, keep the file split: `styles.css` for anything visual,
`app.js` for behaviour, `index.html` for structure — and keep the four rules at
the top of this file: multi-file, hover states real, not plain, zero
dependencies. `tests/test_dashboard.py` enforces the mechanically checkable half
of that (file split, hover coverage, token usage, onboarding and mobile chrome
present, manifest shipped, no third-party origin).
