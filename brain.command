#!/bin/zsh
# Double-clickable launcher: re-index the vault, then open the brain.
# Regeneration takes ~2s, so the map is always current at open time.
cd "$(dirname "$0")"
python3 generate.py --open
