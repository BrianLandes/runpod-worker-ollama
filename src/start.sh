#!/bin/bash

cleanup() {
    echo "Cleaning up..."
    pkill -P $$ # Kill all child processes of the current script
    exit 0
}

# Trap exit signals and call the cleanup function
trap cleanup SIGINT SIGTERM

# Kill any existing ollama processes (-r: skip kill when pgrep finds nothing)
pgrep ollama | xargs -r kill

# Start the ollama server and log its output
ollama serve 2>&1 | tee ollama.server.log &
OLLAMA_PID=$! # Store the process ID (PID) of the background command

check_server_is_running() {
    echo "Checking if server is running..."
    if cat ollama.server.log | grep -q "Listening"; then
        return 0 # Success
    else
        return 1 # Failure
    fi
}

# Wait for the server to start
while ! check_server_is_running; do
    sleep 5
done

# Models are baked into the image at build time (see Dockerfile) — no runtime pull.
# Load both now, before the handler takes a job, so no job's executionTime carries a model load. A failed
# preload is logged and ignored: the first job then loads the model as before.
EMBED_MODEL_NAME=${EMBED_MODEL_NAME:-nomic-embed-text}
echo "Preloading $OLLAMA_MODEL_NAME and $EMBED_MODEL_NAME..."
curl -s -m 300 http://localhost:11434/api/generate \
    -d "{\"model\": \"$OLLAMA_MODEL_NAME\", \"keep_alive\": -1}" > /dev/null || echo "Preload of $OLLAMA_MODEL_NAME failed"
curl -s -m 120 http://localhost:11434/api/embed \
    -d "{\"model\": \"$EMBED_MODEL_NAME\", \"input\": \"warm\", \"keep_alive\": -1}" > /dev/null \
    || echo "Preload of $EMBED_MODEL_NAME failed"
# Both should be listed here at 100% GPU. One missing, or a CPU/GPU split, means the GPU is too small for both.
ollama ps
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv 2>/dev/null

python -u handler.py $1