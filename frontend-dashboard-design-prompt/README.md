# Dashboard design prototype (not the shipped UI)

This folder is a **React + Vite prototype** of the Kollektiv dashboard. It is
not the dashboard the project serves.

- The shipped dashboard is [`web/`](../web/README.md). The runtime serves it at
  `/ui`, the Pages workflow deploys it, and CI checks it.
- This prototype is not served by the API, not deployed, and not built or
  tested in CI. Its build works (`npm ci && npm run build`), but nothing depends
  on it.
- It calls the same API endpoints and falls back to `src/lib/mock.ts` when the
  API is unavailable.

Which UI the project ships is still an open decision. See
[`docs/ui-prompt.md`](../docs/ui-prompt.md) for the requirements both must meet.
