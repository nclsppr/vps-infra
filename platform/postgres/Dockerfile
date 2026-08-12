# syntax=docker/dockerfile:1.24.0@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# check=error=true

ARG POSTGRES_BASE_IMAGE=scratch
FROM ${POSTGRES_BASE_IMAGE}

# The official entrypoint invokes gosu only when PID 1 starts as root. Atlas
# starts PostgreSQL directly as the image's native postgres identity instead.
# Removing gosu also removes an otherwise unreachable vulnerable Go binary from
# the runtime image.
USER root
RUN test "$(id -u postgres)" = 70 \
    && test "$(id -g postgres)" = 70 \
    && test -x /usr/local/bin/gosu \
    && rm /usr/local/bin/gosu \
    && test ! -e /usr/local/bin/gosu

USER 70:70
