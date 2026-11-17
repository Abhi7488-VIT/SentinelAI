# The contrib Collector image is distroless (no shell, no HTTP client), so Compose could not
# healthcheck it. Add a static busybox for `wget` against the health_check extension.
FROM busybox:1.37.0-musl AS busybox

FROM otel/opentelemetry-collector-contrib:0.140.0
COPY --from=busybox /bin/busybox /bin/busybox
