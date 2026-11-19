# The Loki image is distroless; add a static busybox so Compose can healthcheck /ready.
# Loki runs as uid 10001 but the base image's /loki is root-owned; hand it to the loki user so
# the named data volume mounted there is initialised writable.
FROM busybox:1.37.0-musl AS busybox

FROM grafana/loki:3.6.0
COPY --from=busybox /bin/busybox /bin/busybox
USER root
RUN ["/bin/busybox", "chown", "-R", "10001:10001", "/loki"]
USER 10001
