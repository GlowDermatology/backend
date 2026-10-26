#!/bin/sh
# Write the release's image tags into .env (docs/ops/deploy.md §3.4, §4, §6).
#
#   deploy/pin-release.sh                      the commits checked out in . and ../frontend
#   deploy/pin-release.sh <backend> <frontend> explicit tags (a rollback to images that are still local)
#
# compose.prod.yaml tags the images glowderma-backend:$GIT_SHA and glowderma-frontend:$FRONTEND_GIT_SHA and refuses
# to start without them, so every later command (also in a new shell or from cron) uses the release that was
# built, never an unbuilt ":latest". Run from the backend checkout (next to compose.prod.yaml and .env).
set -eu

env_file=${GLOWDERMA_ENV_FILE:-.env}
if [ "$#" -eq 2 ]; then
	backend=$1
	frontend=$2
elif [ "$#" -eq 0 ]; then
	backend=$(git rev-parse --short HEAD)
	frontend=$(git -C "${FRONTEND_CONTEXT:-../frontend}" rev-parse --short HEAD)
else
	echo "usage: $0 [<backend tag> <frontend tag>]" >&2
	exit 2
fi
for tag in "$backend" "$frontend"; do
	case $tag in
	'' | *[!A-Za-z0-9_.-]*)
		echo "pin-release: invalid image tag '$tag'" >&2
		exit 2
		;;
	esac
done
[ -f "$env_file" ] || {
	echo "pin-release: $env_file not found (run from the backend checkout)" >&2
	exit 1
}

# A copy with the same owner and mode (.env is 0600), filled, then renamed over .env: never half written.
tmp="$env_file.pin.$$"
trap 'rm -f -- "$tmp"' EXIT
cp -p -- "$env_file" "$tmp"
{
	grep -v -e '^GIT_SHA=' -e '^FRONTEND_GIT_SHA=' "$env_file" || true
	printf 'GIT_SHA=%s\nFRONTEND_GIT_SHA=%s\n' "$backend" "$frontend"
} >"$tmp"
mv -f -- "$tmp" "$env_file"
echo "pin-release: GIT_SHA=$backend FRONTEND_GIT_SHA=$frontend written to $env_file"
