# Handoff: bake the Ollama models into the image

**Goal:** stop pulling models at runtime and **bake both models into the Docker
image** so cold-start workers load them from local disk — no re-download, no
network volume, no region lock. Then remove the config that baking makes dead.

**Models to bake (must match exactly what the calling app requests):**
- Text (completions): `hf.co/TheDrummer/Cydonia-24B-v4.3-GGUF:Q4_K_M`
- Embeddings: `nomic-embed-text`

> ⚠️ Before building, confirm these against the live RunPod endpoint's current env
> (`OLLAMA_MODEL_NAME` and `OLLAMA_EXTRA_MODELS`). The text value is whatever the
> endpoint serves today; the embed value must equal what the app sends as
> `{"model": "..."}`.

---

## Why (motivation)

This worker backs the LLM + embeddings endpoint for **Alsion** (the
`reddit_bot_project` / `allison-lewda` game). That game now ties its per-credit
price to actual RunPod cost, so serving cost and cold starts matter directly.

The problem chain:
- Cold starts were slow, so the endpoint was run with **min workers = 1** (an
  "active worker") to stay warm. Active workers bill the GPU **24/7 regardless of
  traffic** → ~**$15/day even with zero turns**. Wrong tool for bursty game traffic.
- The cheap cold-start levers are **FlashBoot** (free, on by default), a longer
  **idle timeout**, and **baking the model into the image**. A **network volume**
  also avoids re-downloads but adds network latency and **pins the endpoint to one
  datacenter**.
- **This template currently does neither well:** it sets
  `ENV OLLAMA_MODELS=/runpod-volume` and pulls models at **runtime** in
  `src/start.sh`. So with no volume attached, *every cold worker re-downloads ~15GB*
  (Cydonia-24B Q4 ≈ 14GB + nomic-embed ≈ 0.3GB); with a volume attached, you eat
  the volume's latency + region lock.

**Decision:** bake both models into the image. Cold workers then load from local
image layers (fast, volume-free). Trade-off: model choice becomes a **build-time**
decision (change models → rebuild), and the image grows ~15GB (RunPod caches it
per host, so only the first spin-up on a new host pays the image pull).

---

## Current state you'll find in this repo

- **`Dockerfile`** — `FROM ollama/ollama`, installs Python, `ADD ./src /work`,
  `ENV OLLAMA_MODELS="/runpod-volume"`, entrypoint `start.sh`. **No build-time
  model pull.**
- **`src/start.sh`** — at container start: `ollama serve`, then
  `ollama pull $OLLAMA_MODEL_NAME`, then a loop pulling each `$OLLAMA_EXTRA_MODELS`,
  then `python handler.py`.
- **`src/engine.py:22`** — `model = os.getenv("OLLAMA_MODEL_NAME", "llama3.2:1b")`.
  This is the **default model for simple `{"prompt": ...}` requests**, which is how
  the app calls completions (it does *not* send a model name). Embeddings requests
  *do* carry their own `"model"` (see `_handle_embeddings_request`).
- **`.runpod/hub.json`** — declares env inputs `OLLAMA_MODEL_NAME` (default `phi3`),
  `OLLAMA_EXTRA_MODELS`, `MAX_CONCURRENCY`, `OLLAMA_NUM_PARALLEL`.
- **`embed_model/`** — an existing multi-stage "bake into a volume image" example
  (pulls into `/runpod-volume` at build, `COPY --from=0`). Reference only; the plan
  below bakes into an in-image path instead. You can delete `embed_model/` after.

### Dead-code analysis (the reason for the cleanup)
- **`OLLAMA_EXTRA_MODELS` → fully dead after baking.** Its only consumer is the
  `start.sh` pull; no Python reads it (embeddings name their model per-request).
  **Remove it.**
- **`OLLAMA_MODEL_NAME` → NOT dead.** `engine.py` uses it as the default text model.
  Keep it, but its value must equal the baked text model — so **drive it from the
  build ARG** (`ENV OLLAMA_MODEL_NAME=${TEXT_MODEL}`) so it can't drift.

---

## Changes to make

### 1. `Dockerfile` — bake at build, in-image model dir, pin the default

Replace the `ENV OLLAMA_MODELS="/runpod-volume"` line with the block below (keep
everything else — base image, Python install, `ADD ./src /work`, pip install,
entrypoint — as-is):

```dockerfile
# --- Bake models into the image (was: pulled at runtime onto a network volume) ---
# Models now ship in the image and load from local disk on every cold start:
# no re-download, no network volume, no region lock. Change models = rebuild.
ARG TEXT_MODEL=hf.co/TheDrummer/Cydonia-24B-v4.3-GGUF:Q4_K_M
ARG EMBED_MODEL=nomic-embed-text

# In-image model dir — deliberately NOT /runpod-volume, so a stray volume mount
# can't overmount and hide the baked models.
ENV OLLAMA_MODELS=/root/.ollama

# Pull at build time. `ollama serve` runs on CPU here (no GPU needed just to
# download weights); we wait for it, pull, then stop it.
RUN ollama serve & \
    pid=$! ; \
    until ollama list >/dev/null 2>&1; do sleep 1; done ; \
    ollama pull "$TEXT_MODEL" && \
    ollama pull "$EMBED_MODEL" ; \
    kill "$pid"

# engine.py defaults simple {"prompt":...} requests to OLLAMA_MODEL_NAME — pin it
# to the baked text model so the served model can't drift from what's in the image.
ENV OLLAMA_MODEL_NAME=${TEXT_MODEL}
```

### 2. `src/start.sh` — delete the runtime pulls

Remove the `OLLAMA_MODEL_NAME` pull block and the `OLLAMA_EXTRA_MODELS` loop
(currently ~lines 32–53). The models are baked, so the start script just needs to
serve and run the handler:

```bash
# (after the "wait for server to start" loop)
# Models are baked into the image (see Dockerfile) — no runtime pull.
python -u handler.py $1
```

(Optional tidy: the `pgrep ollama | xargs kill` line errors on empty input — use
`pgrep ollama | xargs -r kill` or `... 2>/dev/null`.)

### 3. `.runpod/hub.json` — drop the now-build-time model inputs

Remove the `OLLAMA_MODEL_NAME` and `OLLAMA_EXTRA_MODELS` entries from the `env`
array (leaving them would mislead in the console — they no longer do anything at
runtime). Keep `MAX_CONCURRENCY` and `OLLAMA_NUM_PARALLEL`.

### 4. `src/engine.py` — optional

Logic is unchanged (the default now comes from the baked `ENV`). Optionally change
the `"llama3.2:1b"` fallback to a louder default or an explicit error, since it
should never be hit once `OLLAMA_MODEL_NAME` is baked in.

---

## Build & deploy

```bash
# Needs ~20GB free disk for the model layers. CPU-only build (no GPU to pull).
docker build -t <your-registry>/runpod-worker-ollama:baked .
# To override models without editing the Dockerfile:
#   --build-arg TEXT_MODEL=... --build-arg EMBED_MODEL=...

docker push <your-registry>/runpod-worker-ollama:baked
```

Then in the RunPod console (Serverless → this endpoint):
1. Point the endpoint at the new `:baked` image tag.
2. **Remove** the `OLLAMA_MODEL_NAME` and `OLLAMA_EXTRA_MODELS` env vars (baked now).
3. Set **min workers = 0** (flex) — kills the 24/7 active-worker cost.
4. Confirm **FlashBoot** is enabled.
5. Raise **idle timeout** to ~60–120s (keeps a worker warm across turns in a
   session, then scales to zero).
6. **Detach the network volume** if one is attached — `OLLAMA_MODELS` no longer
   points at it, so it'd just be unused cost + region lock.

---

## Verify

1. Trigger a cold start and watch the worker logs — you should **NOT** see
   "pulling manifest" / multi-GB downloads. Model load → VRAM only.
2. Smoke-test both routes (examples in `test_inputs/`):
   - `test_inputs/openai_completion.json` → text generation.
   - `test_inputs/openai_embeddings.json` → embeddings (model `nomic-embed-text`).
3. Confirm cold-start wall time dropped substantially vs the re-download path.
4. Back in the game project (`reddit_bot_project`), after some prod play, re-run the
   cost analysis (`docs/cost-tracking.md`). The `cold_start_rate` (from
   `usage_events.delay_ms`) should drop, and cost-per-turn should be cleaner now
   that the 24/7 active-worker tax is gone.

---

## Gotchas

- **Exact name match matters.** Completions use the baked `OLLAMA_MODEL_NAME`
  default (app sends no model); embeddings must be baked under the exact name the
  app requests (`nomic-embed-text`). A mismatch → runtime "model not found".
- **Build host needs the disk** (~15GB of weights) and the **push is large**
  (~15–20GB image). First cold start on each new RunPod host still pulls the image;
  cached after.
- **`OLLAMA_MODELS` moved off `/runpod-volume`** on purpose — bake **or** volume,
  not both. Don't re-attach a volume.
- `containerDiskInGb: 20` in `hub.json` is runtime scratch and is fine — the models
  live in the image's read-only layers, not the container disk.
- The `embed_model/` directory is now a redundant alternative approach — safe to
  delete once this is working.
