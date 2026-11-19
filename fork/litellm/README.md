# LiteLLM Proxy for Onyx

One model gateway in front of Requesty, Azure OpenAI and self-hosted models.
Onyx connects to it as the **LiteLLM Proxy** provider and reads its model
list. To add a model later, change `config.yaml` only; Onyx needs no change.

```
Onyx ──► LiteLLM Proxy (http://litellm:4000) ──► Requesty (Claude and other models)
                                             ├──► Azure OpenAI
                                             └──► self-hosted (vLLM, Ollama, ...)
```

The proxy keeps the provider keys, so Onyx stores only the proxy key. The
proxy also gives rate limits, fallbacks and a cost overview per key.

## Files

| File | What it is |
|---|---|
| `docker-compose.litellm.yml` | The `litellm` service and its own Postgres (`litellm_db`). |
| `config.yaml` | The models the proxy serves. Keys come from `.env`. |
| `env.template` | Copy to `.env` (git ignores it) and fill in. |

## Set up

1. Copy the env file and fill in the values:

   ```bash
   cp fork/litellm/env.template fork/litellm/.env
   ```

   - Set `LITELLM_MASTER_KEY` to a random value that starts with `sk-`.
     Set `LITELLM_SALT_KEY` to a long random string. Do not change the salt
     key after the first start.
   - **Azure OpenAI:** set the resource URL, the key, the API version and
     `AZURE_OPENAI_GPT4O_MODEL=azure/<deployment name>`.
   - **Requesty:** set `REQUESTY_API_KEY`. `REQUESTY_CLAUDE_SONNET_MODEL` and
     `REQUESTY_CLAUDE_OPUS_MODEL` are `openai/<Requesty model id>`. The
     defaults use Bedrock in eu-central-1, so requests stay in the EU. To see
     every id, run `curl https://router.requesty.ai/v1/models`.
   - **Optional, Claude on Azure AI Foundry:** uncomment its block in
     `config.yaml` and set `AZURE_CLAUDE_API_BASE`, the key and
     `AZURE_CLAUDE_MODEL=azure_ai/<deployment name>`.

2. In `config.yaml`, remove the model blocks you do not use. Uncomment the
   Anthropic or the self-hosted block if you need it. Onyx lists every
   model in the file, and a model without a working key fails when someone
   picks it.

3. Start the proxy in the same Compose project as Onyx. Run from
   `deployment/docker_compose`, because the paths in the file are relative
   to it:

   ```bash
   cd deployment/docker_compose
   docker compose -f docker-compose.yml -f ../../fork/litellm/docker-compose.litellm.yml up -d
   ```

   Add the same `-f` flags you use for the rest of the stack, for example
   `-f docker-compose.prod.yml`.

4. Check the proxy:

   ```bash
   curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" http://127.0.0.1:4000/v1/models
   ```

5. In Onyx, open **Admin > Language Models**. Under **Gateways & Routers**,
   click **Connect** on **LiteLLM Proxy**.
   - API Base URL: `http://litellm:4000`. Use `http://localhost:4000` when the
     Onyx API server runs on the host instead of in Docker.
   - API Key: the master key, or a virtual key that you make in the proxy.

   Click **Refresh models**, pick the models, and click **Connect**.

## Add a Requesty model

Copy a Requesty block in `config.yaml` and give it a new `model_name`. Set
`model: openai/<Requesty model id>`, for example
`openai/vertex/gemini-4-neon`. LiteLLM has no Requesty provider of its own;
the `openai/` prefix sends the call to Requesty's OpenAI-compatible API.

## Add a self-hosted model

Run an OpenAI-compatible server, for example vLLM or Ollama. Then add a
block to `config.yaml`:

```yaml
  - model_name: llama-3.3-70b
    litellm_params:
      model: openai/meta-llama/Llama-3.3-70B-Instruct
      api_base: http://vllm:8000/v1
      api_key: none
    model_info:
      max_input_tokens: 128000
```

Restart the proxy (`docker compose ... restart litellm`), then click
**Refresh models** in Onyx. The server must be reachable from the `litellm`
container: put it in the same Compose project, or use its network address.

## Notes

- The proxy port is bound to `127.0.0.1` only. Onyx reaches it over the
  Compose network.
- `turn_off_message_logging` keeps prompts and answers out of the proxy logs.
- The image is pinned (`ghcr.io/berriai/litellm-database:v1.106.6`). To
  update, change the tag, then run `docker compose ... pull litellm`.
- Not tested with real Azure or Requesty keys. Tested:
  - the proxy starts and its key check works;
  - Onyx reads the model list;
  - a Requesty call reaches Requesty, which refused a test key;
  - Onyx gets an answer through the proxy from a self-hosted
    (OpenAI-compatible) model, streaming and not.
