#!/bin/sh
# Off-platform logical backup of the audit database (complements the platform's
# volume backups). Run from an operator machine or CI, never inside the app.
#
#   DATABASE_URL=postgresql://... scripts/db_backup.sh [output-dir]
#
# On Railway use the PostgreSQL service's *public* URL (DATABASE_PUBLIC_URL)
# or `railway run --service Postgres scripts/db_backup.sh`. Needs pg_dump 16+.
# Writes <output-dir>/aq-<UTC timestamp>.dump (custom format) plus a .sha256,
# checks the archive is readable, and never prints the connection URL.
set -eu

: "${DATABASE_URL:?set DATABASE_URL (it is never printed)}"
out_dir="${1:-backups}"
mkdir -p "$out_dir"
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
file="$out_dir/aq-$stamp.dump"

# libpq does not understand SQLAlchemy driver suffixes
url="$(printf '%s' "$DATABASE_URL" | sed -e 's#^postgresql+psycopg://#postgresql://#' -e 's#^postgres://#postgresql://#')"

pg_dump --dbname="$url" --format=custom --compress=6 --no-owner --no-privileges --file="$file"
pg_restore --list "$file" >/dev/null   # the archive must be readable
tables="$(pg_restore --list "$file" | grep -c ' TABLE DATA ' || true)"
( cd "$out_dir" && sha256sum "aq-$stamp.dump" > "aq-$stamp.dump.sha256" )
echo "backup written: $file ($tables tables with data)"
