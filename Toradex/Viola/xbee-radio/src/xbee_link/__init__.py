# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Dependency-free XBee API driver, buffer transfer and RF link test.

Driver layer (reusable): :mod:`frames`, :mod:`serial_port`, :mod:`xbee`,
:mod:`transfer`. Configuration scaffolding: :mod:`config`, :mod:`radio_setup`.
Test scaffolding (safe to delete): :mod:`linktest`, :mod:`__main__`.
"""

from .config import ConfigError, LinkConfig
from .radio_setup import SetupError, open_radio
from .transfer import BufferReceiver, TransferError, send_buffer
from .xbee import XBee, XBeeError

__version__ = "0.1.0"

__all__ = [
    "BufferReceiver",
    "ConfigError",
    "LinkConfig",
    "SetupError",
    "TransferError",
    "XBee",
    "XBeeError",
    "__version__",
    "open_radio",
    "send_buffer",
]
