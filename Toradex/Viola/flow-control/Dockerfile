# Post-assembly test harness image for the Toradex Colibri iMX6ULL (armv7).
# Build for the target arch, e.g.:
#   docker buildx build --platform linux/arm/v7 -t adapter-test .
FROM python:3.11.16-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY README.md ./
COPY constraints.txt ./
COPY src/ ./src/

RUN pip install --no-cache-dir --constraint constraints.txt .

# The XBee UART must be passed in at runtime. On the 803-001-001 adapter it is
# UART_B = iMX6ULL UART2 = /dev/ttymxc1:
#   docker run --rm --device /dev/ttymxc1 -e XBEE_PORT=/dev/ttymxc1 adapter-test
# The current GPIO diagnostic uses legacy /sys/class/gpio, not /dev/gpiochip*.
# A container needs platform-specific writable sysfs access.
ENTRYPOINT ["adapter-test"]
