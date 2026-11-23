# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

import pytest

from adapter_test.config import (
    DEFAULT_FLOW_CONTROL,
    NUM_EXPECTED_PINS,
    PIN_MAP,
    VIOLA_IMX6ULL_X9_GPIOS,
    ProbeConfig,
)


def test_pin_map_matches_tuple() -> None:
    assert len(PIN_MAP) == len(VIOLA_IMX6ULL_X9_GPIOS)


def test_pin_map_count() -> None:
    assert len(PIN_MAP) == NUM_EXPECTED_PINS


def test_linux_gpio_numbers_unique() -> None:
    numbers = [pin.linux_gpio for pin in VIOLA_IMX6ULL_X9_GPIOS]
    assert len(numbers) == len(set(numbers))


def test_known_pin_mapping() -> None:
    # From the end-node reference: SODIMM_135 -> X9 pin 8 -> linux gpio 24.
    pin = PIN_MAP["SODIMM_135"]
    assert (pin.sodimm, pin.x9_pin, pin.linux_gpio) == (135, 8, 24)


def test_flow_control_defaults_to_rtscts() -> None:
    assert DEFAULT_FLOW_CONTROL == "rtscts"
    assert ProbeConfig().flow_control == "rtscts"


def test_flow_control_from_env(monkeypatch) -> None:
    monkeypatch.setenv("XBEE_FLOW_CONTROL", "rtscts")
    assert ProbeConfig.from_env().flow_control == "rtscts"


def test_validate_normalizes_config() -> None:
    config = ProbeConfig(flow_control="RTSCTS")
    config.validate()
    assert config.flow_control == "rtscts"


def test_invalid_environment_value_has_clear_error(monkeypatch) -> None:
    monkeypatch.setenv("XBEE_BAUD", "fast")
    with pytest.raises(ValueError, match=r"\$XBEE_BAUD must be an integer"):
        ProbeConfig.from_env()


@pytest.mark.parametrize("variant", ["imx6ull-512mb-wb", "imx6ull-512mb-wb-it"])
def test_wifi_module_variant_supports_uart(variant: str) -> None:
    ProbeConfig(module_variant=variant).validate()


def test_wifi_default_gpio_selection_omits_unavailable_pins() -> None:
    config = ProbeConfig(module_variant="imx6ull-512mb-wb", check_gpio=True)
    config.validate()
    selected = set(config.gpio_pins_to_check())
    assert "SODIMM_103" not in selected
    assert "SODIMM_135" in selected


def test_non_wifi_default_gpio_selection_includes_sodimm_103() -> None:
    config = ProbeConfig(module_variant="imx6ull-512mb-it", check_gpio=True)
    config.validate()
    assert "SODIMM_103" in config.gpio_pins_to_check()


def test_wifi_explicit_unavailable_gpio_is_rejected() -> None:
    config = ProbeConfig(
        module_variant="imx6ull-512mb-wb",
        check_gpio=True,
        gpio_pins=("SODIMM_103",),
    )
    with pytest.raises(ValueError, match="SODIMM_103"):
        config.validate()
