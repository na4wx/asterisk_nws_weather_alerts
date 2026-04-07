#!/usr/bin/env python3
import json, os, subprocess, urllib.request, urllib.error, hashlib, re, time
from pathlib import Path

# ------------------------------------------------------------
# Paths
# ------------------------------------------------------------
SUBS_FILE = Path("/var/lib/asterisk/nws_subscriptions.json")
SOUNDS_DIR  = Path("/var/lib/asterisk/sounds/custom")
STATE_FILE  = Path("/var/lib/asterisk/nws_alert_state.json")
SAME_CODES_FILE = Path("/usr/local/bin/sameCodes.json")  # SAME code -> county mapping

# ------------------------------------------------------------
# Config (env overrides allowed)
# ------------------------------------------------------------
USER_AGENT          = os.getenv("NWS_USER_AGENT", "FreePBX-NWS-Alert/1.0 (contact: yourname@example.com)")
NWS_PREWAIT_SEC     = int(os.getenv("NWS_PREWAIT_SEC", "2"))  # whole seconds of silence/1 before message
NWS_ALERT_DELAY_SEC = int(os.getenv("NWS_ALERT_DELAY_SEC", "30"))  # delay between sequential alerts
CID_NAME            = os.getenv("NWS_CID_NAME", "System Alert")
CID_NUM             = os.getenv("NWS_CID_NUM", "0000")

# ------------------------------------------------------------
# API
# ------------------------------------------------------------
# Note: message_type must be lowercase per NWS enum
NWS_URL = "https://api.weather.gov/alerts/active?status=actual&message_type=alert,update"

# ------------------------------------------------------------
# Audio cache naming / retention
# ------------------------------------------------------------
CACHE_PREFIX   = "nws_"          # final files: nws_<SAME>_<GROUP>.wav16
CACHE_TTL_SECS = 2 * 24 * 3600   # ~2 days

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2))

def load_same_codes():
    """Load SAME code -> county name mapping from sameCodes.json"""
    codes_list = load_json(SAME_CODES_FILE, [])
    result = {}
    for item in codes_list:
        if isinstance(item, dict):
            result.update(item)
    return result

def fetch_alerts():
    req = urllib.request.Request(
        NWS_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp).get("features", [])
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        print(f"NWS HTTPError {e.code}: {e.reason}\n{body}")
        return []
    except urllib.error.URLError as e:
        print(f"NWS URLError: {e}")
        return []

def sanitize_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)[:80]

def _first_ref_id_from_string(refs_str: str):
    """
    CAP 'references' string: 'sender,id,sent sender,id,sent ...'
    Extract the first 'id' (middle token of the first triplet).
    """
    m = re.search(r'[^,\s]+,([^,\s]+),[^,\s]+', refs_str or "")
    return m.group(1) if m else None

def canonical_alert_group_id(props: dict) -> str:
    """
    Stable 'group id' for an alert thread.
    Prefer first ID in CAP 'references' chain (ties updates to original alert).
    'references' may be a string or a list; handle both. Fallback to this id/hash.
    """
    refs = props.get("references")
    ref_id = None
    if isinstance(refs, str):
        ref_id = _first_ref_id_from_string(refs)
    elif isinstance(refs, list):
        for item in refs:
            if isinstance(item, str):
                ref_id = _first_ref_id_from_string(item)
                if ref_id:
                    break
    if ref_id:
        return ref_id
    return props.get("id") or hashlib.sha1(json.dumps(props, sort_keys=True).encode()).hexdigest()

def extract_weather_phenomenon(description: str) -> str:
    """
    Extract key weather phenomenon from description.
    First tries pattern matching against common hazards.
    Falls back to extracting first noun phrase if no pattern match.
    """
    if not description:
        return "Weather Alert"
    
    # Common weather phenomena to match (case-insensitive)
    phenomena = [
        "dense fog", "heavy snow", "heavy rain", "thunderstorm", "high wind",
        "frost", "freeze", "heat advisory", "wind chill", "blizzard",
        "tornado", "flash flood", "flood", "winter storm", "ice storm",
        "severe thunderstorm", "hail", "extreme cold", "heat", "wind advisory",
        "winter weather", "lake effect snow", "sleet", "freezing rain",
        "fog advisory", "freeze warning", "frost advisory"
    ]
    
    desc_lower = description.lower()
    for phenomenon in phenomena:
        if phenomenon in desc_lower:
            # Return in title case
            return " ".join(word.capitalize() for word in phenomenon.split())
    
    # Fallback: extract first sentence and get opening phrase
    first_sentence = description.split(".")[0] if "." in description else description
    first_sentence = first_sentence.strip()
    
    # Try to extract first few words (typically contains the key phenomenon)
    words = first_sentence.split()
    if len(words) >= 2:
        # Return first 2-3 words (e.g., "Patchy dense fog" -> "Patchy Dense Fog")
        phrase = " ".join(words[:3]) if len(words) >= 3 else " ".join(words[:2])
        return " ".join(word.capitalize() for word in phrase.split())
    elif len(words) == 1:
        return words[0].capitalize()
    
    return "Weather Alert"

def tts_wav16_base(text: str, same_code: str, group_id: str) -> str:
    """
    Ensure a 16k mono PCM .wav16 exists for this SAME+group.
    Returns the base for Playback, e.g., 'custom/nws_047001_ab12cd34'.
    """
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    gid_short = sanitize_id(group_id) or hashlib.sha1(group_id.encode()).hexdigest()[:10]
    base = f"{CACHE_PREFIX}{same_code}_{gid_short}"
    final_wav = SOUNDS_DIR / f"{base}.wav"
    final_wav16 = SOUNDS_DIR / f"{base}.wav16"

    if final_wav16.exists():
        return f"custom/{base}"

    tmp_in  = Path("/tmp/nws_tts_in.wav")
    tmp_out = Path("/tmp/nws_tts_out.wav")

    subprocess.run(["pico2wave", "-l", "en-US", "-w", str(tmp_in), text], check=True)
    subprocess.run([
        "sox", str(tmp_in),
        "-r", "16000", "-c", "1", "-b", "16", "-e", "signed-integer",
        str(tmp_out), "norm", "-3"
    ], check=True)

    if final_wav.exists():
        final_wav.unlink(missing_ok=True)
    if final_wav16.exists():
        final_wav16.unlink(missing_ok=True)

    tmp_out.replace(final_wav)
    final_wav.rename(final_wav16)

    try:
        import pwd, grp
        os.chown(final_wav16, pwd.getpwnam("asterisk").pw_uid, grp.getgrnam("asterisk").gr_gid)
    except Exception:
        pass
    os.chmod(final_wav16, 0o644)

    try:
        tmp_in.unlink(missing_ok=True)
    except Exception:
        pass

    return f"custom/{base}"

def page_extension(ext: str, playback_base: str):
    """
    Auto-answer via *80, prepend silence/1 N times (post-answer delay),
    then play the file; set CallerID to 'System Alert'.
    """
    if NWS_PREWAIT_SEC > 0:
        play_chain = "&".join(["silence/1"] * NWS_PREWAIT_SEC + [playback_base])
    else:
        play_chain = playback_base

    # Use the context/exten/priority form so that callerid is honoured by
    # Asterisk before the channel is answered.  The 'application' form
    # ignores the trailing callerid token and falls back to the PBX IP.
    subprocess.run([
        "asterisk", "-rx",
        f"channel originate Local/*80{ext}@from-internal"
        f" extension {play_chain}@app-nws-alert-play"
        f" callerid \"{CID_NAME}\" <{CID_NUM}>"
    ], check=False)


def cleanup_old_audio():
    now = time.time()
    for p in SOUNDS_DIR.glob(f"{CACHE_PREFIX}*.wav16"):
        try:
            if now - p.stat().st_mtime > CACHE_TTL_SECS:
                p.unlink()
        except Exception:
            pass
    for p in SOUNDS_DIR.glob(f"{CACHE_PREFIX}*.wav"):
        try:
            if now - p.stat().st_mtime > CACHE_TTL_SECS:
                p.unlink()
        except Exception:
            pass

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    global same_codes_lookup
    
    subs = load_json(SUBS_FILE, [])
    state = load_json(STATE_FILE, {"seen_pairs": []})
    same_codes_lookup = load_same_codes()  # Load SAME code -> county mapping
    seen_pairs = set(state.get("seen_pairs", []))   # keys: "<group_id>|<ext>"

    alerts = fetch_alerts()
    new_seen_pairs = set(seen_pairs)
    
    # Build a queue of calls ordered by alert timestamp
    call_queue = []  # List of (sent_timestamp, ext, playbase, group_id)

    for f in alerts:
        props = f.get("properties", {}) or {}
        same_list = (props.get("geocode", {}) or {}).get("SAME", []) or []
        if not same_list:
            continue

        group_id = canonical_alert_group_id(props)

        # Extract alert properties
        event    = props.get("event", "Weather Alert")
        area     = props.get("areaDesc", "")
        headline = props.get("headline", "")
        description = props.get("description", "")
        sent     = props.get("sent", "1970-01-01T00:00:00Z")  # Fallback timestamp

        # Map ext -> one SAME code (avoid duplicate calls when multiple codes match)
        ext_to_code = {}
        for sub in subs:
            ext = sub.get("extension")
            codes = sub.get("codes", [])
            if not ext or not codes:
                continue
            inter = sorted(set(codes).intersection(same_list))
            if not inter:
                continue
            if f"{group_id}|{ext}" in seen_pairs:
                continue
            ext_to_code[ext] = inter[0]  # deterministic selection

        if not ext_to_code:
            continue

        # Ensure audio exists for each code we’ll use
        code_to_playbase = {}
        for code in sorted(set(ext_to_code.values())):
            # Build code-specific message with county name from SAME code lookup
            if event == "Special Weather Statement":
                # Extract the actual phenomenon from description (e.g., "Dense Fog")
                phenomenon = extract_weather_phenomenon(description)
                msg = f"National Weather Service. {phenomenon}. {description}"
            else:
                # Use county name from SAME code instead of full area description
                county = same_codes_lookup.get(code, area)
                msg = f"National Weather Service. {event}. Affected area: {county}. {headline}"
            
            if len(msg) > 900:
                msg = msg[:900] + "..."
            
            try:
                code_to_playbase[code] = tts_wav16_base(msg, code, group_id)
            except Exception as e:
                print(f"TTS fail for code {code} group {group_id}: {e}")

        # Queue each extension call (don't call yet - will process in order)
        for ext, code in ext_to_code.items():
            playbase = code_to_playbase.get(code)
            if not playbase:
                continue
            # Add to queue with timestamp for ordering
            call_queue.append((sent, ext, playbase, group_id))

    # Sort queue by alert sent timestamp (chronological order)
    call_queue.sort(key=lambda x: x[0])
    
    # Process the queue sequentially with delays
    for i, (sent_ts, ext, playbase, group_id) in enumerate(call_queue):
        try:
            page_extension(ext, playbase)
            new_seen_pairs.add(f"{group_id}|{ext}")
            # Wait before next alert to ensure proper sequencing
            if i < len(call_queue) - 1:
                time.sleep(NWS_ALERT_DELAY_SEC)
        except Exception as e:
            print(f"Page fail ext {ext} group {group_id}: {e}")
    save_json(STATE_FILE, {"seen_pairs": sorted(new_seen_pairs)})

    # Housekeeping
    cleanup_old_audio()

if __name__ == "__main__":
    main()
