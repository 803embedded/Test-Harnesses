#!/usr/bin/env sh
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC
#
# Copy xbee-link to one or more targets and build its container there.
#
#   ./deploy.sh 192.0.2.10 192.0.2.11
#   ./deploy.sh -u myuser 192.0.2.10
#   ./deploy.sh otheruser@192.0.2.10
#
# A host without "user@" uses the -u user (default: torizon, or $XBEE_LINK_USER).
# Needs ssh, scp and tar on this computer (Linux, macOS, or Git Bash on
# Windows) and Docker with Compose v2 on each target. The source goes to
# ~/xbee-link on the target; set XBEE_LINK_DIR to use another directory.
set -eu

usage() {
    echo "usage: $0 [-u user] host [host ...]" >&2
    exit 2
}

user="${XBEE_LINK_USER:-torizon}"
while getopts "u:h" opt; do
    case "$opt" in
        u) user="$OPTARG" ;;
        *) usage ;;
    esac
done
shift $((OPTIND - 1))
[ "$#" -gt 0 ] || usage

cd "$(dirname "$0")"
remote_dir="${XBEE_LINK_DIR:-xbee-link}"
archive="$(mktemp)"
trap 'rm -f "$archive"' EXIT

tar -czf "$archive" --exclude='__pycache__' \
    Dockerfile .dockerignore compose.yaml examples src/xbee_link

for host in "$@"; do
    case "$host" in
        *@*) target="$host" ;;
        *) target="$user@$host" ;;
    esac
    echo "==> $target"
    scp -q "$archive" "$target:/tmp/xbee-link.tar.gz"
    ssh "$target" "set -e
        rm -rf '$remote_dir/src' && mkdir -p '$remote_dir'
        tar -xzf /tmp/xbee-link.tar.gz -C '$remote_dir' && rm -f /tmp/xbee-link.tar.gz
        cd '$remote_dir' && docker compose build xbee-link"
done
