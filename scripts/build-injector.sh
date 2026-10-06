#!/bin/sh
set -eu

mode=${1:-}
destination=${2:-}
if [ "$mode" != "production" ] && [ "$mode" != "staging" ]; then
  echo "usage: build-injector.sh production|staging /absolute/output.app" >&2
  exit 2
fi
case "$destination" in
  /*.app) ;;
  *) echo "destination must be an absolute .app path" >&2; exit 2 ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
scratch=${TMPDIR:-/private/tmp}/codex
mkdir -p "$scratch"
source_file=$script_dir/injector.applescript
compiled_source=$scratch/injector-$mode.applescript
bundle_id=$(cd "$script_dir" && /usr/bin/python3 -m fleetdash.launchd bundle-id "$mode")
state_dir=$(cd "$script_dir" && /usr/bin/python3 -m fleetdash.launchd state "$mode")
applescript_state=$(printf '%s' "$state_dir" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
STATE_DIR=$applescript_state awk '
  BEGIN { value = ENVIRON["STATE_DIR"] }
  { at = index($0, "@STATE_DIR@"); if (at) $0 = substr($0, 1, at - 1) value substr($0, at + 11); print }
' "$source_file" > "$compiled_source"

osacompile -o "$destination" "$compiled_source"
plutil -replace OSAAppletStayOpen -bool true "$destination/Contents/Info.plist"
plutil -replace CFBundleIdentifier -string "$bundle_id" "$destination/Contents/Info.plist"
codesign --force --sign - "$destination"
