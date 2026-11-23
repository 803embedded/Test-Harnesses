# Toradex Viola

These are test tools for the 803embedded 803-001-001 Digi XBee adapter on a Toradex Viola carrier with a Colibri iMX6ULL module.

## Tools

| Directory            | Tool           | What it tests                                                                   | Boards |
| -------------------- | -------------- | ------------------------------------------------------------------------------- | -----: |
| [`flow-control/`][1] | `adapter-test` | UART both ways, RTS/CTS flow control, XBee identity; optional GPIO wiring test  |      1 |
| [`xbee-radio/`][2]   | `xbee-link`    | RF link: data integrity, throughput, XBee supply voltage at full transmit power |      2 |

To check a newly assembled adapter, first run `flow-control` on each board, then run `xbee-radio` between two boards. Each directory has its own README with build, deployment and usage instructions.

Both tools use the XBee UART, and only one program can open it at a time. Stop one tool, and any other software using `/dev/ttymxc1`, before starting the other.

## Wiring: 803-001-001

| XBee signal      | XBee pin | Adapter pin | Viola X9 pin | X9 signal (I/O)   | SODIMM | iMX6ULL                              |
| ---------------- | -------: | ----------- | -----------: | ----------------- | -----: | ------------------------------------ |
| VCC (+3.3 V)     |        1 | J1.4        |            4 | +3.3 V            |      - | 3.3 V rail                           |
| GND              |       10 | J1.7, J2.6  |        7, 40 | GND               |      - | -                                    |
| DIN (module RX)  |        3 | J2.2        |           36 | UART_B_TXD (O)    |     38 | UART2                                |
| DOUT (module TX) |        2 | J2.3        |           37 | UART_B_RXD (I)    |     36 | UART2                                |
| CTS#             |       12 | J2.5        |           39 | UART_B_CTS (I)    |     32 | UART2                                |
| RTS#             |       16 | J2.4        |           38 | UART_B_RTS (O)    |     34 | UART2                                |
| RESET#           |        5 | J1.8        |            8 | SODIMM_135 (GPIO) |    135 | gpio 24                              |
| RSSI             |        6 | J1.9        |            9 | SODIMM_98 (GPIO)  |     98 | gpio 122                             |
| DTR# / SLEEP_RQ  |        9 | J1.10       |           10 | SODIMM_133 (GPIO) |    133 | gpio 110                             |
| ON/SLEEP#        |       13 | J1.11       |           11 | SODIMM_103 (GPIO) |    103 | gpio 118                             |
| AD0 / DIO0       |       20 | -           |            - | -                 |      - | 10k pull-down (R1), not routed to X9 |

The GPIO numbers are global sysfs line numbers confirmed on the Colibri iMX6ULL/Viola target with `sudo cat /sys/kernel/debug/gpio`. GPIO bank bases on the checked image are 0, 32, 64, 96, and 128.

The XBee UART is Colibri UART_B, implemented by iMX6ULL UART2. On the checked image it is `/dev/ttymxc1`; UART_A/UART1/`/dev/ttymxc0` is the debug console. Confirm this on another image with `dmesg | grep ttymxc` or the device-tree aliases.

## License

This project is licensed under the MIT License. See the repository's [`LICENSE`][3].

[1]: flow-control/
[2]: xbee-radio/
[3]: ../../LICENSE
