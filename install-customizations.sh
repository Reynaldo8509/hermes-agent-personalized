#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${HERMES_PLUGIN_DIR:-}" ]]; then echo "Set HERMES_PLUGIN_DIR to the Hermes plugins directory." >&2; exit 2; fi
force=0; [[ "${1:-}" == "--force" ]] && force=1
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p -- "$HERMES_PLUGIN_DIR"
shopt -s nullglob
for src in "$repo_dir"/custom/plugins/*; do
  [[ -d "$src" ]] || continue
  dst="$HERMES_PLUGIN_DIR/$(basename -- "$src")"
  if [[ -e "$dst" && $force -ne 1 ]]; then echo "Skip existing plugin: $(basename -- "$src") (pass --force to replace)"; continue; fi
  mkdir -p -- "$dst"
  cp -a -- "$src"/. "$dst"/
  echo "Installed plugin $(basename -- "$src")"
done
custom_dir="${HERMES_CUSTOM_DIR:-$(dirname -- "$HERMES_PLUGIN_DIR")/custom}"
mkdir -p -- "$custom_dir"
for name in network-ingest video2implementation; do
  src="$repo_dir/custom/$name"; [[ -d "$src" ]] || continue
  dst="$custom_dir/$name"
  if [[ -e "$dst" && $force -ne 1 ]]; then echo "Skip existing customization: $name (pass --force to replace)"; continue; fi
  mkdir -p -- "$dst"; cp -a -- "$src"/. "$dst"/
  echo "Installed customization $name"
done
echo "Review local secrets, unit paths, and Hermes configuration; restart only the intended service manually."
