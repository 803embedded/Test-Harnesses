#!/usr/bin/env sh
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC
#
# Run a two-board RF link test from this computer.
#
#   ./remote-test.sh RX_HOST TX_HOST                    # 300 seconds
#   ./remote-test.sh -d 1800 192.0.2.11 192.0.2.10      # 30 minutes
#   ./remote-test.sh -s 10M 192.0.2.11 192.0.2.10       # until 10 MiB are verified
#
# RX_HOST receives and TX_HOST sends. The receiver's radio address is read
# automatically. Both boards need xbee-link deployed first (./deploy.sh).
# The sender's live diagnostics stream to this terminal; the receiver's
# summary is printed at the end. Ctrl-C stops both sides.
#
# The sender always transmits at full rate. Give -d or -s, not both
# (default -d 300). With -s the expected run time is printed after a few
# seconds of measurement; Ctrl-C aborts.
#   -d SECONDS  transmit for this long
#   -s SIZE     transmit until this much data is verified, e.g. 512K, 10M
#   -u USER     SSH user for hosts given without "user@" (default torizon,
#               or $XBEE_LINK_USER)
#
# Exit status: 0 when both sides report PASS, 1 otherwise, 2 for usage errors.
set -eu

usage() {
    echo "usage: $0 [-d seconds | -s size] [-u user] rx_host tx_host" >&2
    exit 2
}

duration=""
total=""
user="${XBEE_LINK_USER:-torizon}"
while getopts "d:s:u:h" opt; do
    case "$opt" in
        d) duration="$OPTARG" ;;
        s) total="$OPTARG" ;;
        u) user="$OPTARG" ;;
        *) usage ;;
    esac
done
shift $((OPTIND - 1))
[ "$#" -eq 2 ] || usage
if [ -n "$duration" ] && [ -n "$total" ]; then
    echo "error: give either -d (seconds) or -s (size), not both" >&2
    exit 2
fi
if [ -n "$total" ]; then
    case "$total" in
        ''|*[!0-9.KkMmGgIiBb]*) echo "error: -s needs a size such as 512K or 10M" >&2; exit 2 ;;
    esac
    amount="--total $total"
    describe="until $total are verified"
else
    duration="${duration:-300}"
    case "$duration" in
        *[!0-9.]*) echo "error: -d needs a number of seconds" >&2; exit 2 ;;
    esac
    amount="--duration $duration"
    describe="for $duration s"
fi

target() {
    case "$1" in
        *@*) echo "$1" ;;
        *) echo "$user@$1" ;;
    esac
}

rx="$(target "$1")"
tx="$(target "$2")"
dir="${XBEE_LINK_DIR:-xbee-link}"
compose="cd '$dir' && docker compose"
# Every service is behind a profile; `down` only removes those it is given.
down="docker compose --profile rx --profile tx --profile cli down >/dev/null 2>&1"

# Make sure nothing is left running on the boards, however this script ends.
cleaned=""
cleanup() {
    [ -n "$cleaned" ] && return
    cleaned=1
    echo "==> cleaning up containers on both boards" >&2
    ssh -n "$tx" "docker stop xbee-link-tx-run >/dev/null 2>&1 || true" || true
    ssh -n "$rx" "$compose stop rx >/dev/null 2>&1; $down" || true
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM HUP

echo "==> reading the receiver's radio address on $rx"
peer="$(ssh -n "$rx" "$compose run --rm xbee-link info" \
    | sed -n 's/.*64-bit address  *\([0-9A-F]\{16\}\).*/\1/p' | head -n 1)"
if [ -z "$peer" ]; then
    echo "error: could not read the radio address on $rx (run: ssh $rx '$compose run --rm xbee-link info')" >&2
    exit 1
fi
echo "    receiver radio: $peer"

echo "==> starting the receiver on $rx"
ssh -n "$rx" "$compose version >/dev/null && { $down; docker compose up -d rx; }"
# The radio starts from its saved settings each run, so setup can take a while.
attempts=0
until ssh -n "$rx" "docker logs xbee-link-rx 2>&1" | grep -q "listening on"; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 45 ]; then
        echo "error: the receiver on $rx did not become ready" >&2
        ssh -n "$rx" "docker logs --tail 10 xbee-link-rx 2>&1" >&2 || true
        exit 1
    fi
    sleep 1
done

echo "==> sending from $tx at full rate $describe (Ctrl-C stops it)"
tx_status=0
# -tt gives the remote run a terminal, so Ctrl-C reaches the sender, which
# then stops cleanly and prints its summary; it also ends the run if this
# connection drops.
ssh -tt "$tx" "$compose run --rm --name xbee-link-tx-run xbee-link \
    tx --peer $peer $amount" || tx_status=$?

echo "==> stopping the receiver on $rx"
ssh -n "$rx" "$compose stop rx >/dev/null 2>&1" || true
rx_log="$(ssh -n "$rx" "docker logs xbee-link-rx 2>&1" || true)"
cleanup
echo "$rx_log" | sed -n '/summary/,$p'

if [ "$tx_status" -eq 0 ] && echo "$rx_log" | grep -q "RESULT: PASS" \
    && ! echo "$rx_log" | grep -q "could not restore"; then
    echo "==> LINK TEST PASS"
    exit 0
fi
echo "==> LINK TEST FAIL (sender exit $tx_status)"
exit 1
