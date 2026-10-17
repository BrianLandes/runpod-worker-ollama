ARG OLLAMA_VERSION=0.30.10

# Use an official base${OLLAMA_VERSION} image with your desired version
FROM ollama/ollama:${OLLAMA_VERSION}

ENV PYTHONUNBUFFERED=1

# Set up the working directory
WORKDIR /

RUN apt-get update --yes --quiet && DEBIAN_FRONTEND=noninteractive apt-get install --yes --quiet --no-install-recommends \
    software-properties-common \
    gpg-agent \
    build-essential apt-utils \
    && apt-get install --reinstall ca-certificates \
    && add-apt-repository --yes ppa:deadsnakes/ppa && apt update --yes --quiet \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --quiet --no-install-recommends \
    python3.11 \
    python3.11-dev \
    python3.11-distutils \
    python3.11-lib2to3 \
    python3.11-gdbm \
    python3.11-tk \
    bash \
    curl && \
    ln -s /usr/bin/python3.11 /usr/bin/python && \
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# --- Bake models into the image (was: pulled at runtime onto a network volume) ---
# Models now ship in the image and load from local disk on every cold start:
# no re-download, no network volume, no region lock. Change models = rebuild.
# Placed before `ADD ./src` so editing source code doesn't invalidate the ~15GB model layer.
ARG TEXT_MODEL=hf.co/TheDrummer/Cydonia-24B-v4.3-GGUF:Q4_K_M
ARG EMBED_MODEL=nomic-embed-text

# In-image model dir — deliberately NOT /runpod-volume, so a stray volume mount
# can't overmount and hide the baked models.
ENV OLLAMA_MODELS=/root/.ollama

# Pull at build time. `ollama serve` runs on CPU here (no GPU needed just to
# download weights); we wait for it to be ready, pull both models, then stop it.
# `set -e` makes a failed pull abort the build instead of shipping an empty image.
RUN set -e; \
    ollama serve & \
    pid=$!; \
    until ollama list >/dev/null 2>&1; do sleep 1; done; \
    ollama pull "$TEXT_MODEL"; \
    ollama pull "$EMBED_MODEL"; \
    kill "$pid" 2>/dev/null || true

# engine.py defaults simple {"prompt":...} requests to OLLAMA_MODEL_NAME — pin it
# to the baked text model so the served model can't drift from what's in the image.
ENV OLLAMA_MODEL_NAME=${TEXT_MODEL}

# --- Context window ---
# Ollama's default is 4096 tokens, far below this model's native 131072. A prompt
# longer than the window is NOT rejected: Ollama keeps the TAIL and silently
# discards the front, so the model answers confidently having never seen the
# opening of its own prompt. Callers sending ~5.5k-token prompts were losing the
# whole leading section that way.
#
# Not settable per request on the OpenAI-compatible route we serve — Ollama's
# /v1/completions accepts and ignores unknown fields, so a caller-supplied
# num_ctx is a no-op. The server-wide env var is the only knob.
#
# VRAM: the KV cache is preallocated at load, ~160 KiB/token for Cydonia-24B
# (40 layers x 8 KV heads x 256 x 2 bytes), on top of ~13.9 GiB of weights.
#   4096 -> +0.6 GiB (14.5 total)   8192 -> +1.25 GiB (15.2 total)
#  16384 -> +2.5 GiB (16.4 total)  32768 -> +5.0 GiB (18.9 total)
# 8192 clears the callers' largest prompts and still fits a 24 GB worker with
# room to spare. Raising it much further needs the endpoint pinned to 24 GB GPUs.
ENV OLLAMA_CONTEXT_LENGTH=8192

# Ollama allocates OLLAMA_CONTEXT_LENGTH x OLLAMA_NUM_PARALLEL worth of KV cache
# and picks the parallelism itself when unset (up to 4). Left to its own devices
# that quietly multiplies the figures above by 4 — 8192 x 4 = 5 GiB of KV — which
# is how raising the context turns into an out-of-memory on cold start. Pin it so
# the memory math is predictable.
#
# 2 since 0.4.1 (2026-10-17). At 1, the caller's turn — ~10 passes fired together after every narrated beat —
# queued INSIDE Ollama: production timings 2026-10-16/17 put 213 s of execution against 120 s of real GPU work per
# beat, and decode (2/3 of the GPU time) is memory-bandwidth bound, so two sequences decoding together cost little
# more than one. Measure busy vs queued from the caller's timing_events before going to 4
# (alsion: docs/turn-cost-review-2026-10-17.md, docs/llm-call-practices.md).
ENV OLLAMA_NUM_PARALLEL=2

# KV cache at 8-bit (needs flash attention, pinned on below): half the f16 cache for a near-invisible quality
# change, so 2 slots cost what 1 did. At 8192 context, Cydonia-24B, with nomic-embed resident (~0.8 GiB):
#   1 slot f16  +1.25 GiB  (~16.0 total)    2 slots q8_0 +1.25 GiB (~16.0 total)
#   4 slots q8_0 +2.5 GiB  (~17.2 total)    4 slots f16  +5.0 GiB  (~19.7 total)
# Run tools/parity_check.py against the built image before deploying (DEPLOY.md).
ENV OLLAMA_KV_CACHE_TYPE=q8_0

# --- Keep both models resident ---
# Every embed rides this worker beside the text model. If Ollama ever unloads one to serve the other, each embed
# turns into a model load (a cold Cydonia load measured 14.6 s locally). On a 24 GB card both fit at once
# (Cydonia ~14.6 GiB at 8192 + nomic-embed ~0.8 GiB), so pin it:
#   MAX_LOADED_MODELS=2 — both may be resident (Ollama's default is 3 per GPU; pinned so it is explicit);
#   KEEP_ALIVE=-1       — never unload on idle. The default 5m dropped nomic after a quiet spell, and the next
#                          embed paid the load. The worker is the only tenant; its idle timeout ends it anyway.
# start.sh also loads both before the handler takes a job, and logs `ollama ps` — the RunPod log then shows
# whether both are resident on whatever GPU the endpoint landed on. On a 16-20 GB card they are NOT both
# resident and every model switch is a reload: keep the endpoint on 24 GB GPUs.
ENV OLLAMA_MAX_LOADED_MODELS=2
ENV OLLAMA_KEEP_ALIVE=-1
ENV EMBED_MODEL_NAME=${EMBED_MODEL}

# Flash attention: faster prefill on long prompts and a smaller KV cache. Recent Ollama starts its runners with
# `--flash-attn auto` (seen on 0.35.1 locally), so this may already be on; pinned so it can't silently be off.
ENV OLLAMA_FLASH_ATTENTION=1

# Set the working directory
WORKDIR /work

# Add my src as /work
ADD ./src /work

# Install runpod and its dependencies.
# --ignore-installed: the base image ships a Debian-managed `cryptography` (a transitive
# dep of runpod) that has no RECORD file, so pip can't uninstall it to upgrade. Installing
# over it instead of uninstalling first avoids the "uninstall-no-record-file" build failure.
RUN pip install --ignore-installed -r requirements.txt && chmod +x /work/start.sh
    

# Set the entrypoint
ENTRYPOINT ["/bin/sh", "-c", "/work/start.sh"]