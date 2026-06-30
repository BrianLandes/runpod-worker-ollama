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

This repo currently uses `0.1.0`. For the next build pick the next number, e.g. `0.1.1`.

```bash
# set this once per rebuild — used by the commands below
export TAG=0.1.1
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
([Dockerfile](./Dockerfile)). Only these remain, both optional:

| Variable              | Purpose                                                        | Example |
|-----------------------|---------------------------------------------------------------|---------|
| `MAX_CONCURRENCY`     | Max concurrent requests per worker (optional, default 8)      | `8`     |
| `OLLAMA_NUM_PARALLEL` | Ollama parallel request setting (optional)                    | `4`     |

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
