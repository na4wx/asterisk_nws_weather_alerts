# FreePBX NWS SAME Alert System

## 🌩 How It Works (Quick Start)

This system transforms your FreePBX into a **live weather alerting platform** powered by the **National Weather Service (NWS)**. Once installed:

1. **Dial the SAME menu extension** (default **7788**, customizable in `extensions_custom.conf`).

   * Press **1** → Add a SAME (FIPS) code (6 digits)
   * Press **2** → Remove a SAME code
   * Press **3** → Hear a list of your subscribed SAME codes
  * Press **4** → Enter ZIP code, then choose county/SAME code

2. **Sit back and relax** ☕
   When the NWS issues an alert for any of your SAME codes:

   * The system **immediately fetches the alert**
   * Generates **clear audio** using local text-to-speech
   * Your phone **auto-answers via intercom** and plays the warning
   * The call will display as **System Alert**

3. **No spam, no repeats**
   Each extension gets called **once per alert thread** (initial + updates), even if multiple SAME codes overlap.

---

## Installation

### 🔹 Option A: Automatic Installation (recommended)

Run the provided `install.sh` script. It will:

* Install dependencies (`pico2wave`, `sox`)
* Ask for your **contact email** (required by NWS)
* Ask for your preferred **SAME menu extension** (default: 7788)
* Configure **pre-play delay** (default: 2 seconds)
* Configure **Caller ID name/number** (default: `System Alert` / `0000`)
* Copy all scripts and config files to the correct locations
* Fix permissions
* Generate audio prompts
* Reload FreePBX
* Enable and start the poller service

```bash
sudo ./install.sh
```

Non-interactive example:

```bash
sudo ./install.sh --menu-ext 7788 --email ops@yourcompany.com --delay 2 \
     --cid-name "System Alert" --cid-num 0000
```

After installation, check status:

```bash
systemctl status nws-alert-poller
```

---

### 🔹 Option B: Manual Installation

If you prefer to install manually, follow these steps:

#### What’s Included (repo files)

| Repo file                  | Purpose                                                                |
| -------------------------- | ---------------------------------------------------------------------- |
| `same_subs.py`             | AGI script for users to **add/remove/list** SAME codes by phone        |
| `extensions_custom.conf`   | Dialplan additions (binds SAME menu to an extension, default **7788**) |
| `generatePrompts.sh`       | Generates the **menu audio prompts** (wideband)                        |
| `nws_alert_poller.py`      | Polls NWS API, generates TTS audio, **auto-answers** target extensions |
| `nws-alert-poller.service` | systemd unit to keep the poller running continuously                   |
| `zip_to_same.json`         | Offline ZIP → county/SAME lookup data for menu option **4**            |
| `tools/build_lookup_data.py` | Builds ZIP lookup + enriched SAME metadata artifacts                   |
| `multiPage.sh`             | Manual sender script to page multiple extensions with TTS + delay      |

> **Important:** Where this guide says `you@domain.com`, change it to **your real email**. NWS requires a valid contact in the User-Agent.

#### Requirements

* A working **FreePBX/Asterisk** system
* Linux shell access (root)
* Packages: `libttspico-utils` (pico2wave TTS), `sox`

```bash
apt-get update
apt-get install -y libttspico-utils sox
```

#### Manual Install (step-by-step)

1. **Copy files to their destinations**

   * `same_subs.py` → `/var/lib/asterisk/agi-bin/`
   * `extensions_custom.conf` → `/etc/asterisk/`
   * `generatePrompts.sh` → `/usr/local/bin/`
   * `nws_alert_poller.py` → `/usr/local/bin/`
   * `nws-alert-poller.service` → `/etc/systemd/system/`
   * `multiPage.sh` → `/usr/local/bin/`

2. **Make scripts executable (and set AGI ownership)**

   ```bash
   chmod +x /var/lib/asterisk/agi-bin/same_subs.py
   chown asterisk:asterisk /var/lib/asterisk/agi-bin/same_subs.py

   chmod +x /usr/local/bin/nws_alert_poller.py
   chmod +x /usr/local/bin/generatePrompts.sh
   chmod +x /usr/local/bin/multiPage.sh
   ```

3. **Generate the menu prompts**

   ```bash
   /usr/local/bin/generatePrompts.sh
   ```

4. **Reload FreePBX to load the new dialplan**

   ```bash
   fwconsole reload
   ```

5. **Configure and start the poller (systemd)**

   * Edit `/etc/systemd/system/nws-alert-poller.service` and update:

     * `Environment=NWS_USER_AGENT=FreePBX-NWS-Alert/1.0 (contact: you@domain.com)` → your real email
     * `Environment=NWS_PREWAIT_SEC=2` → change delay if needed
   * Enable and start:

     ```bash
     systemctl daemon-reload
     systemctl enable --now nws-alert-poller
     ```

---

## Customization

* **SAME Menu Extension:**
  Default is **7788**. Change it in `extensions_custom.conf` under `[from-internal-custom]`.

* **Caller ID for Alerts:**
  Pages display as **System Alert** with number **0000**. Change the name/number in:

  * `nws_alert_poller.py` → `page_extension()` function
  * `multiPage.sh` → the `asterisk -rx` line

* **Pre-play Delay:**
  Some phones take a second to auto-answer. Delay is handled by chaining `silence/1` files before playback.

  * Change default seconds in systemd unit:

    ```
    Environment=NWS_PREWAIT_SEC=2
    ```
  * Or override per manual page run:

    ```bash
    ./multiPage.sh -e 1001 -m "Test" -d 3
    ```

* **User-Agent Contact Email:**
  Change `you@domain.com` to your real, reachable email in the systemd unit. Required by NWS.

---

## FreePBX / Phone Settings

* **Intercom / Auto-Answer:**
  In FreePBX, ensure the **Intercom prefix** (typically `*80`) is **enabled** (Admin → Feature Codes).
  On phones, enable “Auto Answer by Call-Info/Alert-Info” (name varies by vendor).

* **Wideband (HD) audio:**
  Enable **G.722** on extensions/phones and in FreePBX so alerts and prompts play in higher quality.

---

## Data & Audio Locations (FYI)

* Subscriptions (written by the menu/AGI):
  `/etc/asterisk/nws_subscriptions.json`

* ZIP lookup data (used by AGI menu option 4):
  `/usr/local/bin/zip_to_same.json`

* Enriched SAME metadata (optional, generated):
  `/usr/local/bin/same_metadata.json`

* Full SAME list with state/county/ZIP coverage (optional, generated):
  `/usr/local/bin/same_codes_enriched.json`

* De-duplication state (written by poller):
  `/var/lib/asterisk/nws_alert_state.json`

* Audio cache (per SAME + alert group), wideband:
  `/var/lib/asterisk/sounds/custom/nws_<SAME>_<GROUP>.wav16`

---

## Testing & Troubleshooting

### Build Offline ZIP Lookup Data

The repo includes a builder script to generate production lookup files from a local ZIP/county CSV plus `sameCodes.json`.

1. Put your ZIP/county crosswalk CSV at `data/zip_county.csv`.
   Required columns (case-insensitive aliases supported): ZIP, county, state.
2. Run:

```bash
python3 tools/build_lookup_data.py \
  --same-codes sameCodes.json \
  --zip-county-csv data/zip_county.csv \
  --out-zip zip_to_same.json \
  --out-same data/same_metadata.json \
  --out-enriched data/same_codes_enriched.json
```

3. Re-run `install.sh` (or copy files manually) so `/usr/local/bin/zip_to_same.json` is updated.

The builder does a best-effort enrichment pass against `https://www.weather.gov/nwr/county_coverage?State=<STATE_ABBR>` to prefer weather.gov county naming in generated metadata.

* **Dialplan loaded?**

  ```bash
  asterisk -rx "dialplan show 7788@from-internal"
  ```

* **Reload after changes:**

  ```bash
  fwconsole reload
  ```

* **Poller logs:**

  ```bash
  journalctl -xeu nws-alert-poller --no-pager
  ```

* **Sounds directory permissions (if you see write errors):**

  ```bash
  mkdir -p /var/lib/asterisk/sounds/custom
  chown -R asterisk:asterisk /var/lib/asterisk/sounds/custom
  chmod -R 755 /var/lib/asterisk/sounds/custom
  ```

---

## Notes & Reminders

* Replace **`you@domain.com`** anywhere it appears with your **real, reachable email address**.
  The NWS API rejects generic/missing User-Agents.

* Ensure your phones/extensions negotiate **G.722** to actually hear HD audio; otherwise Asterisk will transcode to narrowband.

* This project is a starting point—harden and monitor your PBX according to your environment’s best practices.

---

If you get stuck on any step, open an issue with your **FreePBX version**, a **snippet of `journalctl -xeu nws-alert-poller`**, and what you’ve tried so far.
