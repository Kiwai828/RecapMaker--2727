# Cloudflare Git Build Fix Notes

## Failure diagnosis

The supplied build log showed Cloudflare Workers Builds executing `npx wrangler deploy` from the repository root and then failing with `Could not detect a directory containing static files`. The previous repository had `wrangler.jsonc` only under `cloudflare/`, so a root-level Wrangler invocation could not see a Worker configuration and tried static asset detection.

## Official findings

Cloudflare Workers Builds runs an optional Build command and then a Deploy command. The default deploy command is `npx wrangler deploy`; the Root directory controls where commands run. A connected repository can therefore use a root `wrangler.jsonc`, or it can set the Root directory to a nested project. Source: https://developers.cloudflare.com/workers/ci-cd/builds/configuration/

Cloudflare's Python Worker documentation says Python Workers use the `python_workers` compatibility flag and are deployed with `uv run pywrangler deploy`. Python Workers are in open beta. Source: https://developers.cloudflare.com/workers/languages/python/

Cloudflare's Workers Builds documentation says the Worker name in the dashboard must match the `name` in the Wrangler file in the selected root directory. Source: https://developers.cloudflare.com/workers/ci-cd/builds/

## Repository fix

The repository now has a root `wrangler.jsonc` with `main: cloudflare/src/entry.py`, the name `recapmaker--2727`, D1/R2/Queues bindings, and `python_workers`. The root `package.json` defines `npm run build` as `cd cloudflare && uvx --from workers-py pywrangler sync`; the dashboard Deploy command remains `npx wrangler deploy`. The root configuration was tested locally with `npm run build` followed by `npx wrangler deploy --dry-run`, which completed successfully.

## Dashboard settings

Use repository root `/` (empty Root directory), Build command `npm run build`, Deploy command `npx wrangler deploy`, and non-production deploy command `npx wrangler versions upload`. The Worker name should be `recapmaker--2727`. The root configuration omits a D1 ID so Wrangler can use automatic resource provisioning; if resources were created manually, add the real IDs/names before deploying.
