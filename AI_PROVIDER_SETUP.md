# VoiceRecap AI provider setup

VoiceRecap now uses a provider-neutral, admin-managed pipeline:

1. **Speech-to-text:** OpenRouter transcription API, with `openai/whisper-large-v3` recommended for Myanmar/Burmese language coverage and timestamped segments.
2. **Translation and dubbing text:** OpenCode Zen chat-completions models. Multiple Zen API keys and multiple models can be added without changing application code.
3. **Media retention:** The Android app keeps video, extracted audio, transcript, TTS clips, subtitles, and final MP4 locally. The Worker holds request audio only in memory while the provider call runs.

## Cloudflare secrets

Configure these in the Worker’s **Settings → Variables and Secrets** page. Secret values are never placed in the APK or stored in D1.

| Binding name | Type | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | Secret | OpenRouter STT catalog and Whisper transcription |
| `OPENCODE_ZEN_KEY_1` | Secret | First OpenCode Zen translation key |
| `OPENCODE_ZEN_KEY_2` | Secret, optional | Second Zen key for failover |
| `OPENCODE_ZEN_KEY_3` | Secret, optional | Third Zen key for failover |
| `JWT_SECRET` | Secret | VoiceRecap access/refresh token signing |
| `ADMIN_PASSWORD` | Secret | Admin account bootstrap password |

A binding name is only a reference stored in D1. For example, adding a model with `secret_name=OPENCODE_ZEN_KEY_2` makes the Worker read the value of that binding at request time.

## Admin panel workflow

Open `/admin`, sign in as the administrator, and open **AI provider models**. Choose **OpenRouter — Whisper STT** or **OpenCode Zen — translation**, confirm the secret binding name, and press **Fetch live models**. The Worker calls the provider’s live catalog endpoint and returns the current model IDs, display names, modalities, pricing, supported parameters, free-price indicator, and expiration metadata. Select a returned model and press **Add selected model**.

Do not type a model ID from memory when the catalog is available. The catalog is intentionally dynamic because model IDs, provider availability, prices, and deprecation status can change. If a model disappears, disable or delete the old row and add its current catalog entry.

## Recommended rows

Create one OpenRouter STT row:

| Field | Recommended value |
|---|---|
| Provider | `openrouter_stt` |
| Capability | `stt` |
| Model | `openai/whisper-large-v3` |
| Secret binding | `OPENROUTER_API_KEY` |
| Priority | `0` |
| Concurrency | `1` initially |

Create at least two OpenCode Zen translation rows. The priority with the lower number is tried first. For example:

| Priority | Model | Secret binding |
|---:|---|---|
| 0 | `deepseek-v4-flash-free` or the current catalog ID | `OPENCODE_ZEN_KEY_1` |
| 1 | `mimo-v2.5-free` or the current catalog ID | `OPENCODE_ZEN_KEY_2` |

Use the exact IDs returned by the live Zen catalog. Free Zen models are not guaranteed to be permanent or unlimited. The Worker cools down a model after a provider rate-limit or temporary error, then tries the next enabled model/key. It never calls `openrouter/free` for translation.

## Failure and credit behavior

The Worker uses an idempotency key from Android to prevent duplicate processing reservations. A provider 429 is returned as `AI_PROVIDER_RATE_LIMIT` with a `Retry-After` header; the failed model is cooled down and reserved credits are refunded. If all enabled rows are unavailable, the Worker returns `AI_PROVIDER_CAPACITY` as HTTP 503 rather than creating more duplicate provider requests. An invalid API key or invalid model returns a provider configuration/rejection error and is visible in the admin job/audit records.

## API endpoints

| Endpoint | Use |
|---|---|
| `GET /api/v1/admin/ai-models/catalog?provider=openrouter_stt&capability=stt&secret_name=OPENROUTER_API_KEY` | Fetch live OpenRouter transcription catalog |
| `GET /api/v1/admin/ai-models/catalog?provider=opencode_zen&capability=translation&secret_name=OPENCODE_ZEN_KEY_1` | Fetch live Zen model catalog |
| `GET /api/v1/admin/ai-models` | List configured rows without exposing secret values |
| `POST /api/v1/admin/ai-models` | Add a catalog-selected model |
| `PATCH /api/v1/admin/ai-models/{id}` | Change priority, limits, secret binding, or enabled state |
| `DELETE /api/v1/admin/ai-models/{id}` | Remove a model row |

These endpoints require an administrator access token.
