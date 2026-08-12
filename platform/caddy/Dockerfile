# syntax=docker/dockerfile:1.24.0@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# check=error=true

ARG CADDY_BUILDER_IMAGE=scratch
ARG CADDY_RUNTIME_IMAGE=scratch

# Alpine 3.23 security updates. Each remote payload is accepted only if it has
# the reviewed SHA-256 value. A changed or unavailable payload fails the build.
FROM scratch AS apk-amd64
ADD --checksum=sha256:7a570d080d05e60b1256ea73586924bf9a25f468c57aae10d1cdfe7fcc3f93a2 https://dl-cdn.alpinelinux.org/alpine/v3.23/main/x86_64/c-ares-1.34.8-r0.apk /c-ares.apk
ADD --checksum=sha256:dc9f2b821e2ce3c578dc6e68dd79ff802d2eb05a390fb15164ad6288c815099c https://dl-cdn.alpinelinux.org/alpine/v3.23/main/x86_64/curl-8.20.0-r0.apk /curl.apk
ADD --checksum=sha256:a162907a766bf7828ebc894b445bf53821db406d5ed50dc987bb8a1933658338 https://dl-cdn.alpinelinux.org/alpine/v3.23/main/x86_64/libcurl-8.20.0-r0.apk /libcurl.apk

FROM scratch AS apk-arm64
ADD --checksum=sha256:7468b8355eb7a54c2829ed3e72b61f49b2968c1dd0281e16ddf939fcf1c9e91b https://dl-cdn.alpinelinux.org/alpine/v3.23/main/aarch64/c-ares-1.34.8-r0.apk /c-ares.apk
ADD --checksum=sha256:3f17be3f1110863109b2163ee47b2edbcbedfc1fdb2138b2f152fbd8e58d9ba4 https://dl-cdn.alpinelinux.org/alpine/v3.23/main/aarch64/curl-8.20.0-r0.apk /curl.apk
ADD --checksum=sha256:6caad44abb771d2f3bad4aaad41e25970292fe94e943e2f2a1f6f4dfec47375c https://dl-cdn.alpinelinux.org/alpine/v3.23/main/aarch64/libcurl-8.20.0-r0.apk /libcurl.apk

ARG TARGETARCH
FROM apk-${TARGETARCH} AS runtime-apks

FROM --platform=$BUILDPLATFORM ${CADDY_BUILDER_IMAGE} AS build

ARG TARGETOS
ARG TARGETARCH
ENV GOTOOLCHAIN=local \
    GOPROXY=https://proxy.golang.org \
    GOSUMDB=sum.golang.org

WORKDIR /src
COPY build/main.go build/go.mod build/go.sum ./
COPY patches/caddy-cel-go-v0.29.patch /tmp/caddy-cel-go-v0.29.patch
RUN go mod download \
    && go mod verify \
    && caddy_source="$(go env GOMODCACHE)/github.com/caddyserver/caddy/v2@v2.11.4" \
    && chmod u+w "${caddy_source}/modules/caddyhttp/celmatcher.go" \
    && cd "${caddy_source}" \
    && git apply --check --unidiff-zero /tmp/caddy-cel-go-v0.29.patch \
    && git apply --unidiff-zero /tmp/caddy-cel-go-v0.29.patch \
    && printf '%s  %s\n' \
       'e4a3578e3307cb0d97b3d140aff2a8d14fb2147d0fd5122c1d94e523c3ed89bb' \
       'modules/caddyhttp/celmatcher.go' \
       | sha256sum -c - \
    && cd /src \
    && CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" \
       go build -mod=readonly -o /usr/bin/vps-caddy \
       -ldflags '-w -s' -trimpath -tags nobadger,nomysql,nopgx

FROM ${CADDY_RUNTIME_IMAGE}

COPY --from=runtime-apks / /tmp/caddy-security-apks/
RUN apk add --no-cache --no-network --upgrade \
      /tmp/caddy-security-apks/c-ares.apk \
      /tmp/caddy-security-apks/curl.apk \
      /tmp/caddy-security-apks/libcurl.apk \
    && rm -rf /tmp/caddy-security-apks

COPY --from=build /usr/bin/vps-caddy /usr/bin/caddy
COPY --chmod=0555 entrypoint.sh /usr/bin/vps-caddy-entrypoint

ENTRYPOINT ["/usr/bin/vps-caddy-entrypoint"]
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
