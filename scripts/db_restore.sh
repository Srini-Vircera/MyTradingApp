#!/bin/sh
# Restore a backup made by scripts/db_backup.sh into an EMPTY database, then
# report its schema revision. Use it for restore drills (into a scratch
# database) and for disaster recovery (into a new PostgreSQL service).
#
#   TARGET_DATABASE_URL=postgresql://... scripts/db_restore.sh backups/aq-<stamp>.dump
#
# Refuses a target that already has tables. Stop the api and worker services
# before pointing them at a restored database; then run `aq db status`.
set -eu

file="${1:?usage: TARGET_DATABASE_URL=... scripts/db_restore.sh <dump-file>}"
: "${TARGET_DATABASE_URL:?set TARGET_DATABASE_URL (it is never printed)}"
url="$(printf '%s' "$TARGET_DATABASE_URL" | sed -e 's#^postgresql+psycopg://#postgresql://#' -e 's#^postgres://#postgresql://#')"

if [ -f "$file.sha256" ]; then
  ( cd "$(dirname "$file")" && sha256sum -c "$(basename "$file").sha256" >/dev/null )
fi
existing="$(psql "$url" -At -c "select count(*) from information_schema.tables where table_schema = 'public'")"
if [ "$existing" != "0" ]; then
  echo "REFUSED: the target database is not empty ($existing tables)" >&2
  exit 2
fi
pg_restore --dbname="$url" --no-owner --no-privileges --exit-on-error "$file"
rev="$(psql "$url" -At -c "select version_num from alembic_version")"
echo "restored $file; schema revision $rev"
