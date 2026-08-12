# VoiceRecap Cloudflare Free-only Backend

This directory is a Cloudflare Python Worker deployment for VoiceRecap. It provides an authenticated FastAPI-compatible API, D1 state, R2 audio/backup storage, a Queue-backed fair transcription scheduler, credit-based plans, MMK/USDT payment order review, user backup/export, audit logging, and a browser admin panel at `/admin`.

Cloudflare currently documents Python Workers and FastAPI support, but the Python runtime is still in open beta. The Worker is therefore intentionally isolated from the Android client contract and uses D1/R2/Queues bindings instead of Uvicorn, SQLAlchemy, or PostgreSQL. See `../CLOUDFLARE_DEPLOYMENT_NOTES.md` for the architecture rationale and Free-tier boundaries.

## Local setup

Install Node.js, `uv`, and Wrangler. Then run:

```bash
cd cloudflare
uv run pywrangler --version
cp .dev.vars.example .dev.vars
# edit .dev.vars with local-only values
npx wrangler d1 migrations apply voicerecap-db --local
uv run pywrangler dev
```

The Worker entrypoint is `src/entry.py`. `src/main.py` exposes the FastAPI routes, while `src/scheduler.py` is the Queue consumer. The queue message contains only a job ID; audio bytes are kept in R2.

## Cloudflare provisioning

Create the resources in the Cloudflare account that owns the Worker:

```bash
npx wrangler d1 create voicerecap-db
npx wrangler r2 bucket create voicerecap-media
npx wrangler r2 bucket create voicerecap-backups
npx wrangler queues create voicerecap-jobs
npx wrangler queues create voicerecap-dead-letter
```

Copy the returned D1 database ID into `wrangler.jsonc`. Keep the R2 bucket names and queue names consistent with that file. Apply the schema remotely:

```bash
npx wrangler d1 migrations apply voicerecap-db --remote
```

Set secrets. Never put these values in `wrangler.jsonc`, D1, Git, or the APK:

```bash
openssl rand -base64 48 | npx wrangler secret put JWT_SECRET
npx wrangler secret put ADMIN_PASSWORD
npx wrangler secret put GEMINI_KEY_1
npx wrangler secret put GEMINI_KEY_2
npx wrangler secret put GEMINI_KEY_3
npx wrangler secret put MODAL_TTS_TOKEN
```

Each Gemini slot created in the admin panel stores a non-secret binding name such as `GEMINI_KEY_1`. The corresponding Worker secret must exist. Multiple keys should represent genuinely separate Google projects/accounts when possible; keys from one project do not multiply Google project quota.

Deploy:

```bash
uv run pywrangler deploy
```

The deployment URL is a `workers.dev` URL unless a custom domain is configured. Set the Android backend base URL to that URL with a trailing slash. The Worker must be deployed before the Android app is used against it.

## First admin login

Set `ADMIN_EMAIL` in `wrangler.jsonc` and set the matching `ADMIN_PASSWORD` as a Worker secret. The administrator account is created or synchronized automatically on the first successful login; no bootstrap endpoint or one-time token is required.

```bash
npx wrangler secret put ADMIN_PASSWORD
```

Open `https://YOUR_WORKER_URL/admin` and sign in with the configured email and environment password. The configured password is the source of truth; if it changes, the stored admin hash is synchronized on the next login. Add Gemini account/project/model slots after signing in. A slot has its own concurrency, RPM, and daily limits. The scheduler chooses the least-recently-used eligible slot, cools down provider failures, and runs one message per queue consumer invocation.

## Credit plans and payment handling

The admin panel can create plans with the following fields:

| Field | Meaning |
|---|---|
| Included credits | Credits granted after an approved purchase or welcome grant |
| Credits per video | Fixed base charge for one video |
| Extra credits per minute | Optional duration-based charge, rounded up by minute |
| TTS credits / 100 characters | Dubbing text charge |
| Voice clone credits | Optional cloning charge |
| Price MMK | Display and order amount in Myanmar kyat |
| Price USDT | Display and order amount in USDT as a decimal string |
| Validity days | Active plan duration; `0` means no expiry |
| Max video seconds | Application-level media limit |

The current payment boundary intentionally uses manual review. A user submits a MMK or USDT payment order and optional transaction reference/proof key; an administrator verifies the payment outside the Worker, then presses **Approve**. Approval is idempotent and grants included credits once. Automatic bank or blockchain settlement is not claimed by this Free-only package because it requires a provider-specific payment integration and credentials.

Credit deductions are ledgered with an idempotency key. Failed queued transcription jobs refund the reserved credits. User backup import never restores credit balance, because allowing client-provided balance restoration would be an abuse vulnerability.

## API highlights

| Route | Purpose |
|---|---|
| `POST /api/v1/auth/register` | Register and grant the configured Free-plan welcome credits |
| `POST /api/v1/auth/login` | Issue access and refresh tokens |
| `GET /api/v1/plans` | Public active plan list with MMK/USDT prices and credit rules |
| `GET /api/v1/credits/balance` | Current credit wallet |
| `POST /api/v1/transcribe` | Upload raw WAV, reserve credits, and enqueue one Gemini job |
| `GET /api/v1/transcribe/{job_id}` | Poll queued/processing/completed/failed status |
| `POST /api/v1/tts/generate` | Credit-aware Modal VoxCPM2 preview proxy |
| `POST /api/v1/tts/batch` | Credit-aware Modal VoxCPM2 batch dubbing proxy |
| `POST /api/v1/backup/export` | Write a user backup manifest and JSON object to R2 |
| `POST /api/v1/backup/import` | Validate a user-owned backup without restoring credits |
| `/api/v1/admin/*` | Admin dashboard APIs for users, plans, credits, payments, Gemini slots, jobs, and audit |
| `GET /admin` | Browser admin panel |

## Important Free-plan boundary

The default Free Worker plan includes limited daily Worker requests and the Free Queue plan includes limited daily queue operations and 24-hour message retention. The API therefore returns `queued`, enforces a configurable queue-depth limit, and applies a Free daily job limit. It cannot promise unlimited simultaneous video processing. If demand exceeds the Cloudflare Free allocation, users see a controlled queue/full response instead of an uncontrolled provider rate-limit storm.

For large-scale video import from YouTube/TikTok, native `yt-dlp` is not available inside a Python Worker. The current Cloudflare-only package accepts audio bytes from the Android local extraction path. URL import remains in the original FastAPI backend; a separate media-extraction service is required if URL import must also be Cloudflare-only.

## Android integration

The Android app now lists admin-created credit plans with MMK and USDT prices, creates payment orders for manual review, and sends the extracted WAV as the request body with:

```text
X-Target-Language: my
X-Video-Duration-Seconds: 73
Idempotency-Key: project:<project-id>:transcription
```

It polls the returned `job_id` until the fair queue completes, then writes Gemini `original_text`, translated `tts_text`, and timestamps into the existing Room transcript editor. The backend remains the only location that holds Gemini credentials. To build the Android APK for the deployed Worker, run `VOICERECAP_BACKEND_URL=https://YOUR_WORKER_URL/ ./gradlew assembleRelease` from the `android` directory.
