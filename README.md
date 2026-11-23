# 803embedded test harnesses

This repository contains hardware test harnesses for 803embedded adapter boards and their supported host platforms.

These tools are not general-purpose adapter testers. Each one is built for one 803embedded adapter on the host hardware listed for it, and accepts only the module variants it lists. Other adapters, carriers and modules are not supported.

## Available harnesses

| Platform | Carrier | Supported module | Directory             |
| -------- | ------- | ---------------- | --------------------- |
| Toradex  | Viola   | Colibri iMX6ULL  | [`Toradex/Viola/`][1] |

### Toradex Viola

| Tool                       | Directory                          | What it tests                                                      |
| -------------------------- | ---------------------------------- | ------------------------------------------------------------------ |
| UART and flow-control test | [`Toradex/Viola/flow-control/`][2] | UART, RTS/CTS and XBee identity on one board; optional GPIO wiring |
| XBee radio link test       | [`Toradex/Viola/xbee-radio/`][3]   | Data integrity and supply voltage under transmit load, two boards  |

## Clone

```sh
git clone https://github.com/803embedded/Test-Harnesses.git
cd Test-Harnesses
```

## License

This project is licensed under the MIT License. See [`LICENSE`][4].

[1]: Toradex/Viola/
[2]: Toradex/Viola/flow-control/
[3]: Toradex/Viola/xbee-radio/
[4]: LICENSE
