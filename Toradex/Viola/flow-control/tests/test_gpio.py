# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

from adapter_test import gpio


def test_existing_input_is_read_without_direction_change(tmp_path, monkeypatch) -> None:
    gpio_dir = tmp_path / "gpio24"
    gpio_dir.mkdir()
    direction = gpio_dir / "direction"
    direction.write_text("in")
    (gpio_dir / "value").write_text("1")
    monkeypatch.setattr(gpio, "SYSFS_ROOT", tmp_path)

    result = gpio.SysfsBackend(settle_s=0).probe(24)

    assert result.performed
    assert result.passed
    assert direction.read_text() == "in"


def test_existing_output_is_left_unchanged(tmp_path, monkeypatch) -> None:
    gpio_dir = tmp_path / "gpio24"
    gpio_dir.mkdir()
    direction = gpio_dir / "direction"
    direction.write_text("out")
    (gpio_dir / "value").write_text("1")
    monkeypatch.setattr(gpio, "SYSFS_ROOT", tmp_path)

    result = gpio.SysfsBackend(settle_s=0).probe(24)

    assert result.performed
    assert not result.passed
    assert "left unchanged" in result.detail
    assert direction.read_text() == "out"
