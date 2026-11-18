# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Read-only inspection of the host's UART flow-control configuration.

This reads the live Linux device tree through sysfs to tell whether the kernel
was told the UART has hardware RTS/CTS (the `uart-has-rtscts` property). It does
NOT change anything. Everything here degrades to "unknown" when it cannot tell
(non-Linux hosts, an unusual port name, or the node is not found), so the harness
stays usable on a developer machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_SYS_TTY = Path("/sys/class/tty")


@dataclass(frozen=True)
class UartFlowControlState:
    """Relevant properties from the bound UART's live device-tree node."""

    uart_has_rtscts: bool
    fsl_dte_mode: bool


def inspect_uart_flow_control(serial_port: str) -> UartFlowControlState | None:
    """Read the UART's live DT flow-control properties, or return unknown."""
    name = Path(serial_port).name
    if not name:
        return None
    of_node = _SYS_TTY / name / "device" / "of_node"
    if not of_node.is_dir():
        return None
    return UartFlowControlState(
        uart_has_rtscts=(of_node / "uart-has-rtscts").exists(),
        fsl_dte_mode=(of_node / "fsl,dte-mode").exists(),
    )


def uart_has_rtscts(serial_port: str) -> bool | None:
    """Whether the port's device-tree node declares `uart-has-rtscts`.

    Returns:
        True  - the DT node has the property (hardware flow control configured).
        False - the DT node exists but lacks the property (not configured).
        None  - could not determine (non-Linux, unusual port, or node absent).
    """
    state = inspect_uart_flow_control(serial_port)
    return None if state is None else state.uart_has_rtscts
