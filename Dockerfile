FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/neopterygii/stagekit-wled-bridge"
LABEL org.opencontainers.image.description="YARG/RB3E Stage Kit to WLED Bridge"

WORKDIR /app
COPY . .

# No dependencies to install — pure stdlib

# Persistent settings live here. Mount a volume to keep brightness/palette/fps
# across container recreates.
VOLUME ["/data"]

EXPOSE 36107/udp
EXPOSE 8080/tcp

# Probes the port the status server actually binds, not a hardcoded one — with
# network_mode: host a wrong port silently probes some other container's
# listener and fails forever. Every failed check forks a shell + wget, and
# those pile up as unreaped zombies against the cgroup pid limit until the
# container can no longer create threads.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD wget -q --spider "http://127.0.0.1:${STATUS_PORT:-8080}/api/status" || exit 1

CMD ["python", "-u", "main.py"]
