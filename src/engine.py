import json
import os

from dotenv import load_dotenv
from openai import OpenAI
from utils import JobInput

client = OpenAI(
    base_url='http://localhost:11434/v1/',

    # required but ignored
    api_key='ollama',
)

# Sampling params the OpenAI SDK accepts as named keyword arguments. Anything
# outside this set must ride in `extra_body` instead: the SDK raises
# `TypeError: got an unexpected keyword argument 'top_k'` rather than passing an
# unknown kwarg through, which would fail the whole job over a param the caller
# merely hoped for.
#
# Caveat worth knowing: Ollama's OpenAI-compatible layer accepts unknown JSON
# fields and ignores them. `temperature`, `top_p` and `max_tokens` genuinely take
# effect; `top_k` / `repeat_penalty` reach Ollama via extra_body but are NOT
# implemented on this route, so they are a silent no-op rather than an error.
# Context size is likewise not settable per request — it comes from the
# server-wide OLLAMA_CONTEXT_LENGTH env var (see the Dockerfile).
_OPENAI_NAMED_PARAMS = frozenset({
    "max_tokens", "max_completion_tokens", "temperature", "top_p", "seed",
    "stop", "n", "frequency_penalty", "presence_penalty", "logit_bias",
    "logprobs", "top_logprobs", "response_format", "user",
})


def _apply_sampling(openai_input, sampling_params):
    """Merge caller sampling params into an OpenAI request body.

    Known params go in as named fields, the rest into `extra_body` so the SDK
    forwards them without choking. Never overwrites a value the caller already
    set explicitly in `openai_input`.
    """
    if not sampling_params:
        return openai_input
    extra = dict(openai_input.get("extra_body") or {})
    for key, value in sampling_params.items():
        if key in openai_input:
            continue
        if key in _OPENAI_NAMED_PARAMS:
            openai_input[key] = value
        else:
            extra.setdefault(key, value)
    if extra:
        openai_input["extra_body"] = extra
    return openai_input


class OllamaEngine:
    def __init__(self):
        load_dotenv()
        print ("OllamaEngine initialized")

    async def generate(self, job_input):
        # The model is baked into the image and pinned via OLLAMA_MODEL_NAME (see Dockerfile),
        # so this should always be set. If it isn't, the image was built/configured wrong —
        # surface a clear error instead of silently serving a default that isn't installed.
        model = os.getenv("OLLAMA_MODEL_NAME")
        if not model:
            yield {"error": "OLLAMA_MODEL_NAME is not set; it is baked into the image at build time."}
            return

        # Depending if prompt is a string or a list, we need to handle it differently and send it to the OpenAI API.
        # `_apply_sampling` carries the caller's max_tokens / temperature / top_p
        # across; without it this rebuild silently discarded every sampling param
        # the caller sent, so requests asking for deterministic output got the
        # model default instead and generation ran with no token ceiling.
        if isinstance(job_input.llm_input, str):
            # Buid new JobInput object with the OpenAI route and input
            openAiJob = JobInput({
                "openai_route": "/v1/completions",
                "openai_input": _apply_sampling({
                    "model": model,
                    "prompt": job_input.llm_input,
                    "stream": job_input.stream
                }, job_input.sampling_params)
            })
        else:
            # Buid new JobInput object with the OpenAI route and input
            openAiJob = JobInput({
                "openai_route": "/v1/chat/completions",
                "openai_input": _apply_sampling({
                    "model": model,
                    "messages": job_input.llm_input,
                    "stream": job_input.stream
                }, job_input.sampling_params)
            })

        print ("Generating response for job_input:", job_input)
        print ("OpenAI job:", openAiJob)
        
        # Create a generator that will yield the response from the OpenAI API
        openAIEngine = OllamaOpenAiEngine()
        generate = openAIEngine.generate(openAiJob)

        # Yield the response from the OpenAI API
        async for batch in generate:
            yield batch

class OllamaOpenAiEngine(OllamaEngine):
    def __init__(self):
        load_dotenv()
        print ("OllamaOpenAiEngine initialized")

    async def generate(self, job_input):
        print("Generating response for job_input:", job_input)

        # Dump job_input to console
        openai_input = job_input.openai_input

        # for now e just mock the response
        if job_input.openai_route == "/v1/models":
            # Async response
            async for response in self._handle_model_request():
                yield response
        elif job_input.openai_route in ["/v1/chat/completions", "/v1/completions"]:
            async for response in self._handle_chat_or_completion_request(openai_input, chat=job_input.openai_route == "/v1/chat/completions"):
                yield response
        elif job_input.openai_route == "/v1/embeddings":
            async for response in self._handle_embeddings_request(openai_input):
                yield response
        else:
            yield {"error": "Invalid route"}

    async def _handle_model_request(self):
        try:
            response = client.models.list()
            # build a json response from the response object
            # SyncPage[Model](data=[Model(id='llama3.2:1b', created=1737206544, object='model', owned_by='library')], object='list')\n
            yield {"object": "list", "data": [model.to_dict() for model in response.data]} 
        except Exception as e:
            yield {"error": str(e)}

    async def _handle_embeddings_request(self, openai_input):
        try:
            # Embeddings are non-streaming; the caller supplies "model" and "input"
            # in openai_input, e.g. {"model": "nomic-embed-text", "input": "hello"}
            response = client.embeddings.create(**openai_input)
            yield response.to_dict()
        except Exception as e:
            yield {"error": str(e)}

    async def _handle_chat_or_completion_request(self, openai_input, chat=False):
        try:
            # Call openai.chat.completions.create or openai.completions.create based on the route
            if chat:
                response = client.chat.completions.create(**openai_input)
            else:
                response = client.completions.create(**openai_input)

            # If streaming is False, we can just return the response
            if not openai_input.get("stream", False):
                yield response.to_dict()
                return

            for chunk in response:
                # Log message to console
                print("Message:", chunk)
                # Return json of the chunk without any line breaks
                yield "data: " + json.dumps(chunk.to_dict(), separators=(',', ':')) + "\n\n"

            yield "data: [DONE]"
        except Exception as e:
            yield {"error": str(e)}