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