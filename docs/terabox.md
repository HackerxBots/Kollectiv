# TeraBox shared storage

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

TeraBox is the optional shared drive. Project files, `PROJECT_STATE.md` and
artifacts go there when it is configured. Without it, everything stays in the
local workspace, which is fine for one machine. Cloudflare R2 is the other
option (see [deployment](deployment.md)).

## What Kollektiv does with it

- Uploads go to `TERABOX_REMOTE_ROOT` (default `/Kollektiv`), one folder per project.
- With several accounts, each upload goes to the account with the most free
  space (`TeraBoxPoolManager`).
- Tokens refresh automatically. They are stored encrypted, never in logs.
- Passwords are never used or stored. The template in `.env.example` holds
  tokens only.

## Setup

1. Create an app on the TeraBox Open Platform. The console and its steps change,
   so follow the platform's own guide. Copy the app id and app key.
2. Run the platform's OAuth authorisation for each account, and copy the
   `access_token` and `refresh_token`. Kollektiv has no login command for this
   step yet.
3. In `.env`:

   ```bash
   TERABOX_APP_ID=your-app-id
   TERABOX_APP_KEY=your-app-key
   TERABOX_ACCOUNTS='[{"email":"you@example.com","access_token":"...","refresh_token":"..."}]'
   ```

4. Check it on the machine that has the credentials:

   ```bash
   kollektiv check --live --require-storage
   ```

   The storage section should show `configured: true` and the account count.
   The API's `GET /storage/status` (admin only) shows quota per account.

## What is not verified yet

- **Live behaviour.** The automated tests use a mock TeraBox server. The CI and
  development sandbox cannot reach the TeraBox API, so the first live run is
  yours. Run the check above before you rely on it.
- **Terms of service.** Pooling several free accounts may be against TeraBox's
  terms. Read the Open Platform terms before you use more than one account. This
  is a decision for the owner, not a default.
- **Per-user folders.** All projects share one root. Project ids are random and
  every project route checks the owner, but the remote folder is not split per
  user yet. That is a planned change, and it moves existing paths, so it needs a
  migration.
