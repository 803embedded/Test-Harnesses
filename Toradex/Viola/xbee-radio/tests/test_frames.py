# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

import pytest

from xbee_link import frames


def test_encode_matches_known_at_frame() -> None:
    # ATAP with frame ID 1, from the XBee user guide checksum rules.
    assert frames.encode(frames.at_command(1, "AP")) == bytes.fromhex("7E00040801415065")


def test_escaped_encoding_escapes_special_bytes() -> None:
    data = bytes((0x10, 0x01)) + bytes((0x7E, 0x7D, 0x11, 0x13))
    encoded = frames.encode(data, escaped=True)
    assert encoded[0] == frames.START
    assert frames.START not in encoded[1:]
    parser = frames.FrameParser(escaped=True)
    assert parser.feed(encoded) == [data]


@pytest.mark.parametrize("escaped", [False, True])
def test_parser_handles_split_input_and_noise(escaped: bool) -> None:
    payloads = [bytes((0x90,)) + bytes(range(i, i + 20)) for i in (0, 0x70, 0x7A)]
    stream = b"\x00\x13junk" + b"".join(frames.encode(p, escaped=escaped) for p in payloads)
    parser = frames.FrameParser(escaped=escaped)
    out: list[bytes] = []
    for i in range(len(stream)):
        out += parser.feed(stream[i : i + 1])
    assert out == payloads
    assert parser.discarded_bytes >= 6


def test_unescaped_parser_resyncs_after_bad_checksum() -> None:
    good = frames.encode(b"\x8a\x00")
    bad = bytearray(frames.encode(b"\x8a\x06"))
    bad[-1] ^= 0xFF
    parser = frames.FrameParser()
    assert parser.feed(bytes(bad) + good) == [b"\x8a\x00"]
    assert parser.checksum_errors == 1


def test_decode_frames() -> None:
    at = frames.decode(bytes.fromhex("880141500001"))
    assert at == frames.AtResponse(1, "AP", 0, b"\x01")

    status = frames.decode(bytes.fromhex("8B05FFFE007400"))
    assert isinstance(status, frames.TransmitStatus)
    assert status.status == 0x74 and not status.ok

    legacy = frames.decode(bytes.fromhex("890700"))
    assert legacy == frames.TransmitStatus(7, 0)

    rx64 = frames.decode(bytes.fromhex("80") + bytes.fromhex("0013A20012345678") + b"\x26\x02hi")
    assert isinstance(rx64, frames.ReceivedPacket)
    assert rx64.source64 == 0x0013A20012345678
    assert rx64.rssi_dbm == -38 and rx64.broadcast and rx64.data == b"hi"

    rx90 = frames.decode(bytes.fromhex("90") + b"\xff" * 8 + b"\x12\x34\x00data")
    assert isinstance(rx90, frames.ReceivedPacket)
    assert rx90.source64 is None and rx90.source16 == 0x1234

    assert frames.decode(b"\x8a\x00") == frames.ModemStatus(0)
    assert isinstance(frames.decode(b"\x95abc"), frames.UnknownFrame)

    with pytest.raises(frames.FrameError):
        frames.decode(b"\x88\x01")


def test_builders_and_values() -> None:
    tx = frames.transmit_request(3, 0x0013A20012345678, b"x")
    assert tx == bytes.fromhex("10030013A20012345678FFFE0000") + b"x"
    assert frames.tx64_request(3, 1, b"x") == bytes.fromhex("00030000000000000001") + b"\x00x"
    assert frames.encode_value(0) == b"\x00"
    assert frames.encode_value(0xFFFE) == b"\xff\xfe"
    assert frames.encode_value("node") == b"node"
    with pytest.raises(frames.FrameError):
        frames.encode_value(-1)
