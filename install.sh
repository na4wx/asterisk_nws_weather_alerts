#!/bin/bash
# FreePBX NWS SAME Alert System - installer
# Usage (non-interactive example):
#   sudo ./install.sh --menu-ext 7788 --email ops@yourcompany.com --delay 2 \
#        --cid-name "System Alert" --cid-num 0000

set -euo pipefail

# ---------- Defaults ----------
MENU_EXT="7788"
EMAIL=""
PREWAIT="2"
CID_NAME="System Alert"
CID_NUM="0000"

# ---------- Helpers ----------
die() { echo "ERROR: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------- Parse args ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --menu-ext) MENU_EXT="${2:-}"; shift 2 ;;
    --email)    EMAIL="${2:-}"; shift 2 ;;
    --delay)    PREWAIT="${2:-}"; shift 2 ;;
    --cid-name) CID_NAME="${2:-}"; shift 2 ;;
    --cid-num)  CID_NUM="${2:-}"; shift 2 ;;
    -h|--help)
      cat <<EOF
Usage: sudo $0 [options]
  --menu-ext <ext>     SAME menu extension (default: 7788)
  --email <addr>       Contact email for NWS User-Agent (REQUIRED)
  --delay <seconds>    Pre-play delay in seconds (default: 2)
  --cid-name <name>    Caller ID name for NWS pages (default: "System Alert")
  --cid-num <number>   Caller ID number for NWS pages (default: 0000)
EOF
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

# ---------- Interactive prompts if missing ----------
if [[ -z "${EMAIL}" ]]; then
  read -rp "Contact email for NWS User-Agent (required): " EMAIL
fi
[[ -z "$EMAIL" ]] && die "Email is required."

read -rp "SAME menu extension [${MENU_EXT}]: " _in || true
MENU_EXT="${_in:-$MENU_EXT}"

read -rp "Pre-play delay seconds [${PREWAIT}]: " _in || true
PREWAIT="${_in:-$PREWAIT}"

read -rp "Caller ID Name [${CID_NAME}]: " _in || true
CID_NAME="${_in:-$CID_NAME}"

read -rp "Caller ID Number [${CID_NUM}]: " _in || true
CID_NUM="${_in:-$CID_NUM}"

# ---------- Root check ----------
[[ $EUID -ne 0 ]] && die "Please run as root (sudo)."

# ---------- Sanity checks ----------
[[ "$MENU_EXT" =~ ^[0-9]+$ ]] || die "Menu extension must be numeric."
[[ "$PREWAIT" =~ ^[0-9]+$ ]]  || die "Delay must be an integer."
# basic email sanity check
[[ "$EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || echo "WARN: '$EMAIL' doesn't look like a typical email, continuing anyway."

# ---------- Install dependencies ----------
echo "Installing dependencies..."
if have apt-get; then
  apt-get update -y
  apt-get install -y libttspico-utils sox
elif have dnf; then
  dnf install -y libttspico-utils sox || true
elif have yum; then
  yum install -y libttspico-utils sox || true
else
  echo "WARN: Could not detect apt/dnf/yum. Ensure pico2wave and sox are installed."
fi

# ---------- Paths ----------
ROOT_DIR="$(pwd)"
AGI_DIR="/var/lib/asterisk/agi-bin"
AST_CONF_DIR="/etc/asterisk"
SOUNDS_DIR="/var/lib/asterisk/sounds/custom"
BIN_DIR="/usr/local/bin"
SYSTEMD_DIR="/etc/systemd/system"

# ---------- Ensure dirs ----------
mkdir -p "$AGI_DIR" "$AST_CONF_DIR" "$SOUNDS_DIR" "$BIN_DIR" "$SYSTEMD_DIR"

# ---------- Copy files ----------
echo "Copying files..."
cp -f "$ROOT_DIR/same_subs.py"            "$AGI_DIR/"
cp -f "$ROOT_DIR/extensions_custom.conf"  "$AST_CONF_DIR/"
cp -f "$ROOT_DIR/generate_prompts.sh"     "$BIN_DIR/"
cp -f "$ROOT_DIR/nws_alert_poller.py"     "$BIN_DIR/"
cp -f "$ROOT_DIR/nws-alert-poller.service" "$SYSTEMD_DIR/"
cp -f "$ROOT_DIR/sameCodes.json"          "$BIN_DIR/"
# optional manual pager script if present with either name
if [[ -f "$ROOT_DIR/multiPage.sh" ]]; then
  cp -f "$ROOT_DIR/multiPage.sh" "$BIN_DIR/"
elif [[ -f "$ROOT_DIR/nws_tts_page.sh" ]]; then
  cp -f "$ROOT_DIR/nws_tts_page.sh" "$BIN_DIR/multiPage.sh"
fi

# ---------- Permissions ----------
chmod +x "$AGI_DIR/same_subs.py"
chmod +x "$BIN_DIR/generate_prompts.sh"
chmod +x "$BIN_DIR/nws_alert_poller.py"
[[ -f "$BIN_DIR/multiPage.sh" ]] && chmod +x "$BIN_DIR/multiPage.sh"
chown -R asterisk:asterisk "$SOUNDS_DIR"

# ---------- Update SAME menu extension in extensions_custom.conf ----------
EXT_FILE="$AST_CONF_DIR/extensions_custom.conf"
if grep -qE '^\s*exten => [0-9]+,1,NoOp\(SAME menu\)' "$EXT_FILE"; then
  sed -i -E "s/^(\\s*exten => )[0-9]+(,1,NoOp\\(SAME menu\\))/\\1${MENU_EXT}\\2/" "$EXT_FILE"
else
  echo "WARN: Could not find 'exten => <num>,1,NoOp(SAME menu)' in extensions_custom.conf; please edit manually."
fi

# ---------- Update systemd unit with email + delay ----------
UNIT_FILE="$SYSTEMD_DIR/nws-alert-poller.service"
if [[ -f "$UNIT_FILE" ]]; then
  sed -i -E "s#(Environment=NWS_USER_AGENT=FreePBX-NWS-Alert/1.0 \\(contact: ).*(\\))#\\1${EMAIL}\\2#g" "$UNIT_FILE"
  if grep -q '^Environment=NWS_PREWAIT_SEC=' "$UNIT_FILE"; then
    sed -i -E "s/^Environment=NWS_PREWAIT_SEC=.*/Environment=NWS_PREWAIT_SEC=${PREWAIT}/" "$UNIT_FILE"
  else
    # add it under [Service]
    awk -v ins="Environment=NWS_PREWAIT_SEC=${PREWAIT}" '
      BEGIN{added=0}
      /^\[Service\]/{print; print ins; added=1; next}
      {print}
      END{if(!added) print ins}
    ' "$UNIT_FILE" > "${UNIT_FILE}.tmp" && mv "${UNIT_FILE}.tmp" "$UNIT_FILE"
  fi
else
  echo "WARN: $UNIT_FILE not found; skipping service customization."
fi

# ---------- Update CallerID in poller + manual pager ----------
# Poller: replace callerid "System Alert" <0000>
POLL_FILE="$BIN_DIR/nws_alert_poller.py"
if [[ -f "$POLL_FILE" ]]; then
  sed -i -E "s/callerid \"[^\"]+\" <[^>]+>/callerid \"${CID_NAME}\" <${CID_NUM}>/g" "$POLL_FILE"
fi

# multiPage.sh: same replacement if present
MP_FILE="$BIN_DIR/multiPage.sh"
if [[ -f "$MP_FILE" ]]; then
  sed -i -E "s/callerid \"[^\"]+\" <[^>]+>/callerid \"${CID_NAME}\" <${CID_NUM}>/g" "$MP_FILE"
fi

# ---------- Generate prompts ----------
echo "Generating menu prompts..."
"$BIN_DIR/generate_prompts.sh" || die "Prompt generation failed."

# ---------- FreePBX reload ----------
echo "Reloading FreePBX dialplan..."
if have fwconsole; then
  fwconsole reload || echo "WARN: fwconsole reload returned non-zero."
else
  echo "WARN: fwconsole not found; ensure Asterisk reloads your dialplan."
fi

# ---------- Enable + start service ----------
echo "Enabling and starting NWS poller service..."
systemctl daemon-reload
systemctl enable --now nws-alert-poller || echo "WARN: systemd enable/start failed. Check logs."

# ---------- Final output ----------
cat <<EOF

==============================================================
Install complete ✅

Configured values:
  SAME menu extension : ${MENU_EXT}
  NWS contact email   : ${EMAIL}
  Pre-play delay (s)  : ${PREWAIT}
  Caller ID Name/Num  : "${CID_NAME}" <${CID_NUM}>

Next steps:
  1) Ensure phones auto-answer intercom (*80) and trust Call-Info/Alert-Info.
  2) Verify dialplan:
       asterisk -rx "dialplan show ${MENU_EXT}@from-internal"
  3) Check service logs:
       journalctl -xeu nws-alert-poller --no-pager
  4) Test manual page (optional):
       /usr/local/bin/multiPage.sh -e 1001 -m "This is a test." -d ${PREWAIT}

If you ever need to change:
  - SAME menu extension: edit /etc/asterisk/extensions_custom.conf and 'fwconsole reload'
  - Email or delay: edit /etc/systemd/system/nws-alert-poller.service, then:
       systemctl daemon-reload && systemctl restart nws-alert-poller
  - Caller ID: update the originate lines in
       /usr/local/bin/nws_alert_poller.py and /usr/local/bin/multiPage.sh

Enjoy fast, HD NWS alerts on your PBX! 🌩
==============================================================
EOF
