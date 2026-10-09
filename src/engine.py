import asyncio
import json
import os
import urllib.request

from dotenv import load_dotenv
from openai import AsyncOpenAI
from utils import JobInput

# Async on purpose. The handler runs up to MAX_CONCURRENCY jobs on ONE event loop, and before 0.4.0 this was the
# sync `OpenAI` client: every Ollama call blocked the loop, so a 119-token embed picked up beside an 8 s generation
# sat out the whole 8 s — and RunPod billed and reported that wait as the embed's executionTime. Nothing in this
# file may block the loop; the native routes go through `_post_native` (urllib in a thread).
client = AsyncOpenAI(
    base_url='http://localhost:11434/v1/',

    # required but ignored
    api_key='ollama',
)

OLLAMA_NATIVE_URL = os.getenv("OLLAMA_NATIVE_URL", "http://localhost:11434")

# Ollama's own timings (nanoseconds), forwarded in `usage` so a caller can split a job's executionTime into model
# load / prompt eval (prefill) / eval (decode), and the rest into waiting. Only the native routes report them —
# the OpenAI-compatible routes return token counts and nothing else.
_TIMING_FIELDS = ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")


def _timings(d):
    return {k: d[k] for k in _TIMING_FIELDS if d.get(k) is not None}


async def _post_native(path, body):
    """POST to Ollama's native API without blocking the event loop. Raises on an HTTP or connection error."""
    def call():
        req = urllib.request.Request(f"{OLLAMA_NATIVE_URL}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())
    return await asyncio.to_thread(call)


def _completion_shape(model, d):
    """A native /api/generate answer in the /v1/completions shape callers read, plus Ollama's timings."""
    return {"object": "text_completion", "model": model, "native": True,
            "choices": [{"index": 0, "text": d.get("response", ""),
                         "finish_reason": "length" if d.get("done_reason") == "length" else "stop"}],
            "usage": {"prompt_tokens": d.get("prompt_eval_count"), "completion_tokens": d.get("eval_count"),
                      **_timings(d)}}


# A plain {"prompt": ...} job is served on the native /api/generate (for the timings) with the options Ollama itself
# builds from a /v1/completions request, so the output is what the OpenAI route would have given:
# - unset temperature / top_p are OpenAI's 1.0, not the model's defaults; unset penalties are 0;
# - top_k and repeat_penalty are DROPPED — the OpenAI route ignores them, and honouring them now would quietly
#   change the sampling of every caller that sends them (Alsion's conservative and diverse sets do);
# - a job carrying any other param (logprobs, suffix, n, …) keeps the OpenAI route.
# OLLAMA_NATIVE_COMPLETIONS=0 puts every plain prompt back on the OpenAI route.
_COMPAT_OPTIONS = {"max_tokens": "num_predict", "max_completion_tokens": "num_predict",
                   "temperature": "temperature", "top_p": "top_p", "seed": "seed", "stop": "stop",
                   "frequency_penalty": "frequency_penalty", "presence_penalty": "presence_penalty"}
_COMPAT_IGNORED = frozenset({"top_k", "repeat_penalty"})
_COMPAT_DEFAULTS = {"temperature": 1.0, "top_p": 1.0, "frequency_penalty": 0.0, "presence_penalty": 0.0}


def _compat_options(params):
    """Ollama options equivalent to a /v1/completions request with `params`, or None if a param has no
    equivalent (the job then keeps the OpenAI route)."""
    if any(k not in _COMPAT_OPTIONS and k not in _COMPAT_IGNORED for k in params):
        return None
    options = dict(_COMPAT_DEFAULTS)
    for key, value in params.items():
        if key in _COMPAT_OPTIONS:
            options[_COMPAT_OPTIONS[key]] = value
    if isinstance(options.get("stop"), str):
        options["stop"] = [options["stop"]]
    return options


def _native_completions_enabled():
    return os.getenv("OLLAMA_NATIVE_COMPLETIONS", "1").strip().lower() not in ("0", "false", "no", "off")

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

        # A string prompt with an output schema: Ollama's native /api/generate, which constrains decoding to the
        # schema. The OpenAI-compatible route below accepts `response_format` on /v1/completions and ignores it.
        if isinstance(job_input.llm_input, str) and job_input.format is not None:
            yield await self._native_generate(model, job_input)
            return

        # A plain non-streaming prompt: the native route with OpenAI-equivalent options, for Ollama's timings.
        if isinstance(job_input.llm_input, str) and not job_input.stream and _native_completions_enabled():
            options = _compat_options(job_input.sampling_params or {})
            if options is not None:
                yield await self._native_compat_generate(model, job_input.llm_input, options)
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

    # Sampling params → Ollama `options` names on the native route (it implements top_k / repeat_penalty too).
    _NATIVE_OPTIONS = {"temperature": "temperature", "top_p": "top_p", "top_k": "top_k", "seed": "seed",
                       "stop": "stop", "max_tokens": "num_predict", "repeat_penalty": "repeat_penalty",
                       "frequency_penalty": "frequency_penalty", "presence_penalty": "presence_penalty"}

    async def _native_generate(self, model, job_input):
        """One non-streaming /api/generate call with `format`, answered in the /v1/completions shape callers
        already read: {"choices": [{"text", "finish_reason"}], "usage": {prompt_tokens, completion_tokens, …}}."""
        params = job_input.sampling_params or {}
        options = {self._NATIVE_OPTIONS[k]: v for k, v in params.items() if k in self._NATIVE_OPTIONS}
        body = {"model": model, "prompt": job_input.llm_input, "stream": False, "format": job_input.format,
                "options": options}
        try:
            d = await _post_native("/api/generate", body)
        except Exception as e:
            return {"error": str(e)}
        if "error" in d:
            return {"error": d["error"]}
        return _completion_shape(model, d)

    async def _native_compat_generate(self, model, prompt, options):
        """A plain prompt on /api/generate with the options /v1/completions would have used (see _compat_options)."""
        try:
            d = await _post_native("/api/generate", {"model": model, "prompt": prompt, "stream": False,
                                                     "options": options})
        except Exception as e:
            return {"error": str(e)}
        if "error" in d:
            return {"error": d["error"]}
        return _completion_shape(model, d)


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
            response = await client.models.list()
            # build a json response from the response object
            # SyncPage[Model](data=[Model(id='llama3.2:1b', created=1737206544, object='model', owned_by='library')], object='list')\n
            yield {"object": "list", "data": [model.to_dict() for model in response.data]} 
        except Exception as e:
            yield {"error": str(e)}

    # /v1/embeddings fields the native /api/embed takes as-is (Ollama's OpenAI layer converts to the same request).
    _NATIVE_EMBED_KEYS = frozenset({"model", "input", "dimensions", "encoding_format", "user"})

    async def _handle_embeddings_request(self, openai_input):
        # Embeddings are non-streaming; the caller supplies "model" and "input"
        # in openai_input, e.g. {"model": "nomic-embed-text", "input": "hello"}.
        # Served on the native /api/embed — same vectors, plus load_duration / total_duration, which say whether
        # this embed had to load its model. A request the native route can't express keeps the OpenAI route.
        try:
            if (set(openai_input) <= self._NATIVE_EMBED_KEYS
                    and openai_input.get("encoding_format", "float") == "float"):
                body = {"model": openai_input["model"], "input": openai_input["input"]}
                if openai_input.get("dimensions"):
                    body["dimensions"] = openai_input["dimensions"]
                d = await _post_native("/api/embed", body)
                if "error" in d:
                    yield {"error": d["error"]}
                    return
                n = d.get("prompt_eval_count")
                yield {"object": "list", "model": openai_input["model"], "native": True,
                       "data": [{"object": "embedding", "index": i, "embedding": e}
                                for i, e in enumerate(d.get("embeddings") or [])],
                       "usage": {"prompt_tokens": n, "total_tokens": n, **_timings(d)}}
                return
            response = await client.embeddings.create(**openai_input)
            yield response.to_dict()
        except Exception as e:
            yield {"error": str(e)}

    async def _handle_chat_or_completion_request(self, openai_input, chat=False):
        try:
            # Call openai.chat.completions.create or openai.completions.create based on the route
            if chat:
                response = await client.chat.completions.create(**openai_input)
            else:
                response = await client.completions.create(**openai_input)

            # If streaming is False, we can just return the response
            if not openai_input.get("stream", False):
                yield response.to_dict()
                return

            async for chunk in response:
                # Log message to console
                print("Message:", chunk)
                # Return json of the chunk without any line breaks
                yield "data: " + json.dumps(chunk.to_dict(), separators=(',', ':')) + "\n\n"

            yield "data: [DONE]"
        except Exception as e:
            yield {"error": str(e)}