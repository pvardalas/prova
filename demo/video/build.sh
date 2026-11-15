#!/usr/bin/env bash
# Re-assemble the Prova demo video: narration (audio/*.wav) + HTML scenes (scenes/*.html) recorded with Playwright.
#   ./build.sh                      -> draft_v1.mp4 
#   ./build.sh --out final.mp4      -> other output name
#   TEMPO=1.0 ./build.sh -> natural narration speed;  ./build.sh --only d1,d6 -> re-record only those scenes
# Regenerate narration after editing segments.json:  (set -a; . ../../.env; set +a; python3 tts.py <ids...>)
set -euo pipefail
cd "$(dirname "$0")"
exec uv run --no-project --with playwright --with imageio-ffmpeg python build.py "$@"
