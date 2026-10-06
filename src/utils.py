class JobInput:
    """One serverless job's input, normalised.

    Beyond the four fields the worker routes on, a simple ``{"prompt": ...}`` job
    may carry sampling parameters (``max_tokens``, ``temperature``, ``top_p``, …).
    These used to be read by nothing and dropped on the floor, so a caller asking
    for ``temperature: 0.1`` silently got the model default — every "conservative"
    extraction pass ran hot, and the caller's ``max_tokens`` ceiling never applied,
    leaving generation uncapped. Collect them here so ``engine`` can forward them.
    """

    # Consumed by the worker itself; everything else is for the model.
    _RESERVED = frozenset({"messages", "prompt", "stream",
                           "openai_route", "openai_input", "format"})

    def __init__(self, job):
        self.llm_input = job.get("messages", job.get("prompt"))
        self.stream = job.get("stream", False)
        self.openai_route = job.get("openai_route")
        self.openai_input = job.get("openai_input")
        # A JSON schema (or "json") the output must follow. Ollama's OpenAI-compatible /v1/completions accepts a
        # `response_format` and IGNORES it, so a job carrying `format` is served by the native /api/generate, which
        # constrains decoding to it (engine.OllamaEngine._native_generate).
        self.format = job.get("format")
        self.sampling_params = {k: v for k, v in job.items()
                                if k not in self._RESERVED and v is not None}
