# Runpod serverless runner for ollama

## How to use

Start a runpod serverless with the docker container ``svenbrnn/runpod-ollama:latest``. Set ``OLLAMA_MODEL_NAME`` environment to a model from ollama.com to automatically download a model.
A mounted volume will be automatically used.

[![Runpod](https://api.runpod.io/badge/BrianLandes/runpod-worker-ollama)](https://console.runpod.io/hub/BrianLandes/runpod-worker-ollama)

## Environment variables

| Variable Name           | Description                                                                                | Default Value |
|-------------------------|--------------------------------------------------------------------------------------------|---------------|
| `OLLAMA_MODEL_NAME`     | The name of the model to download                                                          | NULL          |
| `OLLAMA_EXTRA_MODELS`   | Comma-separated list of additional models to preload (e.g. `nomic-embed-text,all-minilm`)  | NULL          |
| `OLLAMA_CONTEXT_LENGTH` | Context window in tokens. **Ollama's own default of 4096 silently discards the front of any longer prompt** rather than erroring, and this cannot be overridden per request on the OpenAI routes. Costs preallocated VRAM. | `8192`        |
| `OLLAMA_NUM_PARALLEL`   | Ollama slots served concurrently. KV cache = context length × this, so raising it multiplies VRAM. Pinned for predictable memory. | `1`           |
| `MAX_CONCURRENCY`       | Max concurrent requests the handler accepts                                                | `8`           |
| `OLLAMA_MAX_LOADED_MODELS` | Models resident at once. Pinned so the text and embed models never evict each other.   | `2`           |
| `OLLAMA_KEEP_ALIVE`     | How long an idle model stays loaded. `-1` = for the worker's life.                         | `-1`          |
| `OLLAMA_FLASH_ATTENTION`| Flash attention for the runners.                                                           | `1`           |
| `OLLAMA_NATIVE_COMPLETIONS` | `0` serves plain prompts on the OpenAI route again (no timings in `usage`).            | `1`           |

> This fork **bakes** the models into the image at build time (see [`Dockerfile`](./Dockerfile)),
> so `OLLAMA_MODEL_NAME` / `OLLAMA_EXTRA_MODELS` are build-time `ARG`s rather than runtime
> settings. See [DEPLOY.md](./DEPLOY.md) for the rebuild-and-roll-out flow and the VRAM tables.

## Sampling parameters

A simple `{"input": {"prompt": "...", "temperature": 0.1, "max_tokens": 512}}` job forwards its
sampling params through to Ollama. `temperature`, `top_p`, `max_tokens`, `seed` and `stop` take
effect; `top_k` and `repeat_penalty` are passed along but Ollama's OpenAI-compatible layer does
not implement them (accepted and ignored). Before **0.1.1** every one of these was dropped.

## Test requests for runpod.io console

See the [test_inputs](./test_inputs) directory for example test requests. 


## Streaming

Streaming for openai requests are fully working.

## Embeddings

To generate embeddings, preload an embedding model (e.g. set `OLLAMA_EXTRA_MODELS=nomic-embed-text`)
and send a request to the `/v1/embeddings` route:

```json
{
    "input": {
        "openai_route": "/v1/embeddings",
        "openai_input": {
            "model": "nomic-embed-text",
            "input": "The quick brown fox jumps over the lazy dog"
        }
    }
}
```

See [test_inputs/openai_embeddings.json](./test_inputs/openai_embeddings.json) for a runnable example.

## Preload model into the docker image

See the [embed_model](./embed_model/) directory for instructions.

## Licence

This project is licensed under the Creative Commons Attribution 4.0 International License. You are free to use, share, and adapt the material for any purpose, even commercially, under the following terms:

- **Attribution**: You must give appropriate credit, provide a link to the license, and indicate if changes were made. You may do so in any reasonable manner, but not in any way that suggests the licensor endorses you or your use.
- **Reference**: You must reference the original repository at [https://github.com/svenbrnn/runpod-worker-ollama](https://github.com/svenbrnn/runpod-worker-ollama).

For more details, see the [license](https://creativecommons.org/licenses/by/4.0/).