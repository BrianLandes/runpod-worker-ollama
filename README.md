# Runpod serverless runner for ollama

## How to use

Start a runpod serverless with the docker container ``svenbrnn/runpod-ollama:latest``. Set ``OLLAMA_MODEL_NAME`` environment to a model from ollama.com to automatically download a model.
A mounted volume will be automatically used.

[![RunPod](https://api.runpod.io/badge/SvenBrnn/runpod-worker-ollama)](https://www.runpod.io/console/hub/SvenBrnn/runpod-worker-ollama)

## Environment variables

| Variable Name         | Description                                                                                | Default Value |
|-----------------------|--------------------------------------------------------------------------------------------|---------------|
| `OLLAMA_MODEL_NAME`   | The name of the model to download                                                          | NULL          |
| `OLLAMA_EXTRA_MODELS` | Comma-separated list of additional models to preload (e.g. `nomic-embed-text,all-minilm`)  | NULL          |

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