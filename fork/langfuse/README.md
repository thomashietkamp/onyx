# Langfuse for Onyx

Self-hosted [Langfuse](https://langfuse.com) is the tracing backend of this
fork. Onyx sends a trace for each LLM call (prompt, answer, model, tokens and
cost) to it. It is the only tracing provider the fork allows
(`TRACING_PROVIDER_ALLOWLIST=langfuse`).

## Files

| File | What it is |
|---|---|
| `docker-compose.langfuse.yml` | Langfuse web and worker, with its own Postgres, ClickHouse, Redis and MinIO. A separate Compose project (`onyx-langfuse`). |
| `env.template` | Copy to `.env` (git ignores it) and fill in. |

## Set up

1. Make `.env` from the template and fill every value. The template shows
   the commands that make random values. `LANGFUSE_PUBLIC_KEY` and
   `LANGFUSE_SECRET_KEY` are the project keys Onyx uses.

2. Start Langfuse:

   ```bash
   cd fork/langfuse
   docker compose -f docker-compose.langfuse.yml up -d
   ```

   The first start creates the organization and project "Onyx" with your
   keys, and the admin login from `.env`. Sign-up is off.

3. Give Onyx the same keys, in the env file of the API server and the
   workers (`.vscode/.env` for the dev stack):

   ```bash
   LANGFUSE_PUBLIC_KEY=<from fork/langfuse/.env>
   LANGFUSE_SECRET_KEY=<from fork/langfuse/.env>
   LANGFUSE_HOST=http://localhost:3110
   TRACING_PROVIDER_ALLOWLIST=langfuse
   ```

   Restart Onyx. Admin > Tracing then shows Langfuse as connected (from the
   environment) and no other provider.

4. Open Langfuse at `http://localhost:3110` and sign in with the admin login.

## Notes

- Every port is bound to `127.0.0.1`. To open the UI from another device,
  serve port 3110 on your tailnet (`tailscale serve --bg --https=3111
  http://127.0.0.1:3110`) and set `LANGFUSE_PUBLIC_URL` to that address.
- When Onyx runs in Docker, `localhost` is the container itself: set
  `LANGFUSE_HOST` to an address the containers can reach.
- Onyx sends Langfuse v4 events. Find them under Traces in the UI, or with
  `GET /api/public/v2/observations`; the v1 `GET /api/public/traces` lists
  none.
- Langfuse needs about 2 to 3 GB of memory.
- Images are pinned (Langfuse 4.76.0). Langfuse telemetry is off.
