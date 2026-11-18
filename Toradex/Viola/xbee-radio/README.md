# XBee radio link test

`xbee-link` sends data between two XBee radios and checks that it arrives intact. It needs two boards. To test the UART and RTS/CTS wiring of a single adapter, use [`../flow-control/`][1].

It is a small Python package with no third-party dependencies, in three layers:

- **Driver:** XBee API frames (`AP=1`/`AP=2`), a POSIX serial port with RTS/CTS, AT commands, transmit/receive, and reliable transfer of any byte buffer.
- **Config scaffolding:** a TOML file that names the serial port and the AT settings. On start, the radio is found, configured and checked.
- **Test scaffolding:** a command-line link test with live diagnostics (throughput, RSSI, XBee supply voltage) and a PASS/FAIL summary. You can delete it to embed the driver elsewhere.

It runs in a container on the target, and with the system `python3` (3.11 or newer) on any Linux host.

The reference setup is two Toradex Viola carriers, each with a Colibri iMX6ULL, an 803-001-001 adapter and an XBee3 802.15.4 radio on `/dev/ttymxc1`. The adapter wiring is in the [Viola README][2].

## Quick start (containers)

You need Docker with Compose v2 on each target (Torizon OS includes both). Docker is not needed on your computer. The scripts below need `ssh`, `scp` and `tar` on your computer (Linux, macOS, or Git Bash on Windows) and passwordless SSH to the boards.

The examples use `192.0.2.10` (sender) and `192.0.2.11` (receiver); replace them with your boards' IP addresses. Hosts without `user@` log in as `torizon`; change that with `-u USER` or `$XBEE_LINK_USER`.

### 1. Deploy and build

From this directory:

```sh
./deploy.sh 192.0.2.10 192.0.2.11
```

This copies the source to `~/xbee-link` on each board and builds the `xbee-link:local` image there. Run it again after you change the source or the config.

The manual equivalent, which also works from Windows PowerShell:

```sh
tar -czf xbee-link.tar.gz Dockerfile .dockerignore compose.yaml examples src/xbee_link
scp xbee-link.tar.gz torizon@192.0.2.10:/tmp/
ssh torizon@192.0.2.10 'mkdir -p ~/xbee-link && tar -xzf /tmp/xbee-link.tar.gz -C ~/xbee-link && cd ~/xbee-link && docker compose build xbee-link'
```

### 2. Run a link test

Give the receiver first, then the sender:

```sh
./remote-test.sh 192.0.2.11 192.0.2.10            # 5 minutes (default)
./remote-test.sh -d 1800 192.0.2.11 192.0.2.10    # 30 minutes
./remote-test.sh -s 10M 192.0.2.11 192.0.2.10     # until 10 MiB are verified
```

The sender always transmits continuously at full power and as fast as the link allows. On the reference setup that is about 8.9 KiB/s, or roughly 31 MiB per hour. You choose when it stops:

- **`-d SECONDS`:** stop after this long. This is the default (`-d 300`).
- **`-s SIZE`:** stop once this much data has been verified, however long that takes.

Give one or the other; giving both is an error. With `-s`, the sender measures the real rate for 5 seconds, then prints how long the run will take and when it will finish. If the run will take longer than 10 minutes, the line starts with `WARNING:`. Progress lines then show the time left:

```text
WARNING: measured 8.6 KiB/s: 20.0 MiB will take about 39 min 36 s in total (done around 03:45); press Ctrl-C or `docker stop` to abort
tx 7: 105.8 KiB / 256.0 KiB ( 41%)  8.7 KiB/s  Vcc 3.371 V  31 C  | 1.6 MiB / 20.0 MiB, 35 min 45 s left
```

Press Ctrl-C to abort at any time. The sender stops cleanly and prints its summary, the script stops the receiver and removes both containers, and the run is reported as FAIL ("aborted after … verified"). The script also cleans up both boards if the SSH connection drops.

The script then:

1. Reads the receiver's radio address.
2. Starts the receiver in the background.
3. Streams the sender's live diagnostics to your terminal.
4. Prints the receiver's summary.
5. Removes both containers.

It exits 0 only if both sides report PASS.

The sender prints progress every 2 seconds and a summary at the end. This output is from a 10-minute run on the reference setup (`-d 600`, with `--transfer-size 512K`):

```text
tx 1: 183.1 KiB / 512.0 KiB ( 36%)  9.1 KiB/s  Vcc 3.373 V  31 C
...
tx 11: peer verified 512.0 KiB (CRC32 OK) in 58.0 s, 8.8 KiB/s, 5462 frames, 0 retransmitted
==== xbee-link tx summary ====
  local / peer      0013A20087654321 / 0013A20012345678
  transfers         11/11 verified
  data              5.5 MiB in 636.2 s (8.9 KiB/s)
  RF frames         60082 sent, 0 retransmitted
  XBee supply       min 3.335 V, max 3.379 V (limit 3.00 V)
  radio resets      0
  UART checksum err 0
  RESULT: PASS
```

### Running each side by hand

To watch both sides live, run each in its own terminal. Check a radio and read its address:

```sh
ssh torizon@192.0.2.11 'cd ~/xbee-link && docker compose run --rm xbee-link info'
```

Start the receiver, then the sender with the receiver's address:

```sh
ssh -t torizon@192.0.2.11 'cd ~/xbee-link && docker compose up rx'
ssh -t torizon@192.0.2.10 'cd ~/xbee-link && XBEE_PEER=0013A20012345678 TX_DURATION=1800 docker compose up tx'
```

Set either `TX_DURATION` (seconds) or `TX_TOTAL` (bytes, for example `TX_TOTAL=10M`), not both. With neither, the sender runs for 300 seconds. On the command line these are `xbee-link tx --duration S` and `xbee-link tx --total SIZE`. To send one specific file instead, use `xbee-link tx --file PATH`.

The stream is split into transfers. Each transfer is checked with CRC32 and confirmed by the receiver before the next one starts, so a fault shows up within seconds rather than only at the end. The last transfer is shortened so the run ends on time (`--duration`) or on the exact size (`--total`). `--transfer-size` (default 256K) only sets how often this check happens; it does not change the transmit rate or the RF load.

### Start and stop

The containers are started only when you ask for them (`restart: "no"`).

| Action                         | Command (in `~/xbee-link` on the board)         |
| ------------------------------ | ----------------------------------------------- |
| Run in the foreground          | `docker compose up rx` (Ctrl-C stops it)        |
| Run in the background          | `docker compose up -d rx`                       |
| Follow background logs         | `docker logs -f xbee-link-rx`                   |
| Stop                           | `docker compose stop rx` (prints the summary)   |
| Remove the stopped containers  | `docker compose --profile rx --profile tx down` |
| One-off command, any arguments | `docker compose run --rm xbee-link tx --help`   |

Replace `rx` with `tx` for the sender. Stopping a container sends `SIGTERM`. The test then ends cleanly and prints its summary. Only one program can use the UART at a time, so stop any other container that opens `/dev/ttymxc1` first.

Radio settings are volatile by default. A power cycle restores the radio's saved configuration, so the other software sees the radio as it was.

## What the test checks

- **Data integrity:** every transfer is reassembled and checked with CRC32 on the receiver. The receiver reports back, so the sender's PASS means verified end to end.
- **Supply under load:** both sides read the XBee's own VCC measurement (`AT%V`) every 2 seconds. The run fails if it drops below `--min-supply-mv` (default 3000 mV; `0` disables the check).
- **Brownouts:** a radio reset during the run (a modem-status "hardware reset" or "watchdog reset" frame) fails the run. A reset also clears the volatile settings.
- **Link quality:** RSSI, delivery failures, retransmissions, duplicates and UART checksum errors are reported.

For the worst-case supply current, the example config sets maximum transmit power (`PL = 4`). With `--window 4` (the default) the sender keeps the radio transmitting almost continuously. On the reference setup it moves about 8.9 KiB/s, or about 94 frames per second.

## Configuration

The container uses [`examples/viola-imx6ull.toml`][3], mounted read-only. Edit it and restart the container; no rebuild is needed. To use another file, set `XBEE_LINK_CONFIG_FILE=/path/to/file.toml`.

```toml
[serial]
port = "/dev/ttymxc1"
baud = 9600               # tried first; other common rates are probed
flow_control = "rtscts"   # or "none" for 3-wire

[radio]
api_mode = 1              # AP=1 or AP=2
write = false             # true saves the settings with ATWR

[radio.at]                # any settable AT command; applied, then read back
BD = 8                    # 230400 baud
D6 = 1
D7 = 1
MY = 0xFFFE
CH = 0x0C
ID = 0x3332
PL = 4

[link]
# peer = "0013A20012345678"   # default tx destination
```

On start, `xbee-link`:

1. Finds the radio. It tries `[serial] baud`, the configured `BD` rate, and then the other common rates. It tries API mode first, then transparent mode (`+++`). A transparent radio is switched to API mode.
2. Queues every `[radio.at]` setting and applies them together with `AC`.
3. Switches the host UART to the new baud rate and flow control.
4. Reads every setting back and fails on any mismatch. With RTS/CTS, a wiring fault shows up here as a timeout.

Settings to know about:

- **`D6 = 1`, `D7 = 1`:** XBee RTS and CTS flow control. These are required with `flow_control = "rtscts"`; the 803-001-001 adapter and the Viola X9 connector route both lines.
- **`BD = 8` (230400):** at 115200 baud the receiving radio's UART cannot keep up with back-to-back RF frames. It then drops frames it has already acknowledged over the air. The transfer protocol recovers them, but at 230400 none are lost.
- **`MY = 0xFFFE`:** use 64-bit source addresses, so the receiver knows who sent a packet and can reply. With the factory `MY = 0` the receiver cannot report back.
- **`CH`, `ID`:** both radios must match.
- **`PL`:** transmit power, 0 to 4. Lower it for everyday use; keep it at 4 to load-test the supply.

Command-line options override the config: `--port`, `--peer` (or `$XBEE_PEER`), `--no-setup` (use the radio as it is, at `[serial] baud`), and `--json PATH` (also write the result as JSON). Run `xbee-link --help` or `xbee-link tx --help` for the full list.

## Using the driver in your own code

Copy `src/xbee_link` into your project. Delete `linktest.py` and `__main__.py` if you don't need the test.

```python
from xbee_link import BufferReceiver, LinkConfig, open_radio, send_buffer

config = LinkConfig.load("config.toml")
xbee = open_radio(config)                      # find, configure and verify

# Sender
result = send_buffer(xbee, 0x0013A20012345678, payload)
print(result.throughput_bps, result.retransmissions)

# Receiver (keep one BufferReceiver per radio)
receiver = BufferReceiver(xbee)
received = receiver.receive(start_timeout=60)
print(received.source64, len(received.data))
```

Lower-level use, without the config file:

```python
from xbee_link.serial_port import PosixSerial
from xbee_link.xbee import XBee

xbee = XBee(PosixSerial("/dev/ttymxc1", 230400, "rtscts"))
xbee.at("CH", 0x0C)                            # set a parameter
print(xbee.supply_voltage_mv())
xbee.send(0x0013A20012345678, b"hello")        # one RF frame, waits for delivery
packet = xbee.receive(timeout=5)               # ReceivedPacket or None
```

`XBee` accepts any object with the `Transport` methods in [`serial_port.py`][4], for example a thin pyserial wrapper on a non-Linux host.

### Modules

| Module           | Layer  | Contents                                                    |
| ---------------- | ------ | ----------------------------------------------------------- |
| `frames.py`      | driver | API frame encode/decode, escaping, incremental parser       |
| `serial_port.py` | driver | `Transport` interface and a termios `PosixSerial`           |
| `xbee.py`        | driver | `XBee`: AT commands, transmit/receive, delivery status      |
| `transfer.py`    | driver | `send_buffer`, `BufferReceiver`: chunked, verified transfer |
| `config.py`      | config | TOML loading and validation                                 |
| `radio_setup.py` | config | `open_radio`: detect, configure, verify; `read_info`        |
| `linktest.py`    | test   | supply monitor, results, summary and JSON output            |
| `__main__.py`    | test   | the `xbee-link` command line                                |

### Transfer protocol

A buffer is split into chunks of `NP − 6` bytes (96 bytes on the reference radios). Each chunk is a unicast frame, so the radio's MAC acknowledgement and retries apply. The sender keeps `window` frames in flight and retransmits a chunk whose delivery status fails. After the last chunk, it asks the receiver for a report. The receiver checks the CRC32 and either confirms the buffer or lists the chunks it is missing, and the sender resends those. Packets that don't start with a protocol type byte (`0xB1`–`0xB4`) are ignored, so other traffic can share the radio. The wire format is documented in [`transfer.py`][5].

Throughput on the reference setup:

| UART baud | Window | Throughput | Note                                               |
| --------: | -----: | ---------: | -------------------------------------------------- |
|    115200 |      1 |  3.3 KiB/s | waits for each delivery status                     |
|    115200 |      4 |  5.8 KiB/s | receiving radio drops some ACKed frames; recovered |
|    230400 |      4 |  8.9 KiB/s | no losses; 10-minute soak, 60082 frames            |

## Running without a container

The target needs Python 3.11 or newer; no packages are required.

```sh
scp -r src/xbee_link examples/viola-imx6ull.toml user@target:~/xbee-link/
ssh user@target 'cd ~/xbee-link && python3 -m xbee_link -c viola-imx6ull.toml info'
```

The user must be able to open the UART (on Torizon OS, membership in the `dialout` group).

## Development

```sh
python -m venv .venv
. .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
ruff check .
mypy
```

The tests run without hardware. `tests/fake_radio.py` simulates a pair of linked radios, including baud-rate mismatches, packet loss and acknowledged-but-lost frames.

## License

This project is licensed under the MIT License. See the repository's [`LICENSE`][6].

[1]: ../flow-control/
[2]: ../README.md#wiring-803-001-001-b
[3]: examples/viola-imx6ull.toml
[4]: src/xbee_link/serial_port.py
[5]: src/xbee_link/transfer.py
[6]: ../../../LICENSE
