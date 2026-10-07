#!/usr/bin/env bash
# Songarr on Ubuntu or Debian: a backup server (or a main one) that runs by itself.
#
# Mount the NAS first (the setup guide shows how), clone Songarr (so it updates itself from GitHub),
# and run this from its folder as your own user (it asks for sudo when it needs it):
#
#   git clone https://github.com/ApexusNULL/Songarr.git ~/songarr && cd ~/songarr
#   installer/install-linux.sh --music-folder /mnt/nas/Music \
#       --cluster-folder "/mnt/nas/Songarr servers" --name "Ubuntu box" --role backup \
#       --app-address https://songarr-backup.example.com
#
# It installs Python, FFmpeg and Deno, sets Songarr up in .venv, keeps its data in /var/lib/songarr,
# joins the servers sharing the cluster folder (left out: a server of its own), and runs Songarr as
# a systemd service that starts with the machine, and again whenever Songarr restarts itself.
set -euo pipefail

MUSIC="" CLUSTER="" NAME="$(hostname)" ROLE="backup" ADDRESS="" DATA="/var/lib/songarr" PORT=8484 APP_PORT=8486
usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --music-folder) MUSIC="$2"; shift 2 ;;
    --cluster-folder) CLUSTER="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --role) ROLE="$2"; shift 2 ;;
    --app-address) ADDRESS="$2"; shift 2 ;;
    --data) DATA="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --app-port) APP_PORT="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done
[ -n "$MUSIC" ] || { echo "--music-folder is required: where the NAS's music is mounted."; usage; }
cd "$(dirname "$0")/.."
HERE="$(pwd)"
[ -f requirements.txt ] && [ -d songarr ] || { echo "Run this from the Songarr folder."; exit 1; }
[ -d "$MUSIC" ] || { echo "$MUSIC isn't there. Mount the NAS first (see the setup guide)."; exit 1; }

echo "== Installing Python, FFmpeg and tools"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv ffmpeg git curl unzip ca-certificates
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
  || { echo "Songarr needs Python 3.12 or newer (Ubuntu 24.04 and Debian 13 have it)."; exit 1; }
if ! command -v deno >/dev/null && ! command -v node >/dev/null; then
  echo "== Installing Deno (YouTube needs a JavaScript runtime)"
  curl -fsSL https://deno.land/install.sh | sudo DENO_INSTALL=/usr/local sh -s -- -y
fi

echo "== Setting up Songarr in $HERE/.venv"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

sudo mkdir -p "$DATA"
sudo chown "$(id -un):$(id -gn)" "$DATA"
ARGS=(--data "$DATA" --music-folder "$MUSIC")
[ -n "$ADDRESS" ] && ARGS+=(--app-address "$ADDRESS")
[ -n "$CLUSTER" ] && ARGS+=(--cluster-folder "$CLUSTER" --cluster-name "$NAME" --cluster-role "$ROLE")
.venv/bin/python -m songarr "${ARGS[@]}"

echo "== Running Songarr as a service"
sudo tee /etc/systemd/system/songarr.service >/dev/null <<EOF
[Unit]
Description=Songarr music server
Wants=network-online.target
After=network-online.target remote-fs.target

[Service]
User=$(id -un)
WorkingDirectory=$HERE
ExecStart="$HERE/.venv/bin/python" -m songarr --data "$DATA" --port $PORT --app-port $APP_PORT --no-tray
Restart=on-failure
RestartSec=3
# Songarr restarts itself (after updating yt-dlp, or to take over as the active server) by exiting with 75
RestartForceExitStatus=75
SuccessExitStatus=75

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now songarr
sleep 3
systemctl --no-pager --lines=5 status songarr || true
echo
[ -d "$HERE/.git" ] || echo "Note: this copy isn't a git clone, so it won't update itself. Clone https://github.com/ApexusNULL/Songarr.git instead."
echo "Songarr is running. Its admin website is http://127.0.0.1:$PORT on this machine;"
echo "from another computer: ssh -L $PORT:127.0.0.1:$PORT $(id -un)@$(hostname), then open http://127.0.0.1:$PORT."
if [ -n "$CLUSTER" ]; then
  echo "It's one of the servers sharing $CLUSTER: manage it from any of their admin pages (Settings → Servers)."
fi
