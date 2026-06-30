#!/bin/sh
# Starts Ollama, pulls required models if not already cached, then stays alive.
# Model names are read from env vars so they stay in sync with rag_service.py.
set -e

EMBED_MODEL="${OLLAMA_EMBED_MODEL:-bge-m3}"
LLM_MODEL="${OLLAMA_LLM_MODEL:-mistral-nemo}"

ollama serve &
SERVER_PID=$!

echo "[ollama-init] Waiting for server..."
# The ollama image ships without curl/wget — use the built-in CLI which
# only succeeds once the daemon is reachable.
until ollama list >/dev/null 2>&1; do
    sleep 2
done
echo "[ollama-init] Server ready."

# Embedding model — always local, regardless of LLM provider
echo "[ollama-init] Pulling embed model: ${EMBED_MODEL}"
ollama pull "${EMBED_MODEL}"

# LLM model — skipped when Mistral API is configured (it replaces local LLM)
if [ -n "${MISTRAL_API_KEY}" ]; then
    echo "[ollama-init] MISTRAL_API_KEY set — skipping local LLM pull."
elif [ -n "${LLM_MODEL}" ]; then
    echo "[ollama-init] Pulling LLM model: ${LLM_MODEL}"
    ollama pull "${LLM_MODEL}"
fi

echo "[ollama-init] Ready."
wait "${SERVER_PID}"
