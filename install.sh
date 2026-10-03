#!/bin/bash
# Tilt logger & dashboard installer
# Run on the Raspberry Pi, from the folder containing these files:
#   sudo bash install.sh
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo bash install.sh"; exit 1
fi
cd "$(dirname "$0")"
for f in tilt_logger.py tilt_dashboard.py tilt-logger.service \
         tilt-dashboard.service tilt-logger-watch.path tilt-logger-watch.service; do
  [ -f "$f" ] || { echo "Missing $f — run this from the unzipped folder."; exit 1; }
done

echo "==> Installing packages (bluez, pip, bleak)..."
apt-get update -qq
apt-get install -y -qq bluez python3-pip >/dev/null
pip3 install -q bleak --break-system-packages

echo "==> Creating service user and directories..."
id -u tilt >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin tilt
mkdir -p /opt/tilt-logger /var/log/tilt
cp tilt_logger.py tilt_dashboard.py /opt/tilt-logger/
chown -R tilt:tilt /opt/tilt-logger /var/log/tilt   # web updates need this

echo "==> Installing systemd services..."
cp tilt-logger.service tilt-dashboard.service \
   tilt-logger-watch.path tilt-logger-watch.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tilt-logger tilt-dashboard tilt-logger-watch.path

echo "==> Setting up weekly log rotation..."
cat > /etc/logrotate.d/tilt <<'ROT'
/var/log/tilt/tilt.jsonl {
    weekly
    rotate 12
    compress
    copytruncate
    missingok
}
ROT

echo "==> Making sure Bluetooth is on..."
rfkill unblock bluetooth 2>/dev/null || true
systemctl enable --now bluetooth >/dev/null 2>&1 || true
bluetoothctl power on >/dev/null 2>&1 || true

IP=$(hostname -I | awk '{print $1}')
echo
echo "Done. Services running:"
systemctl --no-pager --legend=false list-units 'tilt-*' | sed 's/^/  /'
echo
echo "Open the dashboard:   http://${IP}:8080/   (or http://$(hostname).local:8080/)"
echo "User guide:           http://${IP}:8080/guide"
echo "Logger status:        journalctl -u tilt-logger -f"
echo
echo "The Tilt only broadcasts while floating or tilted — drop it in liquid"
echo "and readings appear within seconds."
