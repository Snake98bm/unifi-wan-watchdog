#!/bin/sh
# Install the wan-watchdog "bounce" access on a UniFi Dream Machine. Run as root on the gateway, passing the
# watchdog's PUBLIC key (data/ssh/id_ed25519.pub), the gateway's WAN interfaces and (optional) the tunnel
# interfaces of the UniFi OpenVPN clients the watchdog may reconnect:
#   ssh root@<gateway> "WATCHDOG_PUBKEY='ssh-ed25519 AAAA... wan-watchdog' WAN_IFACES='eth8 eth9' VPN_IFACES='tunovpnc1' sh" < install-bounce.sh
# Windows PowerShell:
#   Get-Content install-bounce.sh -Raw | ssh root@<gateway> "tr -d '\r' | WATCHDOG_PUBKEY='...' WAN_IFACES='eth8 eth9' VPN_IFACES='tunovpnc1' sh"
# Undo: run it with "sh -s remove" instead of "sh" (WATCHDOG_PUBKEY is still needed to find the key line).
#
# What it does:
#  - writes /data/wan-watchdog/wan-bounce.sh: the ONLY thing the watchdog's SSH key can run.
#    It accepts "check", "bounce <if>" (verified link drop), "bounce-long <if>" (link down 5 s, then up)
#    and "wanlog <if>" (read-only: recent syslog lines about that port), only for the interfaces in
#    WAN_IFACES, plus "vpnrestart <tun>" (reconnect that OpenVPN client) only for the ones in VPN_IFACES.
#    Anything else is refused. Every request is logged to syslog with the tag wan-watchdog.
#  - adds the watchdog's public key to /root/.ssh/authorized_keys, locked to that script
#    (command=..., no shell, no pty, no forwarding). Your own keys are left alone.
# /data and /root survived a firmware update on a UDM SE. If an update ever drops the key, the watchdog logs it
# and falls back to the PoE cycle; run this script again to restore it.
set -e
D=/data/wan-watchdog
AK=/root/.ssh/authorized_keys
KEY="${WATCHDOG_PUBKEY:?set WATCHDOG_PUBKEY to the watchdog's public key (data/ssh/id_ed25519.pub)}"
case "$KEY" in ssh-ed25519\ AAAA*|ssh-rsa\ AAAA*|ecdsa-sha2-*\ AAAA*) ;; *) echo "WATCHDOG_PUBKEY isn't an SSH public key"; exit 1 ;; esac
TAG=$(echo "$KEY" | awk '{print $3}')
[ -n "$TAG" ] || { echo "WATCHDOG_PUBKEY needs a comment at the end (e.g. wan-watchdog)"; exit 1; }
IFACES="${WAN_IFACES:-eth8 eth9}"
for i in $IFACES; do case "$i" in eth[0-9]|eth[0-9][0-9]) ;; *) echo "WAN_IFACES: bad interface '$i'"; exit 1 ;; esac; done
VPNS="${VPN_IFACES:-}"
for i in $VPNS; do case "$i" in tun*[!a-z0-9]*|tun) echo "VPN_IFACES: bad interface '$i'"; exit 1 ;; tun*) ;;
  *) echo "VPN_IFACES: bad interface '$i'"; exit 1 ;; esac; done

# every authorized_keys line except the watchdog's (its key comment is the last field; exact match, no regex,
# and awk succeeds even when nothing is left)
others() { awk -v t="$TAG" '$NF != t' "$AK"; }

if [ "$1" = "remove" ]; then
  if [ -f "$AK" ]; then
    others > "$AK.tmp"
    cat "$AK.tmp" > "$AK"
    rm -f "$AK.tmp"
  fi
  rm -rf $D
  echo "Removed: watchdog key and $D"
  exit 0
fi

mkdir -p $D
cat > $D/wan-bounce.sh <<'EOS'
#!/bin/sh
# Forced command for the wan-watchdog SSH key.
# Allowed: check | bounce <if> | bounce-long <if> | wanlog <if> | vpnrestart <tun>
# bounce: renegotiate the link (ethtool -r) and VERIFY it dropped and came back (carrier_changes +2
# and carrier up). If it didn't drop, or didn't come back within 15 s, try link down 5 s / up once.
# Takes at most ~45 s. Exit codes: 0 = verified, 2 = refused, 3 = link not back up, 4 = never dropped.
ALLOWED="eth8 eth9"   # replaced by WAN_IFACES when installed
VPN_ALLOWED=""        # replaced by VPN_IFACES when installed (empty = vpnrestart is always refused)
set -- $SSH_ORIGINAL_COMMAND
logger -t wan-watchdog "request from ${SSH_CLIENT%% *}: $SSH_ORIGINAL_COMMAND"
allowed() { for i in $ALLOWED; do [ "$i" = "$1" ] && return 0; done; return 1; }
vpn_allowed() { for i in $VPN_ALLOWED; do [ "$i" = "$1" ] && return 0; done; return 1; }
N=/sys/class/net
changes() { cat $N/$1/carrier_changes 2>/dev/null || echo 0; }
carrier() { cat $N/$1/carrier 2>/dev/null || echo 0; }
# wait up to $3 s until the link changed at least $4 times since $2 and is up again; prints seconds waited
wait_back() { i=0; while [ $i -lt $3 ]; do sleep 1; i=$((i + 1))
  [ "$(changes $1)" -ge $(($2 + $4)) ] && [ "$(carrier $1)" = 1 ] && break; done; echo $i; }
note() { logger -t wan-watchdog "$*"; }   # progress goes to syslog only; stdout is just the result line
report() {  # $1 if, $2 changes before, $3 method, $4 seconds, $5 changes needed (2 = down+up, 1 = up only)
  d=$(($(changes $1) - $2)); up=$(carrier $1)
  msg="$1 $([ "$up" = 1 ] && echo "back UP at $(cat $N/$1/speed 2>/dev/null) Mb/s" || echo "still DOWN") after ${4}s ($3)"
  note "$msg, link changed ${d}x"; echo "$msg"
  [ "$up" = 1 ] || exit 3
  [ $d -ge $5 ] || exit 4
  exit 0
}
long() {  # link down 5 s, then up; if the link was already down, coming back up is enough
  b=$(changes $1); need=$([ "$(carrier $1)" = 1 ] && echo 2 || echo 1)
  ip link set "$1" down; sleep 5; ip link set "$1" up
  report "$1" "$b" "link down 5 s" "$(($(wait_back "$1" "$b" 25 $need) + 5))" $need
}
case "$1" in
  check)
    echo "ok $(cat /usr/lib/version)"; exit 0 ;;
  bounce)
    allowed "$2" && [ $# -eq 2 ] || { echo "refused"; exit 2; }
    b=$(changes $2)
    if ethtool -r "$2" >/dev/null 2>&1; then
      s=$(wait_back "$2" "$b" 15 2)
      if [ "$(changes $2)" -ge $((b + 2)) ] && [ "$(carrier $2)" = 1 ]; then
        report "$2" "$b" "renegotiated" "$s" 2
      fi
      # it didn't drop the link, or dropped it and it didn't come back: try link down/up once
      note "renegotiation didn't $([ "$(carrier $2)" = 1 ] && echo "drop the link" || echo "bring the link back") after ${s}s, using link down/up"
    else
      note "renegotiation not supported on $2, using link down/up"   # the UDM SE's switch ports
    fi
    long "$2" ;;
  bounce-long)
    allowed "$2" && [ $# -eq 2 ] || { echo "refused"; exit 2; }
    long "$2" ;;
  wanlog)
    # read-only: the last 200 syslog lines about this WAN port (link, DHCP, failover, monitor failures, bounces),
    # without the routine noise: IPv6 probes, DHCP renewals, monitor "is up", the IPsec resolver, timing warnings
    allowed "$2" && [ $# -eq 2 ] || { echo "refused"; exit 2; }
    NOISE='probe-runner|tunnel-controller: vti|utils-daemon: Routine execution|IPsec transport|DHCP lease [0-9.]+:renew|wman\._dump|dpinger update: [^ ]+ is up'
    cat /var/log/messages.1 /var/log/messages 2>/dev/null \
      | grep -a -E "(^|[^a-z0-9])$2([^0-9]|\$)|wan-failover-group" | grep -a -v "wan-watchdog\[[0-9]*\]: request from" \
      | grep -a -v -E "$NOISE" | tail -n 200 | cut -c1-300
    exit 0 ;;
  vpnrestart)
    # Reconnect a UniFi OpenVPN client. SIGUSR1 makes OpenVPN drop the connection, look the server name up
    # again and reconnect, often to another server (new exit IP). The policy route's kill switch stays in place
    # meanwhile. UniFi's client configs have no persist-tun, so a reconnect re-creates the tunnel device:
    # done = a new device (other ifindex) with an IPv4 address.
    # Exit codes: 0 = reconnected, 2 = refused, 3 = not back within 60 s, 5 = that OpenVPN client isn't running.
    vpn_allowed "$2" && [ $# -eq 2 ] || { echo "refused"; exit 2; }
    cfg=$(grep -l -x -F -e "--dev $2" /etc/openvpn/*/config.* 2>/dev/null | head -n 1)
    [ -n "$cfg" ] || { echo "no OpenVPN client config for $2"; exit 5; }
    # UniFi may start a new OpenVPN process for it, so the process is looked up again each time
    vpid() { for p in $(pidof openvpn); do
      tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q -F -e "--config $cfg " && { echo $p; return; }; done; }
    vpeer() { [ -n "$1" ] && ss -tunpH 2>/dev/null | grep -F "pid=$1," | awk '{print $6}' | head -n 1; }
    p=$(vpid)
    [ -n "$p" ] || { echo "the OpenVPN client for $2 isn't running"; exit 5; }
    old=$(vpeer "$p"); idx=$(cat $N/$2/ifindex 2>/dev/null || echo 0)
    note "reconnecting OpenVPN $2 (pid $p, server ${old:-?})"
    kill -USR1 "$p"
    i=0; back=0
    while [ $i -lt 60 ]; do sleep 1; i=$((i + 1))
      n=$(cat $N/$2/ifindex 2>/dev/null || echo 0)
      if [ "$n" != 0 ] && [ "$n" != "$idx" ] && ip -4 addr show dev "$2" 2>/dev/null | grep -q inet; then
        back=1; break; fi
    done
    new=$(vpeer "$(vpid)")
    if [ $back = 1 ]; then msg="$2 reconnected after ${i}s, server ${old:-?} -> ${new:-?}"
    else msg="$2 NOT back after ${i}s (server was ${old:-?})"; fi
    note "$msg"; echo "$msg"
    [ $back = 1 ] || exit 3
    exit 0 ;;
  *)
    echo "refused"; exit 2 ;;
esac
EOS
sed -i -e "s/^ALLOWED=.*/ALLOWED=\"$IFACES\"/" -e "s/^VPN_ALLOWED=.*/VPN_ALLOWED=\"$VPNS\"/" $D/wan-bounce.sh
chown root:root $D $D/wan-bounce.sh
chmod 755 $D $D/wan-bounce.sh

LINE="command=\"$D/wan-bounce.sh\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty $KEY"
echo "$LINE" > $D/authorized_key.line
mkdir -p /root/.ssh && chmod 700 /root/.ssh
touch $AK && chmod 600 $AK
[ -f $AK.bak-wan-watchdog ] || cp -p $AK $AK.bak-wan-watchdog
others > "$AK.tmp"
echo "$LINE" >> $AK.tmp
cat $AK.tmp > $AK && rm -f $AK.tmp
echo "Installed $D/wan-bounce.sh for: $IFACES${VPNS:+ (VPN reconnect: $VPNS)}"
echo "authorized_keys now has $(wc -l < $AK) key(s); watchdog key locked to wan-bounce.sh"
echo "Backup of the previous file: $AK.bak-wan-watchdog"
