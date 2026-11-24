# Skirnir router as a container image (ADR-0116, ADR-0121). Same program, same config.yaml, same paths as the systemd
# install (router/install.sh); docs/install-router.md#container describes the first start and compose.yaml the service.
#
#   docker build -t skirnir-router router/          (or: podman build -t skirnir-router router/)
#
# Base image pinned by digest (ADR-0055, test/check_pinned_references.py; Dependabot proposes updates). Python packages
# only from router/requirements-container-lock.txt: exact versions, SHA-256 hashes, wheels only, no dependency resolution
# at build time (ADR-0074). No secret is baked in: they arrive at run time as environment variables (`*_env` keys) or
# mounted files. The router runs as its own unprivileged account (uid/gid 10001, like skirnir-router on a host, ADR-0086),
# reads /etc/skirnir-router (mounted read-only) and writes only /var/lib/skirnir-router (router.state_dir) and
# /var/log/skirnir-router (client_auth.audit_log); its log goes to stdout.
FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/var/lib/skirnir-router

COPY requirements.txt requirements-session.txt requirements-container-lock.txt /opt/skirnir-router/
COPY skirnir_router/pydeps.py /tmp/pydeps.py
# Install every locked entry with its hash, refuse anything that would need a build or an unlisted file, then check that
# the result meets the router's requirements (pydeps.py verify, pip check) before pip itself leaves the image.
RUN python -m pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: \
        -r /opt/skirnir-router/requirements-container-lock.txt \
 && python /tmp/pydeps.py verify --requirements /opt/skirnir-router/requirements-session.txt \
 && python -m pip check \
 && python -c "import aiohttp, yaml, cryptography, paho.mqtt, uvloop, asyncpg" \
 && python -m pip uninstall --yes --quiet pip \
 && rm -f /tmp/pydeps.py \
 && groupadd --system --gid 10001 skirnir-router \
 && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/skirnir-router --no-create-home \
        --shell /usr/sbin/nologin skirnir-router \
 && install -d -o root -g skirnir-router -m 0750 /etc/skirnir-router \
 && install -d -o skirnir-router -g skirnir-router -m 0700 /var/lib/skirnir-router /var/log/skirnir-router

# Program files as router/install.sh copies them; owned by root, so the router cannot change its own code.
COPY skirnir_router /opt/skirnir-router/skirnir_router
COPY router.py ui.html ui.js skirnir.png favicon.png config.example.yaml config.minimal.yaml decision-tfidf.json \
     /opt/skirnir-router/
COPY --chmod=0755 container/entrypoint.sh container/healthcheck.py /opt/skirnir-router/container/

# Named volumes start with these owners and modes; a bind mount must give the folder to uid 10001 itself.
VOLUME ["/var/lib/skirnir-router", "/var/log/skirnir-router"]
USER 10001:10001
WORKDIR /var/lib/skirnir-router
EXPOSE 11434 11435
# GET /api/version on the API port over loopback (no identity needed, nothing secret sent); port and TLS from config.yaml.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/opt/skirnir-router/container/healthcheck.py"]
ENTRYPOINT ["/opt/skirnir-router/container/entrypoint.sh"]
CMD ["/etc/skirnir-router/config.yaml"]
