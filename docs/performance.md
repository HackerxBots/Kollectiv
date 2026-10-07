# Performance, and the honest answer to "is Python too slow?"

Every few months someone asks whether Kollektiv should be rewritten in Rust, Go,
TypeScript or C#. The short answer is **no, and the numbers say why** — but there
are four places where a faster language (or a faster Python) genuinely earns its
keep, and this page is where we decide that on evidence instead of vibes.

Run the measurements yourself:

```bash
python scripts/benchmark.py          # the table on this page
python scripts/benchmark.py --quick  # smaller N
python scripts/benchmark.py --json   # for a spreadsheet or a CI job
```

## Where a run's seconds actually go

A run is a loop that hands work to language models and waits. The plan is one
model call; each task is one worker call plus one review; the file writes and
database rows are milliseconds. Measured on the reference machine (CPython
3.11.2, Linux, one modern laptop-class core), with the heuristic brain so nothing
touches the network:

| What | Median | Per call | Why it matters |
| --- | --- | --- | --- |
| `estimate.json@100` | 0.027 ms | 0.3 µs/task | estimating a run is free — it is arithmetic over the plan |
| `estimate.json@1000` | 0.187 ms | 0.2 µs/task | scales linearly, as it must |
| `estimate.json@5000` | 0.937 ms | 0.2 µs/task | 5 000 tasks still under a millisecond; no hidden quadratic |
| `config.parse@2000` | 1 430 ms | 0.72 ms | PyYAML parsing `.kollektiv.yml`, once per command |
| `config.subset@2000` | 93 ms | 0.05 ms | the built-in strict reader: **15× faster**, and no dependency |
| `connectors.build` | 192 ms | once | imports 9 connector modules at startup |
| `connectors.catalog` | 0.032 ms | — | the 41-action catalogue clients read |
| `gateway.catalogue` | 0.30 ms | — | all 57 gateway tools |
| `project.create` | 10.2 ms | — | plan (heuristic) plus database writes for `POST /projects` |
| `budget.record@2000` | 4 751 ms | 2.4 ms/row | one ledger row per project **per day** — 2 000 of them is a stress test, not a workload |
| `run.empty_pool` | 0.027 ms | — | nothing dispatched = microseconds of orchestration |
| `health` | 2.7 ms | — | instant by design: it reports configuration, never queries the ledger |
| `cli.help` (cold start) | 223 ms | once | process start to argument parsing: imports, not computation |

And what a run actually costs in wall-clock time: at the shipped defaults, a
two-task project is **4 brain calls + 2 worker calls**. Each is one HTTP round
trip to a model that takes hundreds of milliseconds to tens of seconds to answer.
The Python work in that run is the 10 ms of planning, a few milliseconds of
database and file writes, and the estimate above. **Under 1 % of the wall clock
is Python.** A 50× faster interpreter would make a five-minute run four seconds
shorter, and only if the calls stayed serial.

The two numbers worth improving, if you care at all:

* **223 ms of process start** is the price of importing FastAPI, SQLModel, httpx
  and the rest before a CLI command does anything. Left alone deliberately: it is
  paid once per command, and the alternative — lazy imports everywhere — makes the
  code worse for a saving no user can perceive.
* **192 ms building the connector registry** at startup. Nine connectors, each
  imported on demand inside `from_settings`, because a `kollektiv estimate` that
  never sends a message should at least never load a Slack client. If it ever
  grows, the next step is to import each connector only when it is configured.

## The escalation ladder (do these in order)

1. **Concurrency before speed.** A run already dispatches tasks concurrently
   (`max_concurrency`), and every call is `httpx.AsyncClient` with retries. If a
   run feels slow, the answer is a cheaper/faster model, more workers, or a
   shorter critical path — not a different language. Profile the *plan* first:
   `kollektiv estimate --project-id prj_…` shows the calls, and one needless
   review round trip costs more than every microsecond of Python in the project.
2. **A newer interpreter.** Python 3.14 is ~27 % faster than 3.13 on
   single-threaded work, and the free-threaded build has gone from a ~40 %
   single-thread penalty (3.13t) to roughly 5–10 %, with ~3–4× scaling on four
   threads for CPU-bound code. When the ecosystem settles, `requires-python`
   moves to 3.14 and anyone who wants it can run the free-threaded build
   unchanged — our own `tests/test_perf.py` guards the paths that would benefit.
   Caveat to keep in mind: any C extension that has not opted in **silently
   re-enables the GIL**, so measure, don't assume.
3. **PyO3 for one hot function, if a profiler names one.** Rust is 10–100× faster
   on CPU-bound loops, and `maturin` makes a Rust module a normal `pip install`
   for users. The rule here: a profiler must show **one pure-Python function over
   ~10 % of a real run's wall clock**, and the speedup must be worth a compiled
   wheel per platform (which our pure-Python `pip install` currently avoids).
   Nothing in the numbers above comes close, so this is a tool we are holding,
   not using.
4. **Parallel processes.** The orchestrator is stateless between calls; two
   instances with two databases scale further than any rewrite, and they are
   already supported (SQLite or Postgres, object storage, GitHub as the sync
   layer).
5. **A rewrite** — Rust/Go/C#/C++ — is the last rung and, for this project, the
   wrong one. What it buys: the sub-1 % of wall clock above. What it costs: the
   464-test suite, FastAPI/SQLModel/httpx/MCP, the OpenAI-compatible client
   stack, every connector, and the ability of a contributor to read the code.
   If Kollektiv ever needs a rewrite it will be because the *product* changed
   (a hosted control plane serving thousands of tenants, say), not because
   Python is slow.

## Language by layer, and why

| Layer | Language today | Right answer | Notes |
| --- | --- | --- | --- |
| Orchestrator, brain, connectors, gateway, MCP | Python 3.11+ | **Python** | I/O orchestration with retries, JSON in, JSON out. The expensive part is the model, not the interpreter. |
| Cost model, budgets, ledger | Python | **Python** | 0.2 µs/task; SQLite handles the rest |
| Dashboard (`web/`) | plain ES modules, no build step | **TypeScript when someone wants it** | The UI is 1 700 lines of dependency-free JS. A build step buys types and costs contributors a toolchain; if the frontend becomes its own project, TypeScript is the obvious pick |
| Gateway transport | Python (FastAPI + MCP SDK) | **Python** | It is a proxy over the same tools; a Go gateway would be faster and would duplicate 57 tool definitions |
| Desktop shell, if we ship one | — | **Rust (Tauri 2)** | Smallest shell, OS webview, mobile later |
| Hot numeric loops, if a profiler finds them | — | **Rust via PyO3** | See rung 3 |
| CI, deploys, infra | YAML + bash | **YAML + bash** | Nothing to rewrite |

## Browser or desktop app?

Three honest tiers, in order of what exists today.

**1. Installable PWA — shipped.** `web/` is a real app window on desktop and
mobile: service worker for the shell, `display: standalone`, an install prompt
where the browser offers one, and instructions for iOS Safari (which has no
prompt of its own). It costs nothing, ships on the same free Pages URL, and works
with the API wherever you run it. What it is *not*: an offline engine. The shell
loads offline; the work needs the API.

**2. A Tauri 2 desktop app — the recipe, not built yet.** Tauri wraps the exact
`web/` directory we already have in a Rust shell that uses the OS webview, and
can carry the API as a *sidecar* process, so "download Kollektiv" means one
installer that starts everything. The honest numbers:

| | Tauri 2 | Electron |
| --- | --- | --- |
| Shell size | 3–10 MB | 120–200 MB |
| Idle RAM | 40–80 MB | 150–400 MB |
| Startup | < 200 ms | 2–5 s |
| Rendering | OS webview (WebKit/WebView2/WebKitGTK) — small CSS differences between platforms | bundled Chromium — identical everywhere |
| Mobile | iOS + Android in v2 | none |

  Subtract the romance: the sidecar needs a frozen Python (PyInstaller-ish) of
  roughly 40–80 MB per platform, so the *real* download is 60–120 MB against
  Electron's 150–250 MB — smaller and nicer, not magic, and the OS webview still
  needs the dashboard's CSS checked per platform. The trigger to build it: a user
  who wants a one-click install and no terminal. The cost: a Rust toolchain, three
  signed builds in CI (Apple/Windows certificates), and a release process we would
  have to maintain. Until then `docker compose up` and a `kollektiv` CLI *are* the
  desktop app.

**3. The engine in the browser — not possible, and not worth pretending.**
Running the whole orchestrator client-side means CPython compiled to WebAssembly
(Pyodide), which is what Cloudflare Python Workers use. The published limits
settle it: **128 MB of memory**, **10 ms of CPU per request on the free plan**
(30 s paid), **no threads and no multiprocessing**, no subprocesses, no
filesystem, and any C extension that Pyodide has not pre-compiled simply fails to
import. Kollektiv needs a workspace directory, git, a long-lived scheduler and
simultaneous in-flight HTTP calls; CORS would block most model and connector
endpoints from a browser anyway. So: the browser gets the dashboard (tier 1), the
engine needs a process — yours, a container, or a hosted one.

## Keeping it honest

`scripts/benchmark.py` is the report; `tests/test_perf.py` is the tripwire. The
tests assert generous ceilings (estimating 5 000 tasks under 100 ms, health under
500 ms, an offline project create under 2 s) so that a real regression — a
quadratic loop, a synchronous call smuggled into an async path, a ledger query
inside `/health` — fails in CI, while a slow machine does not.

Numbers on this page come from one machine and one interpreter. Yours will
differ; the shape will not.

---

* [Kollektiv documentation](README.md) — the hub
* [Budgets and `.kollektiv.yml`](budget.md) — what a run costs in dollars, not milliseconds
* [Deployment](deployment.md) — where the API runs, and where the dashboard is hosted
* [Architecture](architecture.md) — how a run works, and why it is network-bound

Back to the [project README](../README.md).
