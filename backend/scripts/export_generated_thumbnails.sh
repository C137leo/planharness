#!/usr/bin/env bash
# Export loss-preserving originals to local archive and lightweight web thumbnails.
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
archive="$repo/docs/assets/exercise-sources"
mkdir -p "$archive"
export_image() {
    local input="$1" label="$2" output="${1%.png}.jpg" saved="$archive/$2.png"
    test ! -e "$saved"
    sips -s format jpeg -s formatOptions 85 --resampleHeightWidthMax 384 "$input" --out "$output" >/dev/null
    test -s "$output"
    mv "$input" "$saved"
}
shopt -s nullglob
for input in "$repo"/frontend/src/static/ex/generated-v2/*.png; do
    name="$(basename "${input%.png}")"
    export_image "$input" "generated-v2-$name"
done
for input in "$repo"/frontend/src/static/ex/{wall-sit,back-extension}-v2.png; do
    name="$(basename "${input%.png}")"
    export_image "$input" "generated-$name"
done
for input in "$repo"/frontend/src/static/meals/*-v2.png; do
    name="$(basename "${input%.png}")"
    export_image "$input" "meal-$name"
done
python3 "$repo/backend/scripts/build_exercise_image_manifest.py"
