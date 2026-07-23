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
bundle_id=com.benjaminfeder.fleet-dash.injector
if [ "$mode" = "staging" ]; then
  sed 's#\.claude/fleet-dash-prod-state/#.claude/fleet-dash-staging-state/#g' "$source_file" > "$compiled_source"
  bundle_id=com.benjaminfeder.fleet-dash.staging.injector
else
  cp "$source_file" "$compiled_source"
fi

osacompile -o "$destination" "$compiled_source"
plutil -replace OSAAppletStayOpen -bool true "$destination/Contents/Info.plist"
plutil -replace CFBundleIdentifier -string "$bundle_id" "$destination/Contents/Info.plist"
codesign --force --sign - "$destination"
