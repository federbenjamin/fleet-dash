#!/bin/sh
set -eu

production_checkout=${FLEET_DASH_PROD_CHECKOUT:-"$HOME/.claude/fleet-dash-prod"}
production_state=${FLEET_DASH_PROD_STATE:-"$HOME/.claude/fleet-dash-prod-state"}
service=com.benjaminfeder.fleet-dash
fleet_url=http://127.0.0.1:8377
scratch=${TMPDIR:-/private/tmp}/codex
mkdir -p "$scratch"
snapshot=$(mktemp "$scratch/fleet-production.XXXXXX")
trap 'rm -f "$snapshot"' EXIT HUP INT TERM

fail() {
  echo "deploy failed: $*" >&2
  exit 1
}

git -C "$production_checkout" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
  fail "production checkout is missing: $production_checkout"
if [ -n "$(git -C "$production_checkout" status --porcelain --untracked-files=normal)" ]; then
  git -C "$production_checkout" status --short >&2
  fail "production checkout is dirty"
fi

before=$(git -C "$production_checkout" rev-parse HEAD)
git -C "$production_checkout" fetch origin main
release=$(git -C "$production_checkout" rev-parse origin/main)
git -C "$production_checkout" merge-base --is-ancestor "$before" "$release" ||
  fail "origin/main does not descend from the deployed release"

echo "promoting production: $(printf '%.12s' "$before") -> $(printf '%.12s' "$release")"
git -C "$production_checkout" switch --detach "$release"
npm --prefix "$production_checkout" ci --omit=dev
launchctl kickstart -k "gui/$(id -u)/$service"

attempt=0
while [ "$attempt" -lt 30 ]; do
  if curl -fsS --max-time 3 "$fleet_url/api/fleet" -o "$snapshot" 2>/dev/null &&
     /usr/bin/python3 -c 'import json,sys; value=json.load(open(sys.argv[1])); raise SystemExit(0 if value.get("instance",{}).get("mode")=="production" else 1)' "$snapshot"; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done
[ "$attempt" -lt 30 ] || fail "production API did not become healthy"

attempt=0
while [ "$attempt" -lt 30 ]; do
  if /usr/bin/python3 -c 'import json,sys,urllib.request; cfg=json.load(open(sys.argv[1])); request=urllib.request.Request(sys.argv[2],headers={"X-Act-Token":cfg["act_token"]}); value=json.load(urllib.request.urlopen(request,timeout=3)); raise SystemExit(0 if value.get("web_push",{}).get("delivery")=="ready" else 1)' "$production_state/config.json" "$fleet_url/api/diagnostics" 2>/dev/null; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done
[ "$attempt" -lt 30 ] || fail "production Web Push did not become ready"

echo "production ready: $release (API + Web Push)"
