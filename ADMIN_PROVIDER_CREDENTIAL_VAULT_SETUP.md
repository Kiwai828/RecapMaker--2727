# VoiceRecap Admin Provider Credential Vault

## Purpose

The **Provider credentials** panel lets an administrator add, rotate, disable, and remove API keys without placing the provider key in the Android APK or editing Cloudflare Worker secret bindings for each provider key. OpenRouter and OpenCode Zen are built-in provider types. A **Custom** provider supports HTTPS OpenAI-compatible APIs for text translation and JSON/base64 audio transcription.

> API keys entered in the Admin panel are encrypted with AES-GCM in the Worker before they are stored in D1. The UI and list API expose only the final four characters. Provider key plaintext is never returned after submission.

## One Required Cloudflare Secret

Set this once in Cloudflare Workers **Settings → Variables and Secrets** as a Secret:

```text
PROVIDER_CREDENTIAL_MASTER_KEY
```

Use a new random value of at least 32 characters. It encrypts all Admin-panel provider credentials. Do not change or delete it after saving credentials, or existing encrypted keys cannot be decrypted. This master key remains a Cloudflare secret; individual provider keys are managed from the Admin panel.

| Value | Storage location | Editable from Admin panel |
|---|---|---|
| `PROVIDER_CREDENTIAL_MASTER_KEY` | Cloudflare Worker Secret | No — set once in Cloudflare |
| OpenRouter API keys | Encrypted D1 credential vault | Yes |
| OpenCode Zen API keys | Encrypted D1 credential vault | Yes |
| Custom provider API keys | Encrypted D1 credential vault | Yes |

## Built-in Providers

Create an encrypted credential before adding models.

| Provider type | Credential name example | API format | Model capability |
|---|---|---|---|
| OpenRouter built-in | `OpenRouter Whisper key` | OpenAI audio transcription | STT |
| OpenCode Zen built-in | `Zen translation key 1` | OpenAI chat completions | Translation |
| Custom provider | `Company gateway key` | Choose the compatible format | STT or Translation |

For VoiceRecap's intended chain, create these model rows in **AI provider models** after fetching each live catalog:

| Provider | Model ID | Capability | Priority |
|---|---|---|---|
| OpenRouter | `openai/whisper-large-v3` | STT | 0 |
| OpenCode Zen | `deepseek-v4-flash-free` | Translation | 0 |
| OpenCode Zen | `mimo-v2.5-free` | Translation | 1 |

A user processing request is submitted only once. If the priority-0 translation model rejects the request with a retryable provider response, the backend can select the priority-1 model within that same server-side request. The Android app must not send multiple full audio requests.

## Custom Provider Requirements

Custom providers must use HTTPS. Use a base URL that ends in a version root such as `https://provider.example/v1`. The current custom adapter appends `/models`, `/chat/completions`, or `/audio/transcriptions` as applicable. The custom provider must accept an OpenAI-compatible JSON request and Bearer or configured header authentication.

For non-OpenAI-compatible providers, add a dedicated provider adapter rather than selecting Custom JSON. This keeps the video-dubbing segment schema and response validation reliable.

## Operational Safety

Rotate keys by editing a credential and entering a new API key. Disable a credential to disable every attached model immediately. Deleting a credential disables attached models and removes its encrypted ciphertext. Audit events record credential changes without recording key material.

The initial migration preserves existing legacy secret-binding model rows for compatibility. Move each model to a vault credential from the Admin panel after the migration is deployed, then remove obsolete individual provider secrets from Cloudflare if desired.
