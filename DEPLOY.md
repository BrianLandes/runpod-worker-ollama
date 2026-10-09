# Redeploy Guide

Step-by-step for rebuilding this worker and getting it onto RunPod after making code
changes. Written to be followed cold — no prior context assumed.

The image is published to Docker Hub as **`brianlandes/runpod-worker-ollama`** and RunPod
pulls it from there. The flow is: **edit → build → push to Docker Hub → point RunPod at the
new tag**.

---

## 0. One-time prerequisites (only if setting up a fresh machine)

- **Docker** with buildx (`docker buildx version` should print a version).
- **Logged into Docker Hub:** `docker login` (user `brianlandes`). Check with
  `docker system info | grep Username`.
- A **RunPod account** with an existing Serverless endpoint for this worker, plus a
  **RunPod API key** (RunPod console → Settings → API Keys) for testing.

---

## 1. Make your code changes

Edit whatever you need under [`src/`](./src) (handler/engine/startup) or config. Common spots:

- [`src/engine.py`](./src/engine.py) — request routing & how each model is called
  (chat, completion, embeddings).
- [`src/start.sh`](./src/start.sh) — what runs at container start (serve Ollama, then the
  handler). Models are **baked into the image at build time**, not pulled here.
- [`.runpod/hub.json`](./.runpod/hub.json) — the env-var fields shown in the RunPod console.
- [`Dockerfile`](./Dockerfile) — base image / Ollama version (`ARG OLLAMA_VERSION`) and the
  **baked models** (`ARG TEXT_MODEL` / `ARG EMBED_MODEL`). Changing models = rebuild.

---

## 2. Pick a new image tag

**Do not reuse the same tag.** RunPod (and Docker) cache by tag, so pushing over an existing
tag often won't roll out reliably. Bump the version each rebuild.

Tags so far: `0.1.0`, `0.2.0`, `0.2.1` (live since 2026-08-21), `0.3.0` (structured output, 2026-10-06), **`0.4.0`**
(non-blocking jobs, Ollama timings, both models resident, 2026-10-09 — see below). Pick the next number after the last one you pushed (`docker images brianlandes/runpod-worker-ollama` lists
what you've built).

```bash
# set this once per rebuild — used by the commands below
export TAG=0.4.0
```

---

## 3. Build and push to Docker Hub

Run from the **repo root** (`/home/brian/Projects/runpod-worker-ollama`).

> ⚠️ **Platform matters.** RunPod GPU workers are **linux/amd64**. Always pass
> `--platform linux/amd64`, even if your laptop is ARM — otherwise the image won't run there.

```bash
docker buildx build \
  --platform linux/amd64 \
  -t brianlandes/runpod-worker-ollama:$TAG \
  --push \
  .
```

The `--push` flag builds and uploads in one step. This build also **pulls both models
(~15GB) into the image** (Cydonia-24B Q4 ≈ 14GB + nomic-embed ≈ 0.3GB), so it needs ~20GB
free disk, takes longer than a code-only build, and produces a larger push. Wait for it to
finish with `DONE` / no error.

### Verify it landed on Docker Hub

```bash
docker buildx imagetools inspect brianlandes/runpod-worker-ollama:$TAG
```

You should see `Platform: linux/amd64` in the output. Web page (browser):
<https://hub.docker.com/r/brianlandes/runpod-worker-ollama/tags>

> **Optional — also move `:latest`:** if you want a moving pointer, tag both:
> add `-t brianlandes/runpod-worker-ollama:latest` to the build command above. Prefer
> deploying RunPod against the **explicit version tag** regardless, so rollouts are predictable.

---

## 4. Point RunPod at the new image

RunPod does **not** auto-pull a new build by itself — you have to tell the endpoint to use the
new tag.

1. RunPod console → **Serverless** → open your endpoint.
2. **Edit Endpoint** (or "Manage" → edit the template).
3. Set **Container Image** to the new tag:
   ```
   brianlandes/runpod-worker-ollama:0.1.1
   ```
   (use the `$TAG` you built). The image field is a plain reference, **not** a URL.
4. Confirm the **environment variables** (see next section) — and **remove** the now-dead
   `OLLAMA_MODEL_NAME` / `OLLAMA_EXTRA_MODELS` vars if they linger from a prior deploy.
5. **Save / Update.** RunPod rolls out new workers pulling the new image. Old in-flight
   workers drain; the first request to a fresh worker is a **cold start** (slower — it pulls
   the models before accepting jobs).

> If the Docker Hub repo is **private**, RunPod needs Docker Hub credentials added under its
> registry/container settings, or the pull fails.

---

## 5. Environment variables (set on the RunPod endpoint)

The model env vars are **gone** — both models are baked into the image at build time, and the
default generation model is pinned in the image via `ENV OLLAMA_MODEL_NAME=${TEXT_MODEL}`
([Dockerfile](./Dockerfile)). These remain, all optional:

| Variable                | Purpose                                                    | Example |
|-------------------------|------------------------------------------------------------|---------|
| `OLLAMA_CONTEXT_LENGTH` | Context window in tokens (image default **8192**)          | `8192`  |
| `OLLAMA_NUM_PARALLEL`   | Ollama slots served concurrently (image default **1**)     | `1`     |
| `MAX_CONCURRENCY`       | Max concurrent requests per worker (default 8)             | `8`     |

### About `OLLAMA_CONTEXT_LENGTH` — read before changing it

Ollama's own default is **4096**, and a prompt longer than the window is **not rejected**:
Ollama keeps the **tail** and silently discards the front. The model then answers confidently
having never seen the opening of its own prompt. If a caller puts its payload at the top of a
long prompt — the story text, the source document, the data to extract from — that payload is
exactly what disappears, and the only symptom is quietly wrong answers.

This is *not* settable per request on the OpenAI-compatible routes this worker serves.
Ollama's `/v1/completions` accepts unknown JSON fields and ignores them, so a caller-supplied
`num_ctx` or `options` block is a silent no-op. **This env var is the only knob.**

VRAM cost — the KV cache is preallocated at model load, so you pay it whether or not prompts
are actually that long. For the baked Cydonia-24B (~13.9 GiB weights, ~160 KiB/token):

| `OLLAMA_CONTEXT_LENGTH` | KV cache | Total VRAM | Fits 16 GB? | Fits 24 GB? |
|---|---|---|---|---|
| 4096      | 0.6 GiB      | 14.5 GiB     | marginal | yes |
| **8192**  | **1.25 GiB** | **15.2 GiB** | **no**   | **yes** |
| 16384     | 2.5 GiB      | 16.4 GiB     | no       | yes |
| 32768     | 5.0 GiB      | 18.9 GiB     | no       | yes |

> ⚠️ **These figures are multiplied by `OLLAMA_NUM_PARALLEL`.** Ollama picks the parallelism
> itself when unset (up to 4), so leaving it blank silently quadruples the KV cache —
> 8192 × 4 = 5 GiB — which is the usual reason raising the context turns into an
> out-of-memory on cold start. The image pins it to `1`; raise it only if the GPU is sized for it.

> ⚠️ **GPU tier.** [`hub.json`](./.runpod/hub.json) no longer offers `AMPERE_16`: at 8192 the
> model needs ~15.2 GiB and a 16 GB card cannot hold it without spilling layers to CPU, which is
> drastically slower rather than a clean failure. Keep the endpoint on **24 GB** GPUs
> (`AMPERE_24`, `ADA_24`). To run on 16 GB, drop the context back to 4096 — and accept that
> prompts over 4096 lose their front.

### Sampling parameters (worker ≥ 0.1.1)

The worker now forwards `max_tokens`, `temperature`, `top_p`, `seed`, `stop` and friends from
the job input through to Ollama. **Before 0.1.1 it discarded all of them**, keeping only
`prompt` — so callers asking for `temperature: 0.1` silently got the model default, and
`max_tokens` never applied (measured: 1,908 generated tokens against a requested cap of 12).

`top_k` and `repeat_penalty` are forwarded via `extra_body` but Ollama's OpenAI-compat layer
does not implement them — accepted and ignored, not an error. Use `temperature` / `top_p`.

### Structured output — a JSON schema (worker ≥ 0.3.0)

A `{"prompt": …}` job may carry `"format": <JSON schema>` (or `"json"`). The worker then calls Ollama's **native**
`/api/generate` with that `format`, which constrains decoding to the schema, and answers in the usual
`/v1/completions` shape (`choices[0].text`, `usage`, plus `"native": true`). Sampling params map to Ollama options
(`max_tokens` → `num_predict`; `top_k` / `repeat_penalty` DO take effect on this route). Jobs without `format` go
through the OpenAI-compatible route exactly as before. Why: `/v1/completions` accepts a `response_format` and
silently ignores it. The game (Alsion) sends `format` only when `ALSION_RUNPOD_SCHEMA=1`.

### Concurrency, timings and resident models (worker ≥ 0.4.0)

- **Jobs no longer block each other.** Before 0.4.0 the handler called Ollama with the *synchronous* OpenAI client
  inside its async handler, so with `MAX_CONCURRENCY=8` a worker ran its jobs one at a time at the Python level: an
  embed picked up beside an 8 s generation waited the whole 8 s, and RunPod billed and reported that wait as the
  embed's `executionTime`. (Reproduced against a fake Ollama: old engine, embed beside a 2 s generation = 2.25 s;
  0.4.0 = 0.00 s.) Ollama still serves one text generation at a time (`OLLAMA_NUM_PARALLEL=1`), but embeds run on
  their own runner and no longer queue behind it.
- **Ollama's timings come back in `usage`** — `total_duration`, `load_duration`, `prompt_eval_duration`,
  `eval_duration` (nanoseconds, Ollama's names). Only Ollama's native routes report them, so:
  - a plain non-streaming `{"prompt": …}` job is served on `/api/generate` with the options Ollama itself builds
    from `/v1/completions` (unset temperature / top_p = 1.0, penalties 0; `top_k` / `repeat_penalty` **dropped**,
    as the OpenAI route ignores them). Any other param (`logprobs`, `n`, …) keeps the OpenAI route.
    `OLLAMA_NATIVE_COMPLETIONS=0` on the endpoint puts every plain prompt back on the OpenAI route.
  - `/v1/embeddings` is served on `/api/embed` (same vectors) and answered in the OpenAI shape.
  - chat (`messages`) and streaming jobs are unchanged and report no timings.
  A job's `executionTime − total_duration` is time it spent waiting on the worker; `load_duration` > ~0.1 s on an
  embed means it paid a model load.
- **Both models stay resident:** `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_KEEP_ALIVE=-1`, and `start.sh` loads both
  before the handler takes a job, then logs `ollama ps` + `nvidia-smi`. Check a fresh worker's log: both models
  listed, `100% GPU`. If one is missing, the endpoint landed on a GPU too small for both — pin it to 24 GB classes.
- `OLLAMA_FLASH_ATTENTION=1` is pinned (recent Ollama already runs with `--flash-attn auto`).

> **Remove** any `OLLAMA_MODEL_NAME` / `OLLAMA_EXTRA_MODELS` left over from an older deploy —
> they no longer do anything at runtime and just mislead.

> **Do NOT attach a network volume.** Models live in the image's read-only layers and
> `OLLAMA_MODELS` points at an in-image path (`/root/.ollama`), not `/runpod-volume`. A volume
> would be unused cost and pin the endpoint to one datacenter — bake **or** volume, not both.

> **Cold-start settings (do this once on the endpoint):** set **min workers = 0**, confirm
> **FlashBoot** is on, and raise **idle timeout** to ~60–120s. With models baked, cold workers
> load from local disk instead of re-downloading ~15GB, so a 24/7 active worker is no longer needed.

---

## 6. Test the deployed endpoint

Replace `<ENDPOINT_ID>` (from the endpoint page) and `<RUNPOD_API_KEY>`. Use `/runsync` for a
blocking call.

**Generation (chat):**
```bash
curl -s https://api.runpod.ai/v2/<ENDPOINT_ID>/runsync \
  -H "Authorization: Bearer <RUNPOD_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"input":{"openai_route":"/v1/chat/completions","openai_input":{"model":"hf.co/TheDrummer/Cydonia-24B-v4.3-GGUF:Q4_K_M","messages":[{"role":"user","content":"How are you?"}]}}}'
```

**Structured output (≥ 0.3.0)** — expect `"text": "{\"body\": \"zq-alpha\"}"` (or zq-beta) and `"native": true`;
an older image answers free-form instead:
```bash
curl -s https://api.runpod.ai/v2/<ENDPOINT_ID>/runsync \
  -H "Authorization: Bearer <RUNPOD_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"input":{"prompt":"Isla is lying on her back. Describe her body in one word as JSON {\"body\": ...}.","max_tokens":50,"temperature":0.1,"format":{"type":"object","properties":{"body":{"type":"string","enum":["zq-alpha","zq-beta"]}},"required":["body"]}}}'
```

**Embeddings:**
```bash
curl -s https://api.runpod.ai/v2/<ENDPOINT_ID>/runsync \
  -H "Authorization: Bearer <RUNPOD_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"input":{"openai_route":"/v1/embeddings","openai_input":{"model":"nomic-embed-text","input":"hello world"}}}'
```

Example request bodies also live in [`test_inputs/`](./test_inputs).

---

## 7. Quick troubleshooting

| Symptom                                   | Likely cause / fix                                                            |
|-------------------------------------------|------------------------------------------------------------------------------|
| RunPod still runs old code                | Reused the same tag, or endpoint not pointed at new tag. Bump `$TAG`, re-edit endpoint. |
| `exec format error` in worker logs        | Image built for wrong arch. Rebuild with `--platform linux/amd64`.           |
| Pull fails / unauthorized on RunPod       | Private Docker Hub repo without creds in RunPod, or wrong image name.         |
| Embeddings return `Invalid route`         | Old image without the `/v1/embeddings` handler — rebuild & redeploy.         |
| Embeddings/model error "does not support" | Embedding model used for generation (or vice-versa). Match `model` to route. |
| Very slow first request                   | Cold start = image pull on a fresh host (models are baked in, ~15GB). Cached per host after the first pull; don't attach a volume. |

---

## TL;DR

```bash
export TAG=0.1.1                       # bump from last build
docker buildx build --platform linux/amd64 \
  -t brianlandes/runpod-worker-ollama:$TAG --push .
docker buildx imagetools inspect brianlandes/runpod-worker-ollama:$TAG   # verify amd64
# RunPod console → endpoint → Container Image = brianlandes/runpod-worker-ollama:0.1.1 → Save
```
