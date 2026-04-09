"""
parse_testpit.py — Pure Python parser for test-pit geotechnical PDFs.
No LLM required. Outputs PART1.csv-compatible rows.

Usage:
    python parse_testpit.py test.pdf output.csv
"""

import csv
import io
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. DMS → decimal degrees
# ---------------------------------------------------------------------------

def dms_to_decimal(dms_str: str) -> float | None:
    """
    Convert a DMS string like  24°36'32.72"S  or  30° 7'55.06"E
    into a signed decimal degree float.
    Returns None if parsing fails.
    """
    # Normalise unicode degrees/minutes/seconds symbols
    s = (dms_str.strip()
         .replace('\u00b0', '°')
         .replace('\u2019', "'")
         .replace('\u2018', "'")
         .replace('\u201c', '"')
         .replace('\u201d', '"'))

    m = re.match(
        r"(\d+)\s*°\s*(\d+)\s*'\s*([\d.]+)\s*\"?\s*([NSEWnsew])",
        s
    )
    if not m:
        return None
    deg, minutes, secs, hemi = int(m.group(1)), int(m.group(2)), float(m.group(3)), m.group(4).upper()
    decimal = deg + minutes / 60 + secs / 3600
    if hemi in ('S', 'W'):
        decimal = -decimal
    return round(decimal, 6)


# ---------------------------------------------------------------------------
# 2. Normalise borehole ID → "TP-01" style
# ---------------------------------------------------------------------------

def normalise_bh_id(raw: str) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    # Match patterns: TP-01, TP01, TP – 01, TP–02, TP 3, TP1 …
    m = re.match(r"TP\s*[-–\s]?\s*0*(\d+)", s, flags=re.IGNORECASE)
    if m:
        return f"TP-{int(m.group(1)):02d}"
    return s


# ---------------------------------------------------------------------------
# 3. Parse depth range string → (depth_top, depth_bottom)
# ---------------------------------------------------------------------------

_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)"
)
_SINGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+")   # e.g. "0.70+m"

# Sentinel bottom depth for open-ended "outcrop" entries (matches PART1 convention)
_OUTCROP_SENTINEL_BOTTOM = 0.75


def parse_depth_range(s: str, is_outcrop: bool = False) -> tuple[float | None, float | None]:
    """
    Returns (depth_top, depth_bottom).
    For open-ended entries like "0.70+m" we set depth_bottom = depth_top + 0.05
    as a nominal termination (matches PART1 convention), EXCEPT for outcrop
    entries where we use _OUTCROP_SENTINEL_BOTTOM = 0.75.
    """
    if not s:
        return None, None
    m = _RANGE_RE.search(s)
    if m:
        top, bot = float(m.group(1)), float(m.group(2))
        # Fix degenerate ranges like 0.50-0.50 where top==bot
        # These are PDF typos; treat as (prev_bottom, prev_bottom + 0.10)
        # Caller must handle; just flag by returning None bottom so caller can correct
        if top == bot and top > 0:
            return None, None   # caller will fix as a typo
        return top, bot
    m = _SINGLE_RE.search(s)
    if m:
        top = float(m.group(1))
        if is_outcrop:
            return top, _OUTCROP_SENTINEL_BOTTOM
        return top, round(top + 0.05, 3)   # nominal +5 cm
    return None, None


# ---------------------------------------------------------------------------
# 4. Parse the location table (first table in PDF)
# ---------------------------------------------------------------------------

def parse_location_table(tables: list) -> dict:
    """
    Returns  { 'TP-01': (lat, lon), ... }
    from the first table which has columns: Test Pit No | Lat | Lon | Remarks
    """
    coords = {}
    for table in tables:
        for row in table:
            if not row or len(row) < 3:
                continue
            cell0 = str(row[0] or '').strip()
            bh = normalise_bh_id(cell0)
            if not bh:
                continue
            lat_str = str(row[1] or '').strip()
            lon_str = str(row[2] or '').strip()
            lat = dms_to_decimal(lat_str)
            lon = dms_to_decimal(lon_str)
            if lat is not None and lon is not None:
                coords[bh] = (lat, lon)
    return coords


# ---------------------------------------------------------------------------
# 5. Parse the location table's Remarks column → intervals per borehole
# ---------------------------------------------------------------------------

# Standardised material name mapping (PDF text → canonical)
_MATERIAL_MAP = [
    # pattern                            replacement
    (r"residual\s+norite\b",             "Residual Norites"),
    (r"residual\s+norites\b",            "Residual Norites"),
    (r"norite\s+bedrock\s*\(outcrop\)",  "Norite Bedrock (Outcrop)"),
    (r"norite\s+bedrock\b",              "Norite Bedrock"),
    (r"norites\s+bedrock\b",             "Norites Bedrock"),
    (r"pyroxinite\s+bedrock\b",          "Pyroxinite Bedrock"),
    (r"imported,?\s*gravel\s+wearing\s+coarse", "Imported Gravel wearing coarse"),
    (r"hillwash\b",                      "Hillwash"),
    (r"residual\b",                      "Residual Norites"),   # bare "Residual"
]

def _canonical_material(raw: str) -> str:
    s = raw.strip().rstrip('.,;')
    for pat, repl in _MATERIAL_MAP:
        if re.search(pat, s, flags=re.IGNORECASE):
            return repl
    return s.title() if s else ""


def parse_remarks(remarks_text: str, bh_id: str) -> list[dict]:
    """
    Parse a multi-line remarks cell such as:
        "0.00-0.70 Residual\n0.70+m: Norites Bedrock"
    Returns a list of interval dicts (no lab data yet).
    """
    intervals = []
    prev_bottom = 0.0

    for line in remarks_text.splitlines():
        line = line.strip()
        if not line:
            continue

        m_range = _RANGE_RE.search(line)
        m_single = _SINGLE_RE.search(line)

        if not m_range and not m_single:
            continue

        # Detect outcrop descriptions
        is_outcrop = bool(re.search(r"outcrop", line, flags=re.IGNORECASE))

        if m_range:
            raw_top = float(m_range.group(1))
            raw_bot = float(m_range.group(2))
            mat_raw = line[m_range.end():].strip(': \t')

            # Handle degenerate typo depths (e.g. 0.50-0.50 in PDF)
            if raw_top == raw_bot and raw_top > 0:
                depth_top = prev_bottom
                depth_bottom = raw_top
            else:
                depth_top = raw_top
                depth_bottom = raw_bot
        else:
            # open-ended "0.01+" etc.
            raw_top = float(m_single.group(1))
            depth_top = raw_top
            depth_bottom = _OUTCROP_SENTINEL_BOTTOM if is_outcrop else round(raw_top + 0.05, 3)
            mat_raw = line[m_single.end():].strip(': \t')

        material = _canonical_material(mat_raw)
        if not material:
            prev_bottom = depth_bottom
            continue

        prev_bottom = depth_bottom
        intervals.append({
            "borehole_id": bh_id,
            "depth_top":   depth_top,
            "depth_bottom": depth_bottom,
            "material":    material,
        })

    return intervals


# ---------------------------------------------------------------------------
# 6. Parse the lab results table (second table)
# ---------------------------------------------------------------------------

def parse_lab_table(tables: list) -> list[dict]:
    """
    Finds the lab table (has 'Hole No' / 'TP1' style rows) and extracts
    one row per sample with keys:
        bh_id, depth_top, depth_bottom, GM, LL, PI, LS, USC, USH, COLTO
    """
    lab_rows = []
    for table in tables:
        for row in table:
            if not row:
                continue
            cell0 = str(row[0] or '').strip()
            bh = normalise_bh_id(cell0)
            if not bh or len(row) < 8:
                continue
            # Validate: second cell should look like a depth range
            depth_str = str(row[1] or '').strip()
            d_top, d_bot = parse_depth_range(depth_str)
            if d_top is None:
                continue

            def _f(v):
                """Parse float, return None if not numeric."""
                try:
                    return float(str(v).strip())
                except (ValueError, TypeError):
                    return None

            def _s(v):
                s = str(v or '').strip()
                return s if s else None

            lab_rows.append({
                "bh_id":       bh,
                "depth_top":   d_top,
                "depth_bottom": d_bot,
                "GM":   _f(row[3]),
                "LL":   _f(row[4]),
                "PI":   _f(row[5]),
                "LS":   _f(row[6]),
                "USC":  _s(row[7]),
                "USH":  _s(row[8]) if len(row) > 8 else None,
                "COLTO": _s(row[9]) if len(row) > 9 else None,
            })

    return lab_rows


# ---------------------------------------------------------------------------
# 7. Build material bracket string  [GM=..., LL=..., ...]
# ---------------------------------------------------------------------------

def build_bracket(lab: dict) -> str:
    parts = []
    for key in ("GM", "LL", "PI", "LS"):
        v = lab.get(key)
        if v is not None:
            parts.append(f"{key}={v}")
    for key in ("USC", "USH", "COLTO"):
        v = lab.get(key)
        if v:
            parts.append(f"{key}={v}")
    return f"[{', '.join(parts)}]" if parts else ""


# ---------------------------------------------------------------------------
# 8. Merge lab data onto intervals
# ---------------------------------------------------------------------------

def merge_lab_onto_intervals(intervals: list[dict], lab_rows: list[dict]) -> list[dict]:
    """
    For each interval, find a matching lab row by bh_id and depth overlap.
    Exact match preferred; overlapping accepted as fallback.
    """
    # Index lab rows by bh_id
    from collections import defaultdict
    lab_by_bh: dict[str, list[dict]] = defaultdict(list)
    for lab in lab_rows:
        lab_by_bh[lab["bh_id"]].append(lab)

    def _overlaps(iv_top, iv_bot, lab_top, lab_bot):
        """True if [lab_top, lab_bot] overlaps [iv_top, iv_bot] by more than a point."""
        return lab_bot > iv_top and lab_top < iv_bot

    merged = []
    for iv in intervals:
        bh = iv["borehole_id"]
        iv_top = float(iv["depth_top"])
        iv_bot = float(iv["depth_bottom"])

        # Find matching lab rows (exact first, then overlap)
        candidates = lab_by_bh.get(bh, [])
        exact = [l for l in candidates if float(l["depth_top"]) == iv_top and float(l["depth_bottom"]) == iv_bot]
        overlap = [l for l in candidates if _overlaps(iv_top, iv_bot, float(l["depth_top"]), float(l["depth_bottom"]))]
        lab = exact[0] if exact else (overlap[0] if overlap else None)

        if lab:
            bracket = build_bracket(lab)
            material = f"{iv['material']} {bracket}".strip() if bracket else iv["material"]
        else:
            material = iv["material"]

        # Emit integer depths as ints (e.g. 0 not 0.0)
        def _clean_depth(v):
            if v == int(v):
                return int(v)
            return v

        merged.append({
            "borehole_id":  bh,
            "y_position":   iv.get("y_position"),
            "x_position":   iv.get("x_position"),
            "depth_top":    _clean_depth(iv_top),
            "depth_bottom": _clean_depth(iv_bot),
            "material":     material,
            "inclination":  0,
            "azimuth":      0,
            "water_level":  0,
        })

    return merged


# ---------------------------------------------------------------------------
# 9. Main pipeline
# ---------------------------------------------------------------------------

def parse_pdf(pdf_path: str) -> list[dict]:
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        all_tables = []
        for page in pdf.pages:
            all_tables.extend(page.extract_tables())

    # --- Step A: coordinates from location table
    coords = parse_location_table(all_tables)

    # --- Step B: intervals from remarks column
    intervals: list[dict] = []
    for table in all_tables:
        for row in table:
            if not row or len(row) < 4:
                continue
            cell0 = str(row[0] or '').strip()
            bh = normalise_bh_id(cell0)
            if not bh:
                continue
            remarks = str(row[3] or '').strip()
            if not remarks:
                continue
            ivs = parse_remarks(remarks, bh)
            if not ivs:
                continue
            lat, lon = coords.get(bh, (None, None))
            for iv in ivs:
                iv["y_position"] = lat
                iv["x_position"] = lon
            intervals.extend(ivs)

    # --- Step C: lab table
    lab_rows = parse_lab_table(all_tables)

    # --- Step D: merge
    rows = merge_lab_onto_intervals(intervals, lab_rows)

    # Sort by borehole id then depth
    def _sort_key(r):
        m = re.search(r"\d+", r.get("borehole_id") or "")
        bh_num = int(m.group()) if m else 0
        return (bh_num, float(r.get("depth_top") or 0))

    rows.sort(key=_sort_key)
    return rows


def rows_to_csv(rows: list[dict]) -> str:
    columns = [
        "borehole_id", "y_position", "x_position",
        "depth_top", "depth_bottom", "material",
        "inclination", "azimuth", "water_level",
    ]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ('' if row.get(k) is None else row.get(k)) for k in columns})
    return out.getvalue()


# ---------------------------------------------------------------------------
# 10. CLI entry point
# ---------------------------------------------------------------------------


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: parse-testpit <input.pdf> [output.csv]")
        sys.exit(1)
        
    pdf_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else "output.csv"

    rows = parse_pdf(pdf_path)
    csv_str = rows_to_csv(rows)

    Path(out_path).write_text(csv_str, encoding="utf-8")
    print(f"Wrote {len(rows)} rows → {out_path}")

if __name__ == "__main__":
    main()