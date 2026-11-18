# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

import logging

from adapter_test import logging_setup


def test_dockerenv_is_detected(monkeypatch) -> None:
    monkeypatch.setattr(
        logging_setup.os.path, "exists", lambda path: path == "/.dockerenv"
    )

    assert logging_setup.in_container()


def test_container_syslog_request_falls_back_to_stdout(monkeypatch) -> None:
    monkeypatch.setattr(
        logging_setup.os.path, "exists", lambda path: path == "/.dockerenv"
    )

    logger = logging_setup.setup_logging(verbose=True, use_syslog=True)
    try:
        assert logger.level == logging.DEBUG
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.StreamHandler)
        assert not logger.propagate
    finally:
        logger.handlers.clear()
