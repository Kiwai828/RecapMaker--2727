# VoiceRecap Cloudflare Backend

This repository contains the Cloudflare Free-ready VoiceRecap backend under [`cloudflare/`](./cloudflare/). It is a Python Worker with FastAPI-compatible routes, D1 migrations, R2 media/backup storage, Cloudflare Queues, a fair Gemini multi-account scheduler, credit-based plans, MMK/USDT payment-order review, Modal VoxCPM2 TTS proxying, backup/import, and an `/admin` dashboard.

## Deploy from Cloudflare dashboard

In Cloudflare Dashboard, open **Workers & Pages → Create application → Import a repository → GitHub**, select this repository, and choose the `main` branch. Set the project/root directory to `cloudflare/` if the dashboard asks for a root directory. The source uses `wrangler.jsonc` and the Python Workers `pywrangler` workflow; it is a Workers deployment, not a static Pages-only deployment.

Before the first production deployment, create the D1 database, both R2 buckets, and the Queues named in `cloudflare/wrangler.jsonc`, put the D1 `database_id` into that file, apply `cloudflare/migrations/0001_initial.sql`, and add Worker secrets for `JWT_SECRET`, `ADMIN_PASSWORD`, and each `GEMINI_KEY_*` binding used by the admin Gemini slots. Do not commit API keys or `.dev.vars` files.

The complete setup, migration, admin password login, credit-plan configuration, Android build, and Free-tier boundary instructions are in [`cloudflare/README.md`](./cloudflare/README.md).
