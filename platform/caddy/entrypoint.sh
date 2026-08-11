#!/bin/sh

set -eu

load_secret() {
  variable_name="$1"
  file_variable_name="${variable_name}_FILE"
  eval "secret_path=\${${file_variable_name}:-}"
  eval "inline_value=\${${variable_name}:-}"

  if [ -n "${inline_value}" ]; then
    printf 'Error: %s must be supplied through %s, not the environment.\n' \
      "${variable_name}" "${file_variable_name}" >&2
    exit 1
  fi
  if [ -z "${secret_path}" ] || [ ! -r "${secret_path}" ]; then
    printf 'Error: %s is missing or unreadable.\n' "${file_variable_name}" >&2
    exit 1
  fi

  secret_value="$(cat "${secret_path}")"
  if [ -z "${secret_value}" ]; then
    printf 'Error: %s is empty.\n' "${file_variable_name}" >&2
    exit 1
  fi

  export "${variable_name}=${secret_value}"
  unset "${file_variable_name}"
}

surplasse_route=/etc/caddy/routes/surplasse.caddy
if [ -e "${surplasse_route}" ]; then
  if [ ! -f "${surplasse_route}" ] || [ -L "${surplasse_route}" ]; then
    printf 'Error: the active Surplasse route must be a regular file.\n' >&2
    exit 1
  fi
  load_secret OVH_APPLICATION_KEY
  load_secret OVH_APPLICATION_SECRET
  load_secret OVH_CONSUMER_KEY
fi

exec "$@"
