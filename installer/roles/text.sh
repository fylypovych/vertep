#!/usr/bin/env bash
# Text Worker role hook: configure Ollama and pull default model.
set -euo pipefail
ROOT=$1
ROLE=$2
if [[ "$ROLE" != "text" ]]; then
  exit 0
fi
echo "Configuring Text Worker..."
if command -v ollama >/dev/null 2>&1; then
  MODEL=$(grep -E '^OLLAMA_MODEL=' "$ROOT/.env" 2>/dev/null | cut -d= -f2 || true)
  MODEL=${MODEL:-llama3.2}
  echo "Pulling Ollama model: $MODEL"
  ollama pull "$MODEL" || echo "Ollama model pull failed; pull manually after installation."
else
  echo "Ollama not found; text worker will use external LLM API."
fi
