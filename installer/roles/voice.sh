#!/usr/bin/env bash
# Voice Worker role hook: configure TTS runtime directories.
set -euo pipefail
ROOT=$1
ROLE=$2
if [[ "$ROLE" != "voice" ]]; then
  exit 0
fi
echo "Configuring Voice Worker..."
install -d -m 0750 "$ROOT/storage/voices"
install -d -m 0750 "$ROOT/models/tts"
if command -v espeak-ng >/dev/null 2>&1; then
  echo "espeak-ng found: $(espeak-ng --version 2>/dev/null || echo 'unknown')"
else
  echo "espeak-ng not installed; voice worker will use remote TTS service."
fi
