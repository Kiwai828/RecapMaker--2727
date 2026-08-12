# VoiceRecap Cloudflare Backend

This repository contains the Cloudflare Free-ready VoiceRecap backend under [`cloudflare/`](./cloudflare/). It is a Python Worker with FastAPI-compatible routes, D1 migrations, R2 media/backup storage, Cloudflare Queues, a fair Gemini multi-account scheduler, credit-based plans, MMK/USDT payment-order review, Modal VoxCPM2 TTS proxying, backup/import, and an `/admin` dashboard.

## Cloudflare Workers Builds settings

In Cloudflare Dashboard, open **Workers & Pages → Create application → Import a repository → GitHub**, select this repository and the `main` branch. This repository now includes a root `wrangler.jsonc`, so keep **Root directory empty** or set it to `/`. Do not set the root directory to `cloudflare/` when using the root configuration.

Set the Workers Builds commands as follows:

| Setting | Value |
|---|---|
| Build command | `npm run build` |
| Deploy command | `npx wrangler deploy` |
| Non-production deploy command | `npx wrangler versions upload` |
| Root directory | `/` or empty |

The build command runs `uvx --from workers-py pywrangler sync` inside `cloudflare/`; the deploy command then uses the root `wrangler.jsonc`, whose `main` is `cloudflare/src/entry.py`. The previous build failed because Cloudflare ran `npx wrangler deploy` from the repository root while no root Wrangler configuration existed, so Wrangler attempted to detect a static asset directory.

Cloudflare Workers Builds requires the Worker name in the dashboard to match the `name` in the Wrangler configuration. The current configuration uses `recapmaker--2727`, matching this repository's current Workers project name. If the dashboard Worker has a different name, either rename the Worker or change `name` in both Wrangler configurations before deploying.

## First deployment prerequisites

Before the first production deployment, use the resource names in the root `wrangler.jsonc`; the current configuration omits the D1 ID so Wrangler can auto-provision the D1 database, R2 buckets, and Queues during the first deploy. If you already created the resources manually, add their real IDs/names before deploying. Apply `cloudflare/migrations/0001_initial.sql`, and add Worker secrets for `JWT_SECRET`, `ADMIN_PASSWORD`, and each `GEMINI_KEY_*` binding used by the admin Gemini slots. Do not commit API keys or `.dev.vars` files.

The complete migration, admin password login, credit-plan configuration, Android build, and Free-tier boundary instructions are in [`cloudflare/README.md`](./cloudflare/README.md).
