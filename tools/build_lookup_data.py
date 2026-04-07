#!/usr/bin/env python3
"""
Build offline lookup artifacts for ZIP-first SAME enrollment.

Outputs:
- zip_to_same.json: ZIP -> county -> SAME codes
- data/same_metadata.json: normalized SAME metadata with county/state names
- data/same_codes_enriched.json: full SAME list with state/county/zip_codes

Usage:
  python3 tools/build_lookup_data.py \
    --same-codes sameCodes.json \
    --zip-county-csv data/zip_county.csv \
    --out-zip zip_to_same.json \
    --out-same data/same_metadata.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

WEATHER_BASE = "https://www.weather.gov/nwr/county_coverage?State="
USER_AGENT = "asterisk-nws-alerts-build/1.0"


def normalize_county_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    cleaned = re.sub(
        r"\b(county|parish|borough|census area|municipality|city and borough|city)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    parts = cleaned.upper().split()
    if parts:
        if parts[0] == "ST":
            parts[0] = "SAINT"
        elif parts[0] == "STE":
            parts[0] = "SAINTE"

    return " ".join(parts)


def parse_same_codes(path: Path) -> Dict[str, dict]:
    raw = json.loads(path.read_text())
    merged: Dict[str, str] = {}

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                merged.update({str(k): str(v) for k, v in item.items()})
    elif isinstance(raw, dict):
        merged.update({str(k): str(v) for k, v in raw.items()})
    else:
        raise ValueError("sameCodes.json must be a list/dict mapping SAME->county")

    result: Dict[str, dict] = {}
    for code, label in merged.items():
        same = re.sub(r"\D", "", code)
        if not same:
            continue

        parts = [p.strip() for p in label.split(",")]
        if len(parts) < 2:
            county = label.strip()
            state_abbr = ""
        else:
            county = parts[0]
            state_abbr = parts[-1].upper()

        county_norm = normalize_county_name(county)
        county_key = f"{county_norm}|{state_abbr}"

        result[same] = {
            "same_code": same,
            "valid_same": len(same) == 6,
            "state_fips": same[:3] if len(same) >= 3 else "",
            "county_fips": same[-3:] if len(same) >= 3 else "",
            "county_name": county,
            "state_abbr": state_abbr,
            "county_norm": county_norm,
            "county_key": county_key,
            "weather_county_name": county,
        }

    return result


def discover_weather_counties(state_abbr: str) -> Set[str]:
    """
    Best-effort extractor for county names from weather.gov county_coverage page.
    """
    if not state_abbr:
        return set()

    url = WEATHER_BASE + state_abbr
    req = Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except (URLError, HTTPError, TimeoutError):
        return set()

    # Extract likely county-name table cell content.
    candidates = set()
    for m in re.finditer(r">\s*([A-Za-z][A-Za-z .'-]{1,60})\s*<", html):
        txt = m.group(1).strip()
        if any(token in txt.lower() for token in ["county", "parish", "borough", "municipality", "census", "city and borough"]):
            candidates.add(txt)

    return candidates


def apply_weather_names(same_map: Dict[str, dict], enable_fetch: bool) -> None:
    if not enable_fetch:
        return

    by_state: Dict[str, List[str]] = defaultdict(list)
    for same, meta in same_map.items():
        by_state[meta["state_abbr"]].append(same)

    for state_abbr, codes in by_state.items():
        weather_names = discover_weather_counties(state_abbr)
        if not weather_names:
            continue

        weather_by_norm: Dict[str, str] = {}
        for name in weather_names:
            weather_by_norm[normalize_county_name(name)] = name

        for same in codes:
            norm = same_map[same]["county_norm"]
            preferred = weather_by_norm.get(norm)
            if preferred:
                same_map[same]["weather_county_name"] = preferred


def read_zip_county_rows(path: Path) -> List[dict]:
    rows: List[dict] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({k.lower().strip(): (v or "").strip() for k, v in r.items()})
    return rows


def row_get(row: dict, names: List[str]) -> str:
    for n in names:
        if n in row and row[n]:
            return row[n]
    return ""


def build_zip_to_same(zip_rows: List[dict], same_map: Dict[str, dict]) -> Dict[str, List[dict]]:
    county_to_codes: Dict[str, Set[str]] = defaultdict(set)
    county_label: Dict[str, str] = {}
    fips_to_codes: Dict[Tuple[str, str], Set[str]] = defaultdict(set)

    for same, meta in same_map.items():
        if not meta.get("valid_same", False):
            continue
        key = meta["county_key"]
        county_to_codes[key].add(same)
        county_label[key] = f"{meta['weather_county_name']}, {meta['state_abbr']}"

        state_abbr = meta.get("state_abbr", "")
        county_fips = meta.get("county_fips", "")
        if state_abbr and county_fips and len(county_fips) == 3 and county_fips.isdigit():
            fips_to_codes[(state_abbr, county_fips)] .add(same)

    out: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))

    for row in zip_rows:
        zip_raw = row_get(row, ["zip", "zipcode", "zip_code", "zcta", "zcta5", "postal_code"])
        county_raw = row_get(row, ["county", "county_name", "cntyname", "county_nam", "admin2_name"])
        state_raw = row_get(row, ["state", "state_abbr", "stusps", "state_code", "admin1_code"])
        county_fips_raw = row_get(row, ["county_fips", "countyfp", "county_code", "admin2_code"])

        zip_code = re.sub(r"\D", "", zip_raw)
        if len(zip_code) == 9:
            zip_code = zip_code[:5]
        if len(zip_code) != 5:
            continue

        state_abbr = state_raw.upper()
        if not state_abbr:
            continue

        # Prefer FIPS join when county_fips is available.
        county_fips = re.sub(r"\D", "", county_fips_raw).zfill(3) if county_fips_raw else ""
        codes: Set[str] = set()
        if county_fips and len(county_fips) == 3:
            codes = set(fips_to_codes.get((state_abbr, county_fips), set()))

        # Fallback to county-name join.
        if not codes:
            county_norm = normalize_county_name(county_raw)
            if county_norm:
                key = f"{county_norm}|{state_abbr}"
                codes = set(county_to_codes.get(key, set()))

                if not codes and county_norm.startswith("CITY OF "):
                    alt_key = f"{county_norm.replace('CITY OF ', '', 1)}|{state_abbr}"
                    codes = set(county_to_codes.get(alt_key, set()))

                if not codes and county_norm.endswith(" CITY"):
                    alt_key = f"{county_norm[:-5]}|{state_abbr}"
                    codes = set(county_to_codes.get(alt_key, set()))

        if not codes:
            continue

        if county_raw:
            label = f"{county_raw}, {state_abbr}"
        else:
            # Build label from SAME metadata if county name missing in input row.
            sample_code = sorted(codes)[0]
            meta = same_map[sample_code]
            label = f"{meta['weather_county_name']}, {state_abbr}"

        out[zip_code][label].update(codes)

    final: Dict[str, List[dict]] = {}
    for zip_code in sorted(out.keys()):
        counties = []
        for county in sorted(out[zip_code].keys()):
            codes = sorted(out[zip_code][county])
            if codes:
                counties.append({"county": county, "codes": codes})
        if counties:
            final[zip_code] = counties

    return final


def build_same_to_zips(zip_to_same: Dict[str, List[dict]]) -> Dict[str, Set[str]]:
    same_to_zips: Dict[str, Set[str]] = defaultdict(set)
    for zip_code, counties in zip_to_same.items():
        for row in counties:
            for code in row.get("codes", []):
                if isinstance(code, str) and len(code) == 6 and code.isdigit():
                    same_to_zips[code].add(zip_code)
    return same_to_zips


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")


def validate_outputs(zip_to_same: Dict[str, List[dict]], same_map: Dict[str, dict]) -> Tuple[int, int]:
    bad_same = 0
    dangling = 0
    same_codes = set(same_map.keys())

    for zip_code, rows in zip_to_same.items():
        if len(zip_code) != 5 or not zip_code.isdigit():
            bad_same += 1
        for row in rows:
            for code in row.get("codes", []):
                if len(code) != 6 or not code.isdigit():
                    bad_same += 1
                if code not in same_codes:
                    dangling += 1

    return bad_same, dangling


def main() -> int:
    p = argparse.ArgumentParser(description="Build ZIP->SAME lookup artifacts")
    p.add_argument("--same-codes", default="sameCodes.json")
    p.add_argument("--zip-county-csv", default="data/zip_county.csv")
    p.add_argument("--out-zip", default="zip_to_same.json")
    p.add_argument("--out-same", default="data/same_metadata.json")
    p.add_argument("--out-enriched", default="data/same_codes_enriched.json")
    p.add_argument("--no-weather-fetch", action="store_true")
    args = p.parse_args()

    same_codes_path = Path(args.same_codes)
    zip_csv_path = Path(args.zip_county_csv)

    if not same_codes_path.exists():
        print(f"ERROR: missing {same_codes_path}", file=sys.stderr)
        return 2
    if not zip_csv_path.exists():
        print(f"ERROR: missing {zip_csv_path}", file=sys.stderr)
        print("Provide a CSV with headers including zip/county/state columns.", file=sys.stderr)
        return 2

    same_map = parse_same_codes(same_codes_path)
    apply_weather_names(same_map, enable_fetch=not args.no_weather_fetch)

    zip_rows = read_zip_county_rows(zip_csv_path)
    zip_to_same = build_zip_to_same(zip_rows, same_map)
    same_to_zips = build_same_to_zips(zip_to_same)

    same_metadata = [
        {
            "same_code": same,
            "valid_same": meta["valid_same"],
            "county_name": meta["county_name"],
            "weather_county_name": meta["weather_county_name"],
            "state_abbr": meta["state_abbr"],
        }
        for same, meta in sorted(same_map.items())
    ]

    same_enriched = [
        {
            "same_code": same,
            "valid_same": meta["valid_same"],
            "state": meta["state_abbr"],
            "county": meta["county_name"],
            "zip_codes": sorted(same_to_zips.get(same, set())),
        }
        for same, meta in sorted(same_map.items())
    ]

    bad_same, dangling = validate_outputs(zip_to_same, same_map)

    write_json(Path(args.out_zip), zip_to_same)
    write_json(Path(args.out_same), same_metadata)
    write_json(Path(args.out_enriched), same_enriched)

    print(f"Wrote {args.out_zip} with {len(zip_to_same)} ZIP entries")
    print(f"Wrote {args.out_same} with {len(same_metadata)} SAME entries")
    print(f"Wrote {args.out_enriched} with {len(same_enriched)} SAME entries")
    if bad_same or dangling:
        print(f"WARN: validation found bad_or_non_numeric={bad_same}, dangling_same_refs={dangling}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
