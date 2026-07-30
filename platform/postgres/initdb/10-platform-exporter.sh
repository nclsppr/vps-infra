#!/bin/sh

set -eu

secret_path="${POSTGRES_EXPORTER_PASSWORD_FILE:-}"
if [ -z "${secret_path}" ] || [ ! -r "${secret_path}" ]; then
  printf 'Error: POSTGRES_EXPORTER_PASSWORD_FILE is missing or unreadable.\n' >&2
  exit 1
fi

exporter_password="$(cat "${secret_path}")"
if [ -z "${exporter_password}" ]; then
  printf 'Error: the PostgreSQL exporter password is empty.\n' >&2
  exit 1
fi

psql --variable ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --set=exporter_password="${exporter_password}" <<'SQL'
SELECT format(
  'CREATE ROLE postgres_exporter LOGIN PASSWORD %L CONNECTION LIMIT 5',
  :'exporter_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'postgres_exporter'
) \gexec

SELECT format(
  'ALTER ROLE postgres_exporter WITH LOGIN PASSWORD %L CONNECTION LIMIT 5',
  :'exporter_password'
) \gexec

GRANT pg_monitor TO postgres_exporter;
ALTER ROLE postgres_exporter SET search_path = pg_catalog;
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
GRANT CONNECT ON DATABASE postgres TO postgres_exporter;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
