# VoiceRecap Cloudflare Free Backend

VoiceRecap uses this Cloudflare Python Worker as an **account and AI gateway**, not as a media storage service. It provides FastAPI-compatible authentication, D1 account state, credit plans priced in MMK/USDT, payment review, audit logs, browser administration at `/admin`, fair Gemini key scheduling, and a stateless Modal VoxCPM2 proxy.

> **Local-media policy:** the Android app retains the source video, extracted WAV, clone-reference audio, generated TTS WAV, transcript, subtitles, and final MP4 on the user’s device. The Worker does not have an R2 binding, does not write audio/video to object storage, and does not accept an endpoint for uploading generated results.

When deploying through **Workers Builds → Import a repository**, use repository root (`/`). Set the build command to `npm run build` and deploy command to `npx wrangler deploy`. The root configuration deploys `cloudflare/src/entry.py` as Worker `recapmaker--2727`.

## Operational architecture

```text
Android local project workspace
  ├─ source video / extracted WAV / reference voice / TTS WAV / output MP4
  │
  ├─ POST /transcribe: request-scoped WAV only
  ▼
Cloudflare Worker + D1
  ├─ auth, plans, credits, payments, audit metadata
  ├─ fair Gemini account/model slot selection
  └─ stateless Modal VoxCPM2 relay
       │
       ├─ Gemini Files API: temporary upload, deleted in finally
       └─ Modal: reference audio and generated WAV exist only in the request
```

The Gemini Files API object is explicitly deleted after each transcription attempt. Clone reference audio is carried in the TTS request only and is never written to D1, R2, a queue, or a backup. The Android app must not submit generated WAV or MP4 files to the backend.

## Cloudflare provisioning

Only a D1 database is needed. **Do not create R2 buckets or Queues for this version.** This removes the R2 activation error entirely.

```bash
npx wrangler d1 create voicerecap-db
# Copy the resulting database_id into both wrangler.jsonc files.
npx wrangler d1 migrations apply voicerecap-db --remote
```

Set the Worker secrets. Never put secrets in Git, D1, Admin UI, or the Android APK.

```bash
openssl rand -base64 48 | npx wrangler secret put JWT_SECRET
npx wrangler secret put ADMIN_PASSWORD
npx wrangler secret put GEMINI_KEY_1
npx wrangler secret put GEMINI_KEY_2
npx wrangler secret put GEMINI_KEY_3
```

The admin panel stores only a secret binding name, such as `GEMINI_KEY_1`, in each Gemini slot. The actual key remains a Cloudflare Worker secret. Use separate Google projects/accounts for keys when possible, because multiple keys in one Google project share that project’s quota.

Deploy with:

```bash
npm run build
npx wrangler deploy
```

Set the Android backend base URL to the resulting `workers.dev` URL, including a trailing `/`.

## Timeout and reliability policy

Cloudflare documents that HTTP-triggered Workers have no hard wall-clock duration while the client remains connected, and waiting on network I/O does not count toward CPU time. Workers Free remains constrained to 10 ms active CPU and 128 MB memory, so the Worker only relays requests and never performs video/audio rendering. [1]

| Boundary | Value | Behavior |
|---|---:|---|
| Android connection/write/read timeout | 30 s / 900 s / 900 s | A foreground WorkManager job retains the user-visible operation for up to 15 minutes. |
| Worker provider timeout | 900 s | The Modal fetch is bounded at 15 minutes. A provider’s own shorter limit still applies. |
| Gemini request | Client remains connected | Raw WAV stays only in Worker memory for the request; Gemini temporary file is deleted in a `finally` block. |
| Retry | Gemini only, up to 3 attempts | Retry occurs only for retryable provider failures, with slot cooldown. |
| TTS credit handling | Reserve then refund | Any failed Modal generation/batch refunds the reservation idempotently. |

A client disconnect can cancel in-flight Worker work. Android therefore uses an idempotency key per transcription project and persistent local project state. Retrying the same completed key returns the saved **transcript metadata** only; it does not retain any audio. The app should create a new idempotency key after a disconnected in-progress request, because the provider result may be unknown.

## First administrator login

Set `ADMIN_EMAIL` as a non-secret environment variable and `ADMIN_PASSWORD` as a secret. The administrator account is created or synchronized on its first successful login. No bootstrap token is used.

Open `https://YOUR_WORKER_URL/admin`, sign in with the configured credentials, create plans, and configure Gemini slots. Slots use priority, concurrency, RPM, daily limit, cooldown, and least-recently-used selection so one busy API key does not block every user.

## API highlights

| Route | Purpose | Media retention |
|---|---|---|
| `POST /api/v1/auth/register` | Create account and apply eligible welcome credits | None |
| `POST /api/v1/auth/login` | Issue access and refresh tokens | None |
| `GET /api/v1/plans` | Active plans with MMK/USDT prices and credits | None |
| `POST /api/v1/transcribe` | Receive request-scoped WAV, call Gemini, return completed transcript | No Worker storage; Gemini file deleted |
| `GET /api/v1/transcribe/{job_id}` | Fetch transcript job metadata/result JSON | Transcript metadata only |
| `POST /api/v1/tts/generate` | Proxy one Modal VoxCPM2 generation | No Worker storage |
| `POST /api/v1/tts/batch` | Proxy dubbing segments and return WAV base64 | No Worker storage |
| `POST /api/v1/backup/export` | Return metadata-only JSON for Android to save locally | No Worker storage |
| `POST /api/v1/backup/import` | Import project metadata without restoring credits | No media accepted |
| `/api/v1/admin/*` | Users, plans, credits, payments, Gemini slots, audit | Account metadata only |

## Android integration

The Android app selects a **target language before processing**, extracts audio locally, then calls `/api/v1/transcribe` with the WAV body and headers:

```text
X-Target-Language: my
X-Video-Duration-Seconds: 73
Idempotency-Key: project:<project-id>:transcription
```

The direct response is normally `completed` with the structured transcript. The app writes the result to Room, keeps generated audio in its private project workspace, and writes the final MP4 through MediaStore under `Movies/VoiceRecap`. Clone reference audio is passed only when a user previews or exports clone TTS; it must be stored as a local URI, never in WorkManager input data as base64.

Build with the deployed backend URL:

```bash
VOICERECAP_BACKEND_URL=https://YOUR_WORKER_URL/ ./gradlew assembleRelease
```

## References

[1]: https://developers.cloudflare.com/workers/platform/limits/ "Cloudflare Workers limits"
