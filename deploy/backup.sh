#!/usr/bin/env bash
# Glow Derma backups (docs/ops/deploy.md §11). Run as root on the VPS.
#
#   deploy/backup.sh nightly       database + media volume, then removes nightly files older than KEEP_DAYS
#                                  (cron: deploy/glowderma-backup.cron)
#   deploy/backup.sh pre-upgrade   database only, before every upgrade (§4); the rollback path across migrations
#   deploy/backup.sh pre-restore   database only, before a restore (§11): what the restore is about to replace
#
# Every file is written as <name>.tmp, verified (pg_restore reads the whole archive; gzip and tar read the whole
# tarball) and only then renamed: a failed or partial dump never sits under a final name. Any failure exits
# non-zero with a message on stderr (the cron entry appends both to /var/log/glowderma-backup.log). Files are
# 0600 in a 0700 directory: they hold every personal record (docs/ops/compliance.md).
#
# Environment (optional): GLOWDERMA_STACK_DIR (default /srv/glowderma/backend), GLOWDERMA_BACKUP_DIR (default
# /srv/backups), GLOWDERMA_BACKUP_KEEP_DAYS (default 14).
set -euo pipefail
umask 077
export PATH=${PATH:+$PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

label=${1:-}
case $label in
nightly | pre-upgrade | pre-restore) ;;
*)
	echo "usage: $0 nightly|pre-upgrade|pre-restore" >&2
	exit 2
	;;
esac

stack_dir=${GLOWDERMA_STACK_DIR:-/srv/glowderma/backend}
backup_dir=${GLOWDERMA_BACKUP_DIR:-/srv/backups}
keep_days=${GLOWDERMA_BACKUP_KEEP_DAYS:-14}
stamp=$(date +%F-%H%M%S)
compose=(docker compose -f "$stack_dir/compose.prod.yaml" --project-directory "$stack_dir")

pending=""
fail() {
	echo "backup $label FAILED: $*" >&2
	exit 1
}
cleanup() {
	if [ -n "$pending" ]; then
		rm -f -- "$pending"
	fi
}
trap cleanup EXIT

install -d -m 0700 "$backup_dir"

# Database: custom-format dump as the superuser over the container's socket, read back in full.
db="$backup_dir/db-$label-$stamp.dump"
pending="$db.tmp"
"${compose[@]}" exec -T postgres pg_dump -U glowderma -Fc glowderma >"$pending" || fail "pg_dump"
[ -s "$pending" ] || fail "pg_dump wrote nothing"
"${compose[@]}" exec -T postgres pg_restore --file=/dev/null <"$pending" || fail "the dump does not read back"
mv -- "$pending" "$db"
pending=""
echo "backup $label: $db"

if [ "$label" = nightly ]; then
	# Media: the edge mounts the volume read-only at /srv/media.
	media="$backup_dir/media-$stamp.tgz"
	pending="$media.tmp"
	"${compose[@]}" exec -T edge tar -cf - -C /srv/media . | gzip >"$pending" || fail "media tarball"
	gzip -t "$pending" || fail "the media tarball is corrupt"
	tar -tzf "$pending" >/dev/null || fail "the media tarball does not list"
	mv -- "$pending" "$media"
	pending=""
	echo "backup $label: $media"
	# Only after both succeeded; pre-upgrade and pre-restore dumps are removed by hand.
	find "$backup_dir" -maxdepth 1 -type f \( -name 'db-nightly-*.dump' -o -name 'media-*.tgz' \) \
		-mtime +"$keep_days" -delete
fi
