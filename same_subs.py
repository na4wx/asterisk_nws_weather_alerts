#!/usr/bin/env python3
import sys, os, json, re, hashlib, subprocess, tempfile, logging
from pathlib import Path
import pwd, grp

# Use a writeable location
DB = Path("/var/lib/asterisk/nws_subscriptions.json")
SOUNDS_DIR = Path("/var/lib/asterisk/sounds/custom")
SAME_CODES_FILE = Path("/usr/local/bin/sameCodes.json")
ZIP_TO_SAME_FILE = Path("/usr/local/bin/zip_to_same.json")
VOICE = "en-US"
LOG = Path("/var/log/asterisk/same_subs.log")

# Logging (best-effort; won't crash if perms deny)
try:
    logging.basicConfig(filename=str(LOG), level=logging.INFO, format="%(asctime)s %(message)s")
except Exception:
    pass

def load_db():
    try:
        return json.loads(DB.read_text())
    except Exception:
        return []

def save_db(data):
    DB.parent.mkdir(parents=True, exist_ok=True)
    DB.write_text(json.dumps(data, indent=2))

def upsert_code(ext, code):
    """Returns True if newly added, False if already present."""
    data = load_db()
    row = next((r for r in data if r.get("extension") == ext), None)
    if not row:
        row = {"extension": ext, "codes": []}
        data.append(row)
    if code not in row["codes"]:
        row["codes"].append(code)
        save_db(data)
        return True
    return False

def remove_code(ext, code):
    data = load_db()
    for r in data:
        if r.get("extension") == ext and code in r.get("codes", []):
            r["codes"] = [c for c in r["codes"] if c != code]
            save_db(data)
            return True
    return False

def list_codes(ext):
    for r in load_db():
        if r.get("extension") == ext:
            return r.get("codes", [])
    return []

def load_same_codes_lookup():
    data = {}
    try:
        raw = json.loads(SAME_CODES_FILE.read_text())
    except Exception:
        return data
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                for k, v in item.items():
                    code = re.sub(r"\D", "", str(k))
                    if len(code) == 6:
                        data[code] = str(v)
    elif isinstance(raw, dict):
        for k, v in raw.items():
            code = re.sub(r"\D", "", str(k))
            if len(code) == 6:
                data[code] = str(v)
    return data

def load_zip_to_same():
    """
    Expected JSON shape:
      {
        "36066": [
          {"county": "Autauga, AL", "codes": ["001001"]},
          {"county": "Elmore, AL", "codes": ["001051"]}
        ]
      }

    Also supports fallback forms:
      "ZIP": ["001001", "001051"]
      "ZIP": {"Autauga, AL": ["001001"]}
    """
    try:
        raw = json.loads(ZIP_TO_SAME_FILE.read_text())
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}

def normalize_zip_matches(zip_map, zip_code, same_lookup):
    entry = zip_map.get(zip_code)
    if not entry:
        return []

    results = []

    if isinstance(entry, list):
        for item in entry:
            if isinstance(item, dict):
                county = str(item.get("county", "Unknown county"))
                codes = [re.sub(r"\D", "", str(c)) for c in item.get("codes", [])]
                codes = [c for c in codes if len(c) == 6]
                if codes:
                    results.append({"county": county, "codes": sorted(set(codes))})
            elif isinstance(item, str):
                code = re.sub(r"\D", "", item)
                if len(code) == 6:
                    county = same_lookup.get(code, "Unknown county")
                    results.append({"county": county, "codes": [code]})

    elif isinstance(entry, dict):
        for county, codes in entry.items():
            cleaned = [re.sub(r"\D", "", str(c)) for c in (codes or [])]
            cleaned = [c for c in cleaned if len(c) == 6]
            if cleaned:
                results.append({"county": str(county), "codes": sorted(set(cleaned))})

    deduped = {}
    for row in results:
        key = (row["county"], tuple(row["codes"]))
        deduped[key] = row
    return list(deduped.values())

def agi_read_env():
    while True:
        line = sys.stdin.readline()
        if not line or line.strip() == "":
            break

def agi_cmd(cmd):
    sys.stdout.write(cmd.strip() + "\n")
    sys.stdout.flush()
    return sys.stdin.readline().strip()

def agi_result_value(resp):
    m = re.search(r"result=([^\s]+)", resp or "")
    if not m:
        return ""
    val = m.group(1)
    return "" if val == "-1" else val

def agi_get_digits(maxdigits, timeout_ms=10000):
    resp = agi_cmd(f"GET DATA beep {timeout_ms} {maxdigits}")
    val = agi_result_value(resp)
    return re.sub(r"\D", "", val or "")

def agi_get_digits_raw(maxdigits, timeout_ms=10000):
    """Like agi_get_digits but preserves * for cancel detection."""
    resp = agi_cmd(f"GET DATA beep {timeout_ms} {maxdigits}")
    val = agi_result_value(resp)
    return val or ""

CANCELLED = object()  # sentinel returned when user presses *

def choose_from_numbered_list(items, intro_text, max_items=9):
    """
    Speak intro_text, read numbered choices, wait for a digit.
    - Returns 0-based index on valid selection.
    - Returns CANCELLED if user presses *.
    - Returns None after two failed attempts (re-prompts once on bad input).
    Warns if list was truncated beyond max_items.
    """
    if not items:
        return None

    truncated = len(items) > max_items
    capped = items[:max_items]

    speak_tts(intro_text)
    if truncated:
        speak_tts(f"Showing the first {max_items} options.")
    for idx, item in enumerate(capped, 1):
        speak_tts(f"Press {idx} for {item}.")
    speak_tts("Press star to cancel.")

    for attempt in range(2):
        speak_tts("Please make your selection now.")
        raw = agi_get_digits_raw(1)
        if raw == "*":
            return CANCELLED
        digits = re.sub(r"\D", "", raw)
        if digits:
            n = int(digits)
            if 1 <= n <= len(capped):
                return n - 1
        if attempt == 0:
            speak_tts("I didn't get that. Let me repeat the options.")
            for idx, item in enumerate(capped, 1):
                speak_tts(f"Press {idx} for {item}.")
            speak_tts("Press star to cancel.")

    return None

_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "Washington D.C.",
    "AS": "American Samoa", "GU": "Guam", "MP": "Northern Mariana Islands",
    "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
}

# States where the geographic unit is not "County"
_STATE_GEO_UNIT = {
    "LA": "Parish",
    "AK": "Borough",
}

def spoken_county_label(raw_label: str) -> str:
    """
    Convert a stored label like "Jefferson, AL" into a natural spoken form:
    "Jefferson County, Alabama".

    Handles:
    - Labels already containing County/Parish/Borough/Census Area (pass through).
    - City of X entries (Virginia independent cities): "City of Alexandria, VA"
      → "the city of Alexandria, Virginia".
    - Louisiana → Parish, Alaska → Borough, everyone else → County.
    """
    raw_label = (raw_label or "").strip()
    parts = [p.strip() for p in raw_label.split(",")]
    if len(parts) < 2:
        return raw_label

    county_part = parts[0]
    state_abbr = parts[-1].upper()
    state_full = _STATE_NAMES.get(state_abbr, state_abbr)

    # Already has a geographic unit word — just replace the abbreviation.
    unit_words = ("county", "parish", "borough", "census area", "municipality", "city and borough")
    if any(w in county_part.lower() for w in unit_words):
        return f"{county_part}, {state_full}"

    # Virginia (and a few others) have "City of X" independent cities.
    if county_part.lower().startswith("city of "):
        city_name = county_part[8:].strip()
        return f"the city of {city_name}, {state_full}"

    unit = _STATE_GEO_UNIT.get(state_abbr, "County")
    return f"{county_part} {unit}, {state_full}"


def run_zip_flow(ext):
    zip_map = load_zip_to_same()
    if not zip_map:
        speak_tts("ZIP lookup is not configured. Please ask your administrator to install zip to same data.")
        return

    same_lookup = load_same_codes_lookup()

    # Allow up to 3 ZIP attempts before giving up.
    for zip_attempt in range(3):
        speak_tts("Enter your five digit zip code, or press star to cancel.")
        raw = agi_get_digits_raw(5)
        if raw == "*":
            speak_tts("Cancelled.")
            return
        zip_code = re.sub(r"\D", "", raw)
        if len(zip_code) != 5:
            speak_tts("That doesn't look like a five digit zip code.")
            continue

        matches = normalize_zip_matches(zip_map, zip_code, same_lookup)
        if matches:
            break
        # ZIP not found — offer retry before giving up.
        if zip_attempt < 2:
            speak_tts("No areas were found for that zip code. Please try another zip code.")
        else:
            speak_tts("No areas were found for that zip code.")
            return
    else:
        return

    # County disambiguation.
    county_index = 0
    if len(matches) > 1:
        labels = [spoken_county_label(m["county"]) for m in matches]
        sel = choose_from_numbered_list(labels, "Multiple areas match that zip code.")
        if sel is CANCELLED:
            speak_tts("Cancelled.")
            return
        if sel is None:
            speak_tts("Sorry, I couldn't understand your selection.")
            return
        county_index = sel

    selected = matches[county_index]
    spoken = spoken_county_label(selected["county"])
    codes = selected["codes"]

    # SAME code disambiguation (rare, but possible).
    code_index = 0
    if len(codes) > 1:
        code_labels = [f"S A M E code {' '.join(c)}" for c in codes]
        sel = choose_from_numbered_list(code_labels, f"{spoken} has multiple S A M E codes.")
        if sel is CANCELLED:
            speak_tts("Cancelled.")
            return
        if sel is None:
            speak_tts("Sorry, I couldn't understand your selection.")
            return
        code_index = sel

    code = codes[code_index]

    # Confirmation before saving.
    speak_tts(f"Subscribe to {spoken}? Press 1 to confirm, or star to cancel.")
    raw = agi_get_digits_raw(1)
    if raw != "1":
        speak_tts("Cancelled.")
        return

    # Duplicate guard.
    added = upsert_code(ext, code)
    if added:
        speak_tts(f"Subscribed to {spoken}.")
    else:
        speak_tts(f"You are already subscribed to {spoken}.")

def speak_tts(text):
    try:
        SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
        base = "tts_" + hashlib.sha1(text.encode()).hexdigest()[:12]
        raw = Path(tempfile.gettempdir()) / (base + ".wav")
        final = SOUNDS_DIR / (base + ".wav")
        final16 = SOUNDS_DIR / (base + ".wav16")

        subprocess.run(["pico2wave", "-l", VOICE, "-w", str(raw), text], check=True)
        subprocess.run([
            "sox", str(raw), "-r", "16000", "-c", "1", "-b", "16", "-e", "signed-integer",
            str(final), "norm", "-3"
        ], check=True)

        if final16.exists():
            final16.unlink(missing_ok=True)
        final.rename(final16)

        try:
            os.chown(final16, pwd.getpwnam("asterisk").pw_uid, grp.getgrnam("asterisk").gr_gid)
        except Exception:
            pass

        agi_cmd(f'STREAM FILE custom/{base} ""')
        try:
            raw.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as e:
        logging.info(f"TTS error: {e}")
        # fall back to a builtin prompt if TTS fails
        agi_cmd('STREAM FILE sorry ""')

def main():
    agi_read_env()
    args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    mode = args.get("mode", "").strip()
    ext = re.sub(r"\D", "", args.get("ext", ""))
    code = re.sub(r"\D", "", args.get("code", ""))

    logging.info(f"AGI mode={mode} ext={ext} code={code}")

    if not ext:
        speak_tts("Sorry. No extension detected.")
        return

    try:
        if mode == "list":
            codes = list_codes(ext)
            if not codes:
                speak_tts("You are not subscribed to any areas.")
            else:
                same_lookup = load_same_codes_lookup()
                labels = [spoken_county_label(same_lookup.get(c, c)) for c in codes]
                speak_tts(f"You are subscribed to {len(labels)} area{'s' if len(labels) != 1 else ''}. Press any key to stop.")
                for label in labels:
                    # short inter-item pause; if user pressed anything, stop reading
                    resp = agi_cmd("GET DATA beep 500 1")
                    val = agi_result_value(resp)
                    if val and val != "":
                        break
                    speak_tts(label + ".")
            return

        if mode == "remove_flow":
            codes = list_codes(ext)
            if not codes:
                speak_tts("You are not subscribed to any areas.")
                return
            same_lookup = load_same_codes_lookup()
            labels = [spoken_county_label(same_lookup.get(c, c)) for c in codes]
            if len(codes) > 9:
                speak_tts(f"You have {len(codes)} subscriptions. Showing the first 9.")
            sel = choose_from_numbered_list(labels, "Which area would you like to remove?")
            if sel is CANCELLED:
                speak_tts("Cancelled.")
                return
            if sel is None:
                speak_tts("Sorry, I couldn't understand your selection.")
                return
            chosen_code = codes[sel]
            remove_code(ext, chosen_code)
            speak_tts(f"{labels[sel]} removed.")
            return

        if mode == "zip_flow":
            run_zip_flow(ext)
            return

        speak_tts("Unknown request.")
    except Exception as e:
        logging.info(f"AGI error: {e}")
        speak_tts("An error occurred.")
        return

if __name__ == "__main__":
    main()
