#!/bin/bash

cleanup() {
    echo "Cleaning up..."
    pkill -P $$ # Kill all child processes of the current script
    exit 0
}

# Trap exit signals and call the cleanup function
trap cleanup SIGINT SIGTERM

# Kill any existing ollama processes
pgrep ollama | xargs kill

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
# IF $OLLAMA_MODEL_NAME is set, make sure to pull the model, else just skip
if [ -z "$OLLAMA_MODEL_NAME" ]; then
    echo "No model name provided. Skipping model pull..."
else
    echo "Pulling model $OLLAMA_MODEL_NAME..."
    ollama pull $OLLAMA_MODEL_NAME
fi

# IF $OLLAMA_EXTRA_MODELS is set, pull each additional comma-separated model
# (e.g. "nomic-embed-text,all-minilm"). Useful for preloading embedding models
# alongside the primary model.
if [ -n "$OLLAMA_EXTRA_MODELS" ]; then
    IFS=',' read -r -a EXTRA_MODELS <<< "$OLLAMA_EXTRA_MODELS"
    for EXTRA_MODEL in "${EXTRA_MODELS[@]}"; do
        # Trim surrounding whitespace so "a, b" works as well as "a,b"
        EXTRA_MODEL="$(echo "$EXTRA_MODEL" | xargs)"
        if [ -n "$EXTRA_MODEL" ]; then
            echo "Pulling extra model $EXTRA_MODEL..."
            ollama pull "$EXTRA_MODEL"
        fi
    done
fi

python -u handler.py $1