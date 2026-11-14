#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -c "ALTER DATABASE \"$POSTGRES_DB\" SET aximote.tz = '${AXIMOTE_TZ:-UTC}';"
