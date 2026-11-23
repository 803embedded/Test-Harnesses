# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Container-friendly logging setup.

Log to stdout when running interactively or inside a container
(so `docker logs` / journald capture it), and only fall back to syslog when
explicitly asked for on a host.
"""

from __future__ import annotations

import logging
import os
import sys

LOG_FORMAT = "[%(asctime)s] %(levelname)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def in_container() -> bool:
    """Best-effort detection of running inside a container."""
    return os.path.exists("/.dockerenv")


def setup_logging(*, verbose: bool = False, use_syslog: bool = False) -> logging.Logger:
    """Configure and return the package logger.

    By default logs to stdout. Set use_syslog=True to log to syslog instead,
    which is ignored when running in a container (syslog usually goes nowhere
    there unless /dev/log is bound in).
    """
    logger = logging.getLogger("adapter_test")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    handler: logging.Handler
    if use_syslog and not in_container():
        from logging.handlers import SysLogHandler

        handler = SysLogHandler(address="/dev/log")
        handler.setFormatter(logging.Formatter("adapter_test: %(levelname)s %(message)s"))
    else:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))

    logger.addHandler(handler)
    logger.propagate = False
    return logger
