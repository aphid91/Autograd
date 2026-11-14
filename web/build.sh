#!/usr/bin/env bash
# Assemble the static site: the web app plus every .npz at the repo root,
# listed in saves/index.json. Used by the Pages workflow and for local runs:
#
#   bash web/build.sh && python3 -m http.server -d _site 8000
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/_site}"

rm -rf "$OUT"
mkdir -p "$OUT/saves"
cp "$ROOT"/web/index.html "$ROOT"/web/style.css "$ROOT"/web/*.js "$ROOT"/web/types.json "$OUT"/
cp "$ROOT"/*.npz "$OUT/saves/"
(cd "$OUT/saves" && ls *.npz) \
  | python3 -c 'import json, sys; print(json.dumps([l.strip() for l in sys.stdin]))' \
  > "$OUT/saves/index.json"
echo "built $OUT"
