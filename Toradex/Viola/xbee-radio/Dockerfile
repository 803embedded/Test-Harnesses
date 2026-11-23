# xbee-link has no third-party dependencies, so the image is the Python base
# image plus the package source. Build it on the target (linux/arm/v7 on a
# Colibri iMX6ULL) or cross-build it with `docker buildx --platform`.
FROM python:3.11.16-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84

ENV PYTHONPATH=/opt/xbee-link \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY src/xbee_link /opt/xbee-link/xbee_link
# Default configuration; mount another file over it to change settings.
COPY examples/viola-imx6ull.toml /etc/xbee-link/config.toml

RUN python3 -m xbee_link --version

ENTRYPOINT ["python3", "-m", "xbee_link"]
CMD ["info"]
