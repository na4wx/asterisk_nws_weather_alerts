# 🌩️ FreePBX NWS SAME Alert System

Turn your FreePBX into a **real-time weather alert station**, powered directly by the National Weather Service. When a tornado warning, flash flood, or severe thunderstorm watch is issued for your area, your phones ring — automatically.

---

## ✨ Features

- 📍 **Subscribe by ZIP code** — no FIPS codes to look up; just enter your ZIP and pick your county
- 🔕 **No duplicates** — each extension is called once per alert thread, even across restarts
- 🗣️ **Local TTS** — all audio generated on your PBX with `pico2wave`; no cloud dependency
- 📞 **Caller ID support** — alerts show your configured name and number on the phone display
- 🗂️ **Simple IVR menu** — add, remove, and list subscriptions entirely by phone

---

## 🚀 Quick Install

```bash
sudo ./install.sh
```

That's it. The script will ask a few questions and handle everything else.

**Non-interactive example:**
```bash
sudo ./install.sh \
  --menu-ext 7788 \
  --email ops@yourcompany.com \
  --delay 2 \
  --cid-name "NWS Alert" \
  --cid-num 0000
```

| Option | Default | Description |
|---|---|---|
| `--menu-ext` | `7788` | Extension users dial to manage subscriptions |
| `--email` | *(required)* | Your contact email — required by NWS in the User-Agent |
| `--delay` | `2` | Seconds of silence before audio plays (phone auto-answer ramp-up) |
| `--cid-name` | `System Alert` | Caller ID name shown on alerted phones |
| `--cid-num` | `0000` | Caller ID number shown on alerted phones |

---

## 📞 Using the IVR Menu

Dial your menu extension (default **7788**) from any internal phone.

| Key | Action |
|---|---|
| **1** | Add an area by ZIP code |
| **2** | Remove a subscribed area |
| **3** | Hear a list of your subscribed areas |
| **★** | Cancel / go back at any prompt |

When you press **1**, just enter your ZIP code. If more than one county matches, you'll be offered a numbered list to choose from. Confirm your selection and you're done.

---

## ⚙️ How It Works

```
NWS API → nws_alert_poller.py → pico2wave TTS → Asterisk originate → Phone auto-answers
```

1. The poller runs as a **systemd service**, continuously polling `api.weather.gov` for active alerts
2. When an alert's SAME codes match a subscriber's list, alert text is converted to **16 kHz wideband audio**
3. Asterisk originates a call to `*80<ext>` (intercom prefix), which triggers **auto-answer**
4. The alert plays, showing your configured Caller ID name and number

---

## 🛠️ Manual Installation

If you prefer to install step by step:

### Requirements

- FreePBX / Asterisk (any modern version)
- Root shell access
- `pico2wave` and `sox`:
  ```bash
  apt-get install -y libttspico-utils sox
  ```

### Files & Destinations

| File | Destination |
|---|---|
| `same_subs.py` | `/var/lib/asterisk/agi-bin/` |
| `extensions_custom.conf` | `/etc/asterisk/` |
| `generate_prompts.sh` | `/usr/local/bin/` |
| `nws_alert_poller.py` | `/usr/local/bin/` |
| `nws-alert-poller.service` | `/etc/systemd/system/` |
| `sameCodes.json` | `/usr/local/bin/` |
| `zip_to_same.json` | `/usr/local/bin/` |

### Steps

```bash
# 1. Copy files (adjust paths if needed)
cp same_subs.py /var/lib/asterisk/agi-bin/
cp extensions_custom.conf /etc/asterisk/
cp generate_prompts.sh nws_alert_poller.py sameCodes.json zip_to_same.json /usr/local/bin/
cp nws-alert-poller.service /etc/systemd/system/

# 2. Permissions
chmod +x /var/lib/asterisk/agi-bin/same_subs.py
chmod +x /usr/local/bin/generate_prompts.sh /usr/local/bin/nws_alert_poller.py
chown -R asterisk:asterisk /var/lib/asterisk/sounds/custom

# 3. Generate menu audio
/usr/local/bin/generate_prompts.sh

# 4. Reload dialplan
fwconsole reload

# 5. Edit the service unit — set your real email
nano /etc/systemd/system/nws-alert-poller.service

# 6. Start the service
systemctl daemon-reload
systemctl enable --now nws-alert-poller
```

---

## 🔧 Customization

### Caller ID
Edit the service unit and change these two lines:
```ini
Environment=NWS_CID_NAME=System Alert
Environment=NWS_CID_NUM=0000
```
Then restart: `systemctl daemon-reload && systemctl restart nws-alert-poller`

### Menu Extension
Edit `/etc/asterisk/extensions_custom.conf`, change the `exten =>` number under `[from-internal-custom]`, then `fwconsole reload`.

### Pre-play Delay
Some phones take a moment to auto-answer. Increase `NWS_PREWAIT_SEC` in the service unit if the first second of audio gets cut off.

### NWS Contact Email
```ini
Environment=NWS_USER_AGENT=FreePBX-NWS-Alert/1.0 (contact: you@domain.com)
```
> ⚠️ Replace `you@domain.com` with a real address. The NWS API requires a valid contact in the User-Agent header.

---

## 📡 Phone Setup

- **Intercom / Auto-Answer:** In FreePBX, ensure the `*80` Feature Code is enabled (**Admin → Feature Codes → Intercom**). On phones, enable "Auto Answer by Call-Info / Alert-Info" — exact label varies by manufacturer.
- **HD Audio:** Enable **G.722** on your extensions and phones for full wideband quality. Without it, Asterisk will transcode to narrowband.

---

## 🗂️ File Locations (Reference)

| Path | Contents |
|---|---|
| `/var/lib/asterisk/nws_subscriptions.json` | Extension → SAME code subscriptions |
| `/var/lib/asterisk/nws_alert_state.json` | De-duplication state (seen alert+extension pairs) |
| `/var/lib/asterisk/sounds/custom/nws_*.wav16` | Cached alert audio (auto-expired after 2 days) |
| `/usr/local/bin/zip_to_same.json` | Offline ZIP → county/SAME lookup data |
| `/var/log/asterisk/same_subs.log` | AGI subscription menu log |

---

## 🔍 Troubleshooting

**Check the poller is running:**
```bash
systemctl status nws-alert-poller
journalctl -xeu nws-alert-poller --no-pager
```

**Verify the dialplan loaded:**
```bash
asterisk -rx "dialplan show 7788@from-internal"
```

**Reload after dialplan changes:**
```bash
fwconsole reload
```

**Sounds directory permissions:**
```bash
mkdir -p /var/lib/asterisk/sounds/custom
chown -R asterisk:asterisk /var/lib/asterisk/sounds/custom
```

---

## 🔄 Rebuilding ZIP Lookup Data

The `zip_to_same.json` file ships ready to use (40,000+ ZIP codes, full national coverage). If you ever need to regenerate it from source:

```bash
python3 tools/build_lookup_data.py \
  --same-codes sameCodes.json \
  --zip-county-csv data/zip_county.csv \
  --out-zip zip_to_same.json
```

Then re-run `install.sh` or copy `zip_to_same.json` to `/usr/local/bin/`.

---

## 💬 Getting Help

If something isn't working, open an issue and include:
- Your FreePBX version
- Output of `journalctl -xeu nws-alert-poller --no-pager`
- Output of `asterisk -rx "dialplan show 7788@from-internal"`

Stay safe out there! 🌪️
