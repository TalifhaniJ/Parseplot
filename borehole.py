"""
parse_borehole.py — Pure Python parser for Mukona dotPLOT geotechnical PDFs.

Handles two log types from job MK-26-817 (and structurally identical jobs):
  • Trial pits  — TP 01 … TP 42  (substation PDF)
  • Road TPs    — R-TP 01 … R-TP 06  (access road PDF)

Outputs one CSV per invocation:
  borehole_intervals.csv  — lithology intervals, GEOLOQS-compatible schema

Usage:
    python borehole.py site_a.pdf
    python borehole.py site_a.pdf site_b.pdf --out-dir ./output
"""

import csv
import io
import re
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# 1. Helpers
# ─────────────────────────────────────────────────────────────────────────────

def dms_to_decimal(s):
    """
    Convert DMS string to decimal degrees.
    Handles dotPLOT's (cid:176) encoding for ° and mixed punctuation.
      e.g.  "33´(cid:176) 2'4.46\"S"  →  -33.034572
    """
    s = re.sub(r'\(cid:\d+\)', ' ', str(s))   # strip PDF glyph escapes
    m = re.search(r'(\d+)[^\d]+(\d+)[^\d]+([\d.]+)[^\d]*([NSEWnsew])', s)
    if not m:
        return None
    deg  = int(m.group(1))
    mins = int(m.group(2))
    secs = float(m.group(3))
    hemi = m.group(4).upper()
    val  = deg + mins / 60 + secs / 3600
    return round(-val if hemi in ('S', 'W') else val, 6)


def strip_page_header(text):
    """Remove dotPLOT page header so it does not pollute material descriptions."""
    # The lithology body always starts at depth 0.00 on its own line
    m = re.search(r"(?m)^[ \t]*0\.00[ \t]*$", text)
    if m:
        return text[m.end():].lstrip()
    # Fallback: drop known header boilerplate lines
    header_pat = re.compile(
        r"Dpower|Geotechnical Investigation|Poseidon|Cookhouse"
        r"|Eastern Cape|JOB NUMBER|Sheet \d+ of|HHHH|OOOO|NNNN",
        re.IGNORECASE
    )
    kept = [ln for ln in text.splitlines() if not header_pat.search(ln)]
    return "\n".join(kept).strip()


def decode_quad(line):
    """
    dotPLOT repeats every character 4×:
      'TTTTPPPP 00001111'  →  'TP 01'
      'RRRR----TTTTPPPP 00001111'  →  'R-TP 01'
    Works on the raw line (spaces preserved then collapsed).
    """
    # Keep the spaces so segment lengths stay correct, then decode
    s = line.strip()
    # find a run of repeated 4× chars
    result = []
    i = 0
    while i < len(s):
        c = s[i]
        result.append(c)
        # skip the next 3 identical chars (or just move 1 if at word boundary)
        j = i + 1
        skip = 0
        while j < len(s) and s[j] == c and skip < 3:
            j += 1
            skip += 1
        i = j
    return ''.join(result)


def to_num(v):
    v = str(v).strip()
    if not v:
        return None
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Hole ID extraction
#    dotPLOT encodes the hole number in 4× repeated chars in two places:
#      • header line:  "Dpower HHHHOOOOLLLLEEEE NNNNoooo:::: TTTTPPPP 00001111"
#      • footer repeat: "HHHHOOOOLLLLEEEE NNNNoooo:::: RRRR----TTTTPPPP 00001111"
# ─────────────────────────────────────────────────────────────────────────────

# Matches the quad-encoded hole ID block on a line
_QUAD_HOLE_LINE = re.compile(
    r'(?:HHHH|HHHHOOOO).*?(?:RRRR----)?TTTTPPPP\s+([\d]{4,})',
    re.IGNORECASE
)


def parse_hole_id(full_text):
    """
    Extract hole ID from dotPLOT quad-encoded text.
    Returns e.g. 'TP 01', 'R-TP 06', or None.
    """
    for line in full_text.splitlines():
        # Quick filter — must contain the quad-repeated 'HOLE No:' block
        if 'HHHH' not in line and 'TTTTPPPP' not in line:
            continue
        decoded = decode_quad(line)
        # Match 'TP 01', 'R-TP 01', 'TP 42', etc.
        m = re.search(r'(R-TP|TP)\s*(\d+)', decoded, re.IGNORECASE)
        if m:
            prefix = m.group(1).upper()
            num    = int(m.group(2))
            return f'{prefix} {num:02d}'
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. End depth — trial pits use "Refusal on … @X.XXm" instead of
#    "End of borehole at X.XX"
# ─────────────────────────────────────────────────────────────────────────────

_REFUSAL = re.compile(
    r'Refusal\s+on\s+[^@]+@\s*([\d.]+)\+?\s*m',
    re.IGNORECASE
)
_EOB = re.compile(r'End\s+of\s+borehole\s+at\s*([\d.]+)', re.IGNORECASE)


def parse_end_depth(full_text):
    """Return refusal / end-of-borehole depth (m), or None."""
    m = _REFUSAL.search(full_text) or _EOB.search(full_text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Coordinates & elevation
# ─────────────────────────────────────────────────────────────────────────────

def parse_coordinates(full_text):
    lat = lon = elev = None

    mx = re.search(r'X-COORD\s*:\s*(.+?[NS])', full_text)
    if mx:
        lat = dms_to_decimal(mx.group(1))

    my = re.search(r'Y-COORD\s*:\s*(.+?[EW])', full_text)
    if my:
        lon = dms_to_decimal(my.group(1))

    me = re.search(r'ELEVATION\s*:\s*([\d]+)\s*m', full_text, re.IGNORECASE)
    if me:
        try:
            elev = int(me.group(1))
        except ValueError:
            pass

    return lat, lon, elev


# ─────────────────────────────────────────────────────────────────────────────
# 5. Sample flag
# ─────────────────────────────────────────────────────────────────────────────

def parse_sample(full_text):
    """
    Returns a short label if a sample was taken, e.g. 'FI+CBR', 'FI', 'Boulder'
    or '' if no sample.
    """
    if re.search(r'No sample was taken', full_text, re.IGNORECASE):
        return ''
    m = re.search(
        r'(?:Disturbed|Boulder)\s+sample\s+taken\s+for\s+([\w\s+]+?)\s+(?:testing|and)',
        full_text, re.IGNORECASE
    )
    if m:
        return m.group(1).strip().replace(' and ', '+').upper()
    if re.search(r'Disturbed sample', full_text, re.IGNORECASE):
        return 'DISTURBED'
    if re.search(r'Boulder sample', full_text, re.IGNORECASE):
        return 'BOULDER'
    return ''


# ─────────────────────────────────────────────────────────────────────────────
# 6. Depth boundary extraction
#    In dotPLOT trial pit logs every lithology boundary appears as a lone
#    decimal number on the left margin (the "depth scale" column).
#    The header Scale line e.g. "Scale 1:10" must be excluded.
# ─────────────────────────────────────────────────────────────────────────────

_SKIP_LINE = re.compile(
    r'(?:Scale|NOTES?|Refusal|Groundwater|Sidewall|Sample|CONTRACTOR'
    r'|MACHINE|DRILLED|PROFILED|TYPE\s+SET|SETUP|INCLINATION|DIAM'
    r'|DATE|TEXT|ELEVATION|X-COORD|Y-COORD|Mukona|dotPLOT'
    r'|\d+:\d+)',   # scale ratios like 1:10
    re.IGNORECASE
)


def collect_depth_boundaries(lines, end_depth):
    """
    Gather all floating-point depths that appear to be layer boundaries.
    Only picks up values in (0, end_depth * 1.1] range and ignores
    lines that are metadata / notes.
    """
    candidates = {0.0, end_depth}
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _SKIP_LINE.search(stripped):
            continue
        # A boundary line is typically ONLY a depth number (possibly with
        # trailing whitespace).  We also accept lines where the depth appears
        # at the very start before any text.
        for m in re.finditer(r'\b(\d+\.\d+)\b', stripped):
            val = float(m.group(1))
            if 0.0 < val <= end_depth * 1.1:
                candidates.add(round(val, 3))
    return sorted(candidates)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Lithology type patterns — extended for Karoo / Eastern Cape geology
# ─────────────────────────────────────────────────────────────────────────────

_LITH_TYPE = re.compile(
    r':\s*(?:[A-Z][A-Za-z /\-]*)?\b(?:'
    # Transported soils
    r'AEOLIAN|HILLWASH|HILWASH|ALLUVIUM|FILL|TOPSOIL'
    # Pedogenic / calcrete
    r'|PEDOGENIC|CALCRETE|HARDPAN'
    # Residual soils
    r'|RESIDUAL\s+(?:MUDSTONE|SANDSTONE|SCHIST|GRANITE|BASALT|DOLERITE)'
    r'|RESIDUAL'
    # Bedrock — Karoo Supergroup + intrusions
    r'|MUDSTONE|SANDSTONE|SHALE|SILTSTONE|TILLITE|COAL'
    r'|DIABASE|DOLERITE|BASALT'
    r'|SCHIST|GNEISS|QUARTZITE|GRANITE|GRANODIORITE|NORITE'
    r'|LIMESTONE|CHERT'
    r')\b[\w /,.\-]*\.?',
    re.IGNORECASE
)

_NOISE = re.compile(
    r'\b(?:Scale|NOTES?|NWD\d?|SPT|HQ|BQ|PQ|UCS|Ref)\b'
    r'|\b\d{1,3}:\d{2,3}\b',   # scale ratios
    re.IGNORECASE
)


def _clean(text):
    text = _NOISE.sub(' ', text)
    text = re.sub(r'^[\d\s.\-/:()]+', '', text)
    return ' '.join(text.split()).strip()


# ─────────────────────────────────────────────────────────────────────────────
# 8. Lithology interval parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_lithology(full_text, end_depth):
    """
    Parse lithology intervals from a cleaned dotPLOT trial pit page.

    Text structure after strip_page_header():
        Scale 0.00
        <description of layer 1>
        1:10
        0.15              ← boundary depth
        <description of layer 2>
        0.50              ← boundary depth / refusal depth
        <description of basal rock>
        NOTES
        ...

    Strategy:
      1. Find all boundary depth numbers (lines that are purely a float).
      2. Slice the text between consecutive boundaries.
      3. In each slice, extract the best material description.
    """
    text = strip_page_header(full_text)
    lines = text.splitlines()

    # ── Step 1: Find depth-boundary lines ────────────────────────────────────
    # A boundary line is a line whose stripped content is ONLY a float number
    # (possibly with trailing +).  We exclude the footer/notes section.
    in_notes = False
    boundaries = []   # list of (depth, line_index)
    for i, line in enumerate(lines):
        s = line.strip().rstrip('+')
        if re.match(r'^NOTES?$', s, re.IGNORECASE):
            in_notes = True
        if in_notes:
            continue
        # Pure-float line → depth boundary
        if re.fullmatch(r'\d+\.\d+', s):
            val = float(s)
            if 0.0 <= val <= end_depth * 1.1:
                boundaries.append((val, i))

    # Always include 0.0 as the first boundary (may not appear as a standalone
    # line — it appears as "Scale 0.00")
    if not boundaries or boundaries[0][0] != 0.0:
        # find the "Scale 0.00" or "0.00" line
        for i, line in enumerate(lines):
            if re.search(r'0\.00', line):
                boundaries.insert(0, (0.0, i))
                break

    # Always include end_depth as last boundary
    if not boundaries or boundaries[-1][0] != end_depth:
        boundaries.append((end_depth, len(lines)))

    # Remove duplicates, sort
    seen = set()
    unique = []
    for d, idx in sorted(boundaries, key=lambda x: x[0]):
        if d not in seen:
            seen.add(d)
            unique.append((d, idx))
    boundaries = unique

    if len(boundaries) < 2:
        return []

    # ── Step 2: Slice text between consecutive boundaries ────────────────────
    intervals = []
    for i in range(len(boundaries) - 1):
        d_top, line_start = boundaries[i]
        d_bot, line_end   = boundaries[i + 1]

        # Grab lines between the two boundary line indices (exclusive of
        # the boundary lines themselves)
        segment_lines = lines[line_start + 1 : line_end]

        # Filter out scale ratio lines ("1:10"), empty lines, and the
        # boundary depth value itself if it leaked in
        clean_lines = []
        for ln in segment_lines:
            s = ln.strip()
            if not s:
                continue
            if re.fullmatch(r'\d+:\d+', s):          # scale ratio
                continue
            if re.fullmatch(r'\d+\.\d+\+?', s):      # stray depth number
                continue
            if _SKIP_LINE.search(s):
                continue
            clean_lines.append(s)

        segment = ' '.join(clean_lines)

        # ── Step 3: Extract lith_type and clean description ──────────────────
        best = None
        for m in _LITH_TYPE.finditer(segment):
            best = m

        if best:
            raw_desc = segment[:best.end()]
            # Extract the lith code from the colon-suffix
            lith_match = re.search(
                r':\s*((?:RESIDUAL\s+)?(?:AEOLIAN|HILLWASH|HILWASH|PEDOGENIC'
                r'|MUDSTONE|SANDSTONE|CALCRETE|HARDPAN|RESIDUAL|DIABASE'
                r'|DOLERITE|SCHIST|ALLUVIUM|FILL|TOPSOIL)[A-Z/ ]*)',
                best.group(0), re.IGNORECASE
            )
            lith_code = lith_match.group(1).strip().upper() if lith_match else ''
        else:
            raw_desc = segment
            lith_code = ''

        desc = _clean(raw_desc)

        # Discard interval if description is suspiciously short or just noise
        if len(desc) < 5:
            desc = ''

        intervals.append({
            'depth_top':    d_top,
            'depth_bottom': d_bot,
            'material':     desc,
            'lith_type':    lith_code,
        })

    return intervals

# ─────────────────────────────────────────────────────────────────────────────
# 9. Per-page parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_page(page):
    full_text = page.extract_text() or ''
    if not full_text.strip():
        return None

    hole_id   = parse_hole_id(full_text)
    end_depth = parse_end_depth(full_text)

    # Skip legend / summary pages
    if not hole_id or not end_depth:
        return None

    lat, lon, elev = parse_coordinates(full_text)
    sample         = parse_sample(full_text)
    intervals      = parse_lithology(full_text, end_depth)

    return {
        'hole_id':   hole_id,
        'lat':       lat,
        'lon':       lon,
        'elevation': elev,
        'end_depth': end_depth,
        'sample':    sample,
        'intervals': intervals,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 10. Build output rows
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_depth(v):
    return int(v) if isinstance(v, float) and v == int(v) else v


def build_interval_rows(bh):
    rows = []
    for iv in bh['intervals']:
        rows.append({
            'borehole_id':  bh['hole_id'],
            'y_position':   bh['lat'],
            'x_position':   bh['lon'],
            'elevation':    bh['elevation'],
            'depth_top':    _fmt_depth(iv['depth_top']),
            'depth_bottom': _fmt_depth(iv['depth_bottom']),
            'material':     iv['material'],
            'lith_type':    iv['lith_type'],
            'sample':       bh['sample'],
            'inclination':  0,
            'azimuth':      0,
            'water_level':  0,          # no GW encountered in any TP
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 11. CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

INTERVAL_COLS = [
    'x_position', 'y_position', 'borehole_id',
    'water_level', 'depth_top', 'depth_bottom', 'material',
]


def write_csv(rows, fieldnames):
    if not rows:
        return ''
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fieldnames, extrasaction='ignore',
                       lineterminator='\r\n')
    w.writeheader()
    for row in rows:
        w.writerow({k: ('' if row.get(k) is None else row[k]) for k in fieldnames})
    return out.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# 12. Main pipeline (importable)
# ─────────────────────────────────────────────────────────────────────────────

def parse_pdf(pdf_path):
    """
    Parse all pages of a dotPLOT trial pit PDF.
    Returns list of interval row dicts.
    """
    import pdfplumber
    all_rows = []
    with pdfplumber.open(pdf_path, laparams={'detect_vertical': False}) as pdf:
        for page in pdf.pages:
            bh = parse_page(page)
            if not bh:
                continue
            all_rows.extend(build_interval_rows(bh))
    return all_rows


# ─────────────────────────────────────────────────────────────────────────────
# 13. CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description='Parse Mukona trial pit PDF → CSV')
    ap.add_argument('pdfs', nargs='+', help='One or more PDF files')
    ap.add_argument('--out-dir', default='.', help='Output directory (default: cwd)')
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_rows = []
    for pdf_path in args.pdfs:
        rows = parse_pdf(pdf_path)
        print(f'{Path(pdf_path).name}: {len(rows)} interval rows parsed')
        all_rows.extend(rows)

    out_path = out_dir / 'borehole_intervals.csv'
    out_path.write_text(write_csv(all_rows, INTERVAL_COLS), encoding='utf-8')
    print(f'\nTotal: {len(all_rows)} rows  →  {out_path}')


if __name__ == '__main__':
    main()
