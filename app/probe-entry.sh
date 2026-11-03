#!/bin/sh
# Start of a probe container (see probe.py). Runs as root only to get the DHCP lease, then runs the probe as the
# unprivileged app user. Nothing in the container listens on a port.
set -e
IF="${PROBE_IF:-eth0}"
DIR="${PROBE_DIR:-/probes}"
mkdir -p "$DIR" && chown app:app "$DIR"
# Docker gave the interface a placeholder address; drop it so only the DHCP lease is ever used
ip -4 addr flush dev "$IF"
ip -4 route flush dev "$IF" 2>/dev/null || true
# udhcpc keeps running in the background and renews the lease; -b = keep trying if the first try fails.
# IF_PEER_DNS=no: leave resolv.conf alone (the probe only uses IP addresses, and the file system is read-only)
IF_PEER_DNS=no udhcpc -i "$IF" -b -t 5 -T 3 -A 10 -x hostname:"${PROBE_HOSTNAME:-wan-probe}"
exec su-exec app python /app/probe.py
