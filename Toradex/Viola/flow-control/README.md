# UART and flow-control test

`adapter-test` tests an assembled 803embedded Digi XBee3 adapter on a Toradex Viola with a Colibri iMX6ULL module. The primary test verifies the UART in both directions, including RTS/CTS hardware flow control. An optional diagnostic checks Linux access to the GPIO signals connected through the adapter.

It needs one board. To test the radio link between two boards, use [`../xbee-radio/`][1].

## Quick start

### What the test covers

The default test verifies:

- Colibri transmit to XBee receive
- XBee transmit to Colibri receive
- RTS and CTS hardware flow control
- XBee identity reads (`HV`, `VR`, `SH`, `SL`, `NI`, and `ID`)

It does not test RF communication. The optional GPIO check verifies OS access, not electrical continuity through the adapter.

### Supported hardware

The adapter must be electrically connected to the Viola X9 signals. A populated X9 connector and soldered wires to the same signals are equivalent to the harness.

Supported Colibri iMX6ULL module selections:

| Module                      | `--module` value      |
| --------------------------- | --------------------- |
| 1 GB IT                     | `imx6ull-1gb-it`      |
| 512 MB IT                   | `imx6ull-512mb-it`    |
| 256 MB                      | `imx6ull-256mb`       |
| 512 MB Wi-Fi / Bluetooth    | `imx6ull-512mb-wb`    |
| 512 MB Wi-Fi / Bluetooth IT | `imx6ull-512mb-wb-it` |

All listed modules support the UART test. Non-Wi-Fi modules also check SODIMM 103/ON-SLEEP when GPIO checking is enabled. Only the Wi-Fi variants omit SODIMM 103 because it is not connected on those modules; XBee sleep can still be controlled in software.

### Supported runtimes

The recommended deployment is a standalone Linux/ARMv7 executable. It contains Python and all required Python packages, so the target needs neither Python nor Docker. One executable supports every module in the table above; `--module` selects the module-specific GPIO availability.

The Docker workflow is retained as an alternative for the 1 GB IT module. It does not support the other module variants.

### 1. Build the standalone executable

Run this command on a development computer with Docker Buildx, from the `Toradex/Viola/flow-control` directory:

```sh
docker buildx build \
  --platform linux/arm/v7 \
  --file Dockerfile.native \
  --target export \
  --output type=local,dest=dist-native \
  .
```

The build runs in an ARMv7 container and outputs `dist-native/adapter-test`. Docker is used only for the build and is not required on the target. The executable targets hard-float ARMv7 Linux with glibc 2.36 or newer and uses the target's standard `libz`; these libraries are present on the checked Torizon OS 7.4 image.

### 2. Copy the executable to the target

Replace `user` and `target-host` with the target's SSH username and hostname or IP address:

```sh
ssh user@target-host 'mkdir -p ~/.local/bin'
scp dist-native/adapter-test user@target-host:~/.local/bin/adapter-test
ssh user@target-host 'chmod +x ~/.local/bin/adapter-test'
```

Repeat these commands to update an existing target. Git is not required on the target.

### 3. Run the UART/RTS/CTS test

Pass the value for the installed module from the supported-module table. For example, on the 1 GB IT module:

```sh
ssh user@target-host '~/.local/bin/adapter-test --module imx6ull-1gb-it'
```

For a 512 MB Wi-Fi / Bluetooth module:

```sh
ssh user@target-host '~/.local/bin/adapter-test --module imx6ull-512mb-wb'
```

A successful run exits with status 0 and reports no failures. A failed run exits with status 1 and logs the reason. The user running the test must be able to open the UART; on Torizon OS this normally means membership in the `dialout` group. The default UART/RTS/CTS test does not require root on the checked image.

### Common commands

Run three-wire UART without hardware flow control:

```sh
ssh user@target-host '~/.local/bin/adapter-test --module imx6ull-1gb-it --flow-control none'
```

Use a different serial port or baud rate:

```sh
ssh user@target-host '~/.local/bin/adapter-test --module imx6ull-1gb-it --port /dev/ttymxc2 --baud 115200'
```

Run the optional GPIO-access check:

```sh
ssh -t user@target-host 'sudo ~/.local/bin/adapter-test --module imx6ull-1gb-it --check-gpio'
```

The checked Torizon image restricts the legacy `/sys/class/gpio/export` interface to root, so the GPIO commands use `sudo` and may prompt for the target user's password. This elevated access is not needed when `--check-gpio` is omitted.

On a Wi-Fi module, selecting the correct module omits only SODIMM 103:

```sh
ssh -t user@target-host 'sudo ~/.local/bin/adapter-test --module imx6ull-512mb-wb --check-gpio'
```

Check only selected GPIO signals:

```sh
ssh -t user@target-host 'sudo ~/.local/bin/adapter-test --module imx6ull-1gb-it --check-gpio --gpio-pins SODIMM_135 SODIMM_133'
```

Run `~/.local/bin/adapter-test --help` on the target for the complete option list.

### Docker workflow for the 1 GB IT module

The container can be built directly on a target that has Docker. From the `Toradex/Viola/flow-control` directory on the development computer, package and copy the source:

```sh
tar -czf adapter-test-source.tar.gz Dockerfile .dockerignore README.md pyproject.toml constraints.txt src/adapter_test
scp adapter-test-source.tar.gz user@target-host:~/
ssh user@target-host 'mkdir -p ~/adapter-test-source && tar -xzf ~/adapter-test-source.tar.gz -C ~/adapter-test-source'
```

Build and run it on the target:

```sh
ssh user@target-host 'cd ~/adapter-test-source && docker build --pull -t adapter-test:local .'
ssh user@target-host 'docker run --rm --privileged --device /dev/ttymxc1:/dev/ttymxc1 adapter-test:local --module imx6ull-1gb-it'
```

The container needs `--privileged` because the test inspects the live device tree under `/sys/firmware/devicetree`. To build the ARMv7 container on another computer instead, export and transfer its image:

```sh
docker buildx build --platform linux/arm/v7 --output type=docker,dest=adapter-test-armv7.tar -t adapter-test:local .
scp adapter-test-armv7.tar user@target-host:~/
ssh user@target-host 'docker load -i ~/adapter-test-armv7.tar'
```

### Common failures

- **Serial port not found:** confirm the UART mapping with `dmesg | grep ttymxc`, then use `--port` if necessary.
- **Permission denied:** for the native executable, confirm that the user can access the UART (normally through the `dialout` group). Run the optional legacy sysfs GPIO check with `sudo`. For Docker, also confirm that the device is passed through and the container has the required privileges.
- **Device-tree flow-control failure:** confirm that the live UART node contains both `uart-has-rtscts` and `fsl,dte-mode`.
- **Radio not responding:** check XBee power, seating, baud rate, and UART wiring.
- **RTS/CTS failure:** the harness runs a follow-up diagnostic and reports whether the CTS or RTS path failed.
- **GPIO access failure:** confirm that sysfs GPIO is enabled and that the line is not already configured as an output or claimed by another peripheral.

## Technical reference

### Wiring

The adapter-to-Viola wiring, the UART mapping (`/dev/ttymxc1`) and the GPIO numbers are in the [Viola README][2].

### GPIO diagnostic

The GPIO check is off by default and uses the legacy `/sys/class/gpio` interface. It checks only the adapter-connected GPIOs:

| XBee signal    | SODIMM | Viola X9 | Linux GPIO |
| -------------- | -----: | -------: | ---------: |
| RESET          |    135 |        8 |         24 |
| RSSI           |     98 |        9 |        122 |
| DTR / SLEEP_RQ |    133 |       10 |        110 |
| ON/SLEEP       |    103 |       11 |        118 |

Non-Wi-Fi modules check all four. Wi-Fi modules omit only SODIMM 103/ON-SLEEP. A device tree can assign these pins to other peripherals, so confirm the live pin state before running the diagnostic.

The check establishes only OS accessibility. It does not prove continuity through the adapter. Sysfs GPIO is deprecated. A future libgpiod implementation would require chip and line-offset mappings.

GPIO access requires writable sysfs access. The checked Torizon image requires `sudo` for the native GPIO check. The Docker workflow requires a privileged container.

### UART modes and radio behavior

The default serial settings are `/dev/ttymxc1` at 9600 baud. Override them with `--port`/`$XBEE_PORT` and `--baud`/`$XBEE_BAUD`.

The harness supports XBee API mode (`AP=1` or `AP=2`) and factory-default transparent mode (`AP=0`).

#### Three-wire mode

With `--flow-control none`, the harness opens the UART without flow control and reads the radio identity. An API-mode radio is queried with API frames. For an `AP=0` radio, the harness enters local AT command mode, reads `AP`, `HV`, `VR`, `SH`, `SL`, `NI`, and `ID`, then exits with `CN`.

Three-wire mode does not read or write `D6` or `D7`, and it does not change `AP`. The requests and responses exercise host transmit, radio receive, radio transmit, and host receive.

#### RTS/CTS mode

RTS/CTS is the default (`--flow-control rtscts` or `XBEE_FLOW_CONTROL=rtscts`). The harness:

1. Reads the live device tree and requires `uart-has-rtscts` and `fsl,dte-mode`.
2. Opens the UART without flow control and reads XBee `D6` and `D7`.
3. In API mode, sets `D6=1` and `D7=1` when needed and issues `AC`.
4. In transparent mode, sets `D6=1` and `D7=1` when needed, sets `AP=1`, and exits command mode with `CN` to apply the pending values.
5. Closes the port and reopens it with `CRTSCTS`.
6. Reads `D6`, `D7`, and the radio identity over the new connection.

The harness never issues `WR`, so changes to `D6`, `D7`, and `AP` are not written to flash. A transparent radio returns to its stored `AP=0` setting after a power cycle.

The RTS/CTS connection uses a finite write timeout. A CTS line that blocks transmission fails the check instead of waiting indefinitely. This is not a flow-control load test.

#### Per-wire flow-control diagnostic

If RTS/CTS verification fails, the harness automatically isolates the two flow-control paths:

- `flow-control-wire:cts`: XBee CTS# to Colibri UART_B_CTS, SODIMM 32 / X9.39
- `flow-control-wire:rts`: Colibri UART_B_RTS, SODIMM 34 / X9.38, to XBee RTS#

It drives one end, confirms by read-back that the driven end changed, and senses the other end. Each result is:

- `PASS (OK)`: the sensed end tracked the driven end.
- `FAIL (OPEN or misrouted)`: the sensed end did not follow.
- `FAIL (INVERTED)`: the sensed end followed with reversed polarity.
- `FAIL (could not test this wire)`: the test could not establish ground truth and reports the exact reason.

The diagnostic runs in API mode and requires access to the UART. When run in Docker, it requires the same privileged-container access as the default RTS/CTS test.

### Target OS and device-tree state

The checked system is Torizon OS Upstream 7.4.0 with kernel 6.6.108. It reports `ID=torizon-upstream`, uses OSTree, and does not provide `/boot/overlays.txt`. Toradex does not provide a Viola-specific DTB for this module; the checked system uses `imx6ull-colibri-emmc-eval-v3.dtb`, with UART2 pin multiplexing defined at the module level.

The live `uart2` node declares `uart-has-rtscts` and `fsl,dte-mode`. Its `uart2grp` pinctrl group muxes all four UART2 pads, including `UART2_CTS_B` on SODIMM 32 and `UART2_RTS_B` on SODIMM 34, at mux mode 0. DTE mode makes UART_B_RTS/SODIMM 34 a module output. It is driven by default whether or not the port is opened with `CRTSCTS`.

The live-device-tree check is read-only. A missing property or unreadable state fails the run before the harness changes the XBee. The harness does not alter the operating system or device tree. These observations apply to the checked image; verify other images independently.

### Implementation

The project uses Digi's official [`digi-xbee`][3] library to encode and decode API frames. The radio logic is carrier-independent; the GPIO map is specific to the Viola and Colibri iMX6ULL.

- `src/adapter_test/radio.py` - XBee configuration, UART probing, and flow-control diagnostics
- `src/adapter_test/gpio.py` - sysfs GPIO access
- `src/adapter_test/config.py` - configuration and adapter-connected GPIO map
- `src/adapter_test/checks.py` - test ordering and result reporting
- `src/adapter_test/logging_setup.py` - stdout and syslog configuration

`Dockerfile.native` builds the standalone ARMv7 executable with PyInstaller and exports only that file. `Dockerfile` builds the optional 1 GB IT container, which logs to stdout for `docker logs`/journald.

### Development

Create a development virtual environment from the `Toradex/Viola/flow-control` directory.

Linux/macOS:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]" -c constraints-dev.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]" -c constraints-dev.txt
```

Run the checks after activating the environment:

```sh
pytest
ruff check .
mypy
```

Run `deactivate` to leave the environment. Tools can also be invoked through the virtual environment's full executable path.

### License

This project is licensed under the MIT License. See the repository's [`LICENSE`][4].

[1]: ../xbee-radio/
[2]: ../README.md#wiring-803-001-001-b
[3]: https://pypi.org/project/digi-xbee/
[4]: ../../../LICENSE
