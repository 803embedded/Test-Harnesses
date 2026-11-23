# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

import logging

from adapter_test import checks
from adapter_test.config import ProbeConfig
from adapter_test.flow_control import UartFlowControlState, uart_has_rtscts

log = logging.getLogger("test")


def test_uart_has_rtscts_unknown_for_bogus_port() -> None:
    # No such tty node, so the result is "unknown", not True/False.
    assert uart_has_rtscts("/dev/definitely-not-a-real-port-xyz") is None


def test_uart_has_rtscts_unknown_for_empty_port() -> None:
    assert uart_has_rtscts("") is None


def test_os_flow_control_skipped_for_3wire() -> None:
    # Nothing to verify when not using hardware flow control.
    assert checks.check_os_flow_control(ProbeConfig(flow_control="none"), log) == []


def test_os_flow_control_passes_when_declared(monkeypatch) -> None:
    state = UartFlowControlState(uart_has_rtscts=True, fsl_dte_mode=True)
    monkeypatch.setattr(checks.flow_control, "inspect_uart_flow_control", lambda _p: state)
    results = checks.check_os_flow_control(ProbeConfig(flow_control="rtscts"), log)
    assert len(results) == 1
    assert results[0].passed


def test_os_flow_control_fails_when_not_declared(monkeypatch) -> None:
    state = UartFlowControlState(uart_has_rtscts=False, fsl_dte_mode=True)
    monkeypatch.setattr(checks.flow_control, "inspect_uart_flow_control", lambda _p: state)
    results = checks.check_os_flow_control(ProbeConfig(flow_control="rtscts"), log)
    assert len(results) == 1
    assert not results[0].passed


def test_os_flow_control_unknown_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(checks.flow_control, "inspect_uart_flow_control", lambda _p: None)
    results = checks.check_os_flow_control(ProbeConfig(flow_control="rtscts"), log)
    assert len(results) == 1
    assert not results[0].passed


def test_os_flow_control_requires_dte_mode(monkeypatch) -> None:
    state = UartFlowControlState(uart_has_rtscts=True, fsl_dte_mode=False)
    monkeypatch.setattr(checks.flow_control, "inspect_uart_flow_control", lambda _p: state)
    results = checks.check_os_flow_control(ProbeConfig(flow_control="rtscts"), log)
    assert len(results) == 1
    assert not results[0].passed
    assert "fsl,dte-mode" in results[0].detail
