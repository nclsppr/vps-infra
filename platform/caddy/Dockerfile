# syntax=docker/dockerfile:1.24.0@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# check=error=true

ARG CADDY_BUILDER_IMAGE=scratch
ARG CADDY_RUNTIME_IMAGE=scratch

FROM ${CADDY_BUILDER_IMAGE} AS build

ARG CADDY_DNS_MODULE
RUN test -n "${CADDY_DNS_MODULE}" \
    && case "${CADDY_DNS_MODULE}" in *@*) ;; *) exit 1 ;; esac \
    && xcaddy build --with "${CADDY_DNS_MODULE}" --output /usr/bin/vps-caddy

FROM ${CADDY_RUNTIME_IMAGE}

COPY --from=build /usr/bin/vps-caddy /usr/bin/caddy
COPY --chmod=0555 entrypoint.sh /usr/bin/vps-caddy-entrypoint

ENTRYPOINT ["/usr/bin/vps-caddy-entrypoint"]
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
