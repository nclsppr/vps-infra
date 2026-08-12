ARG UBUNTU_IMAGE=ubuntu@sha256:678c6550cc43645e08669028bc177f50be4e7c5b8cca677067b1914d4afc7a03
FROM ${UBUNTU_IMAGE}

ENV container=docker

RUN apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        systemd systemd-sysv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY tests/fixtures/surplasse-systemd/guarded-supervisor /tmp/guarded-supervisor
COPY tests/fixtures/surplasse-systemd/docker.service /etc/systemd/system/docker.service
COPY ansible/roles/internal_platform/templates/vps-internal-platform.service.j2 /tmp/vps-internal-platform.service.j2
COPY ansible/roles/public_static_edge/templates/vps-public-static-edge.service.j2 /tmp/vps-public-static-edge.service.j2
COPY ansible/roles/firewall/templates/vps-docker-ingress-firewall.service.j2 /etc/systemd/system/vps-docker-ingress-firewall.service
COPY applications/surplasse/systemd/vps-surplasse.service /etc/systemd/system/vps-surplasse.service
COPY applications/surplasse/systemd/vps-public-static-edge-surplasse.conf /etc/systemd/system/vps-public-static-edge.service.d/50-surplasse.conf
COPY applications/surplasse/systemd/vps-internal-platform-surplasse.conf /etc/systemd/system/vps-internal-platform.service.d/50-surplasse.conf

RUN mkdir -p \
        /etc/systemd/system/vps-internal-platform.service.d \
        /srv/vps/runtime/internal-platform/platform \
        /srv/vps/runtime/public-static-edge \
        /srv/vps/runtime/surplasse/applications/surplasse \
        /srv/vps/runtime/surplasse/scripts \
        /usr/local/libexec/vps \
        /usr/local/sbin \
    && install -m 0755 /tmp/guarded-supervisor /usr/local/libexec/vps/systemd-simulated-service \
    && install -m 0755 /tmp/guarded-supervisor /srv/vps/runtime/surplasse/scripts/activate-surplasse-runtime \
    && install -m 0755 /tmp/guarded-supervisor /usr/local/libexec/vps/activate-surplasse-runtime \
    && ln -sf /bin/true /usr/bin/docker \
    && ln -sf /bin/true /usr/local/sbin/vps-docker-ingress-firewall \
    && sed 's#{{ vps_internal_platform_runtime_dir }}#/srv/vps/runtime/internal-platform/platform#g' \
        /tmp/vps-internal-platform.service.j2 \
        > /etc/systemd/system/vps-internal-platform.service \
    && sed 's#{{ vps_public_static_edge_runtime_dir }}#/srv/vps/runtime/public-static-edge#g' \
        /tmp/vps-public-static-edge.service.j2 \
        > /etc/systemd/system/vps-public-static-edge.service \
    && systemd-analyze verify \
        /etc/systemd/system/docker.service \
        /etc/systemd/system/vps-docker-ingress-firewall.service \
        /etc/systemd/system/vps-internal-platform.service \
        /etc/systemd/system/vps-public-static-edge.service \
        /etc/systemd/system/vps-surplasse.service \
    && systemctl enable \
        docker.service \
        vps-docker-ingress-firewall.service \
        vps-internal-platform.service \
    && systemctl reenable vps-public-static-edge.service \
    && systemctl enable vps-surplasse.service \
    && rm -f \
        /tmp/guarded-supervisor \
        /tmp/vps-internal-platform.service.j2 \
        /tmp/vps-public-static-edge.service.j2

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
