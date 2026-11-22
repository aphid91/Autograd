#!/usr/bin/env bash
# Assemble the static site: the web app plus every .npz in web/saves/ (the
# bundled boards; the repo root's saves are the desktop's and the tests'
# fixtures, and are not shipped), listed in saves/index.json. Used by the Pages workflow and for local runs:
#
#   bash web/build.sh && python3 -m http.server -d _site 8000
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/_site}"

rm -rf "$OUT"
mkdir -p "$OUT/saves"
cp "$ROOT"/web/index.html "$ROOT"/web/style.css "$ROOT"/web/*.js "$ROOT"/web/types.json "$OUT"/
cp "$ROOT"/web/manifest.webmanifest "$OUT"/
cp -r "$ROOT"/web/vendor "$ROOT"/web/icons "$OUT"/
cp "$ROOT"/web/saves/*.npz "$OUT/saves/"
(cd "$OUT/saves" && ls *.npz) \
  | python3 -c 'import json, sys; print(json.dumps([l.strip() for l in sys.stdin]))' \
  > "$OUT/saves/index.json"
# Presentations (`#b=<name>`): each a <name>.json + <name>.npz pair, as the
# editor's Presentation > Export zips them, unzipped into web/presentations/.
mkdir -p "$OUT/presentations"
find "$ROOT/web/presentations" -maxdepth 1 \( -name '*.json' -o -name '*.npz' \) -exec cp {} "$OUT/presentations/" \; 2>/dev/null || true
# The service worker's precache list and version: every file in the site,
# and a hash of all their contents (see web/sw.js).
python3 - "$OUT" <<'PY'
import hashlib, json, pathlib, sys
out = pathlib.Path(sys.argv[1])
files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.name != "sw.js")
h = hashlib.sha256()
for f in files:
    h.update(f.encode()); h.update((out / f).read_bytes())
sw = out / "sw.js"
sw.write_text(sw.read_text()
              .replace('"__VERSION__"', json.dumps(h.hexdigest()[:16]))
              .replace("__FILES__", json.dumps(["./"] + files)))
PY
echo "built $OUT"
