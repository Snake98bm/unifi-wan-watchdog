#!/bin/sh
# Let wan-watchdog read a Quectel modem's own sensors (RM520N-GL: case, board and amplifier temperatures, the
# supply rail) over SSH. Run as root on the modem, passing the watchdog's PUBLIC key (data/ssh/id_ed25519.pub):
#   ssh root@<modem> "WATCHDOG_PUBKEY='ssh-ed25519 AAAA... wan-watchdog' sh" < install-sensors-key.sh
# Windows PowerShell:
#   Get-Content install-sensors-key.sh -Raw | ssh root@<modem> "tr -d '\r' | WATCHDOG_PUBKEY='...' sh"
# Undo: "sh -s remove" instead of "sh" (WATCHDOG_PUBKEY is still needed to find the key line).
#
# What it does: adds the key to root's authorized_keys, locked to ONE read-only command that prints the sensor
# files (command=..., no pty, no forwarding), whatever the watchdog asks for. Your own keys are left alone and the
# file is backed up first. Nothing else on the modem changes. It then prints what the watchdog will see.
set -e
KEY="${WATCHDOG_PUBKEY:?set WATCHDOG_PUBKEY to the watchdog's public key (data/ssh/id_ed25519.pub)}"
case "$KEY" in ssh-ed25519\ AAAA*|ssh-rsa\ AAAA*|ecdsa-sha2-*\ AAAA*) ;; *) echo "WATCHDOG_PUBKEY isn't an SSH public key"; exit 1 ;; esac
BODY=$(echo "$KEY" | awk '{print $2}')
IIO=/sys/bus/iio/devices/iio:device0
CMD="cd $IIO && grep -H . in_temp_*_input in_voltage_vph_pwr_input 2>/dev/null; exit 0"
OPTS="command=\"$CMD\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding"

# root's authorized_keys: ask OpenSSH where it looks (the toolkit's sshd may use its own config), else ~/.ssh
AK=""
for d in sshd /usr/sbin/sshd /opt/sbin/sshd /usrdata/sbin/sshd; do
  if command -v "$d" >/dev/null 2>&1 || [ -x "$d" ]; then
    f=$("$d" -T 2>/dev/null | awk '$1 == "authorizedkeysfile" {print $2; exit}')
    if [ -n "$f" ]; then
      f=$(echo "$f" | sed "s#%h#$HOME#g; s#%u#root#g; s#%%#%#g")
      case "$f" in /*) AK="$f" ;; *) AK="$HOME/$f" ;; esac
      break
    fi
  fi
done
[ -n "$AK" ] || AK="$HOME/.ssh/authorized_keys"
echo "authorized_keys: $AK"

others() { [ -f "$AK" ] && awk -v b="$BODY" 'index($0, b) == 0' "$AK" || true; }

if [ "$1" = "remove" ]; then
  if [ -f "$AK" ]; then
    others > "$AK.tmp" && cat "$AK.tmp" > "$AK" && rm -f "$AK.tmp"
    echo "wan-watchdog sensors key removed"
  fi
  exit 0
fi

[ -d "$IIO" ] || { echo "no $IIO on this modem - nothing to read"; exit 1; }
mkdir -p "$(dirname "$AK")"
chmod 700 "$(dirname "$AK")"
[ -f "$AK" ] && cp "$AK" "$AK.bak-wan-watchdog-$(date +%Y%m%d%H%M%S)"
{ others; echo "$OPTS $KEY"; } > "$AK.tmp"
cat "$AK.tmp" > "$AK" && rm -f "$AK.tmp"
chmod 600 "$AK"
echo "wan-watchdog sensors key installed. The watchdog will read:"
sh -c "$CMD"
