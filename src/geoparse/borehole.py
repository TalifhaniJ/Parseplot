"""
parse_borehole.py — Pure Python parser for geotechnical borehole log PDFs
                    (dotPLOT / Mukona format, one borehole per page).

Outputs two CSVs:
  1. borehole_intervals.csv  — lithology intervals, PART1-compatible schema
  2. drilling_runs.csv       — per-run drilling data (method/depth/RQD/recovery)

Usage:
    python parse_borehole.py a.pdf
    python parse_borehole.py a.pdf --out-dir ./output
"""

import csv
import io
import re
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# 1. Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def dms_to_decimal(s):
    """'23°24′15.16″S' → -23.404211  (handles mixed punctuation)."""
    m = re.search(r'(\d+)[^\d]+(\d+)[^\d]+([\d.]+)[^\d]*([NSEWnsew])', str(s))
    if not m:
        return None
    deg, mins, secs, hemi = int(m.group(1)), int(m.group(2)), float(m.group(3)), m.group(4).upper()
    val = deg + mins / 60 + secs / 3600
    return round(-val if hemi in ('S', 'W') else val, 6)


def decode_quad(line):
    """dotPLOT repeats each char 4×: 'BBBBHHHH00001111' → 'BH01'."""
    s = line.strip().replace(' ', '')
    return ''.join(s[i] for i in range(0, len(s), 4))


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
# 2. Strip page header (legend boilerplate)
# ─────────────────────────────────────────────────────────────────────────────

_HEADER_END = re.compile(r'VWJ-very wide spacng\s+IRR-irregular', re.IGNORECASE)

def strip_header(text):
    m = _HEADER_END.search(text)
    return text[m.end():].strip() if m else text


# ─────────────────────────────────────────────────────────────────────────────
# 3. Page metadata
# ─────────────────────────────────────────────────────────────────────────────

def parse_metadata(full_text):
    meta = {}

    # Hole ID — dotPLOT encodes as 4×-repeated chars, e.g. "BBBBHHHH00001111"
    for line in full_text.splitlines():
        s = line.strip().replace(' ', '')
        if len(s) >= 8 and len(s) % 4 == 0:
            decoded = decode_quad(s)
            bh = re.search(r'BH(\d+)', decoded)
            if bh:
                meta['hole_id'] = 'BH{:02d}'.format(int(bh.group(1)))
                break

    # Coordinates (in footer)
    mx = re.search(r'X-COORD\s*:([\S ]{5,35}?[NS])', full_text)
    if mx:
        meta['lat'] = dms_to_decimal(mx.group(1))
    my = re.search(r'Y-COORD\s*:([\S ]{5,35}?[EW])', full_text)
    if my:
        meta['lon'] = dms_to_decimal(my.group(1))

    # Water level
    mw = re.search(r'(?:seepage|rest level)[^\d]*([\d.]+)\s*m', full_text, re.IGNORECASE)
    if mw:
        meta['water_level'] = float(mw.group(1))

    # End-of-borehole depth (handle PDF typos like "8.69.0")
    me = re.search(r'End of borehole at\s*([\d.]+)', full_text)
    if me:
        raw = me.group(1).rstrip('.')
        parts = raw.split('.')
        raw = parts[0] + ('.' + parts[1] if len(parts) > 1 else '')
        try:
            meta['end_depth'] = float(raw)
        except ValueError:
            pass

    return meta


# ─────────────────────────────────────────────────────────────────────────────
# 4. Drilling runs
# ─────────────────────────────────────────────────────────────────────────────

def parse_drilling_runs(tables):
    """
    The pdfplumber table has a hybrid structure:
      • Row 0  — stacks ALL runs' mat_recovery, core_recovery, RQD in cols 3-5
      • Later rows — one row per run with method, depth, frac_freq (cols 1,2,6)
                     (cols 3-5 are None because they were already packed in row 0)

    Strategy:
      1. Scan every row for (method, depth, frac_freq) → ordered run list
      2. Use row 0 cols 3-5 for mat_recovery / core_recovery / RQD (newline-split)
      3. Zip together by index
    """
    for table in tables:
        if not table:
            continue

        # Step 1 – collect individual run rows (method + depth both present)
        run_rows = []
        for row in table:
            if not row or len(row) < 3:
                continue
            method = str(row[1] or '').strip()
            depth  = str(row[2] or '').strip()
            if re.search(r'NWD\d?|SPT|HQ|BQ|PQ', method, re.IGNORECASE) and re.fullmatch(r'\d+\.\d+', depth):
                frac = str(row[6] or '').strip() if len(row) > 6 else ''
                spt  = str(row[7] or '').strip() if len(row) > 7 else ''
                run_rows.append({
                    'method':   method,
                    'depth_to': float(depth),
                    'frac':     frac,
                    'spt':      spt,
                })

        if not run_rows:
            continue

        # Step 2 – stacked recovery / RQD from row 0
        row0 = table[0]
        mat_recs  = str(row0[3] or '').split('\n') if len(row0) > 3 else []
        core_recs = str(row0[4] or '').split('\n') if len(row0) > 4 else []
        rqds      = str(row0[5] or '').split('\n') if len(row0) > 5 else []

        # Step 3 – merge
        runs = []
        for i, r in enumerate(run_rows):
            spt_raw = r['spt']
            spt = None if spt_raw in ('', 'Ref', '0') else to_num(spt_raw)
            runs.append({
                'method':        r['method'],
                'depth_to':      r['depth_to'],
                'mat_recovery':  to_num(mat_recs[i])  if i < len(mat_recs)  else None,
                'core_recovery': to_num(core_recs[i]) if i < len(core_recs) else None,
                'rqd':           to_num(rqds[i])      if i < len(rqds)      else None,
                'frac_freq':     to_num(r['frac'])    if r['frac']           else None,
                'spt_n':         spt,
            })

        return runs   # only process first matching table per page

    return []


# ─────────────────────────────────────────────────────────────────────────────
# 5. Lithology intervals
# ─────────────────────────────────────────────────────────────────────────────

# Rock/soil type endings that follow a colon
_LITH_TYPE = re.compile(
    r':\s*(?:[A-Z][A-Z /\-]*)?'
    r'(?:ALLUVIUM|METAPYROXENITE|PYROXENITE|BIOTITE\s+GNEISS[\w /]*'
    r'|GRANITE|GNEISS|ENGINEERED\s+FILL|FILL|ASPHALT|CONCRETE'
    r'|SAND|GRAVEL|CLAY|SILT|BASALT|QUARTZITE|SCHIST|DOLERITE'
    r'|SANDSTONE|MUDSTONE|SHALE|LIMESTONE)'
    r'[\w /]*\.?',
    re.IGNORECASE
)

# Tokens to remove from material descriptions
_NOISE = re.compile(
    r'\b(?:Scale|NWD\d?|SPT|HQ|BQ|PQ|UCS|Ref|NOTES?)\b'
    r'|\b\d{1,3}:\d{2}\b'                    # scale ratios "1:50"
    r'|-\d+\s+\d+(?=\s|$)'                   # depth-scale labels "-1 1"
    r'|\b\d+\.\d+\s+\d+\s+\d+\s+\d+\s+\d+\b',  # embedded run rows "3.00 100 100 30 8"
    re.IGNORECASE
)


def _clean(text):
    text = _NOISE.sub(' ', text)
    text = re.sub(r'^[\d\s\.\-/:()]+', '', text)
    return ' '.join(text.split()).strip()


_DRILL_LINE = re.compile(r'\b(?:NWD\d?|SPT|HQ|BQ|PQ)\b', re.IGNORECASE)
_NOTES_LINE = re.compile(
    r'(?:End of borehole|Drilling medium|Groundwater|Rock sample|seepage|rest level'
    r'|CONTRACTOR|MACHINE|DRILLED|PROFILED|TYPE SET|SETUP|X-COORD|Y-COORD'
    r'|REDUCED|LEVEL|Recovery|Frequency|INCLINATION|DIAM|DATE|TEXT)',
    re.IGNORECASE
)


def _collect_depth_markers(lines, end_depth):
    """
    Collect lithology depth boundaries, excluding drilling run lines and notes.
    Lithology markers appear alone or after depth-scale labels like "-1 1 0.35".
    Excluded: lines with NWD4/SPT keywords (run rows) and notes/footer lines.
    """
    candidates = {0.0, end_depth}
    for line in lines:
        if _DRILL_LINE.search(line) or _NOTES_LINE.search(line):
            continue
        for m in re.finditer(r'\b(\d+\.\d+)\b', line):
            val = float(m.group(1))
            if 0.0 < val < end_depth * 1.05:
                candidates.add(val)
    return sorted(candidates)


def parse_lithology(clean_text, end_depth):
    lines = clean_text.splitlines()
    depths = _collect_depth_markers(lines, end_depth)

    if len(depths) < 2:
        return []

    # For each depth boundary, record which line it first appears on
    # so we can slice the text correctly
    depth_line = {}  # depth → line index of first occurrence
    for depth in depths:
        pat = re.compile(r'(?<!\d)' + re.escape('{:.2f}'.format(depth)) + r'(?!\d)'
                         if depth != int(depth)
                         else r'(?<!\d)' + re.escape(str(depth)) + r'(?!\d)')
        for idx, line in enumerate(lines):
            if re.search(r'\b' + re.escape(str(depth)) + r'\b', line):
                depth_line[depth] = idx
                break
        else:
            depth_line[depth] = 0

    intervals = []
    for i in range(len(depths) - 1):
        d_top = depths[i]
        d_bot = depths[i + 1]

        start = depth_line.get(d_top, 0)
        end_  = depth_line.get(d_bot, len(lines))

        segment = ' '.join(l.strip() for l in lines[start:end_] if l.strip())

        # Strip the depth value itself from the start
        segment = re.sub(r'^\s*' + re.escape(str(d_top)) + r'\s*', '', segment)
        segment = re.sub(r'^Scale\s+\d+:\d+\s*', '', segment, flags=re.IGNORECASE)

        # Find last (most complete) lithology-type match
        best = None
        for m in _LITH_TYPE.finditer(segment):
            best = m

        if best:
            desc = _clean(segment[:best.end()])
        else:
            desc = _clean(segment)

        intervals.append({
            'depth_top':    d_top,
            'depth_bottom': d_bot,
            'material':     desc if len(desc) > 8 else '',
        })

    return intervals


# ─────────────────────────────────────────────────────────────────────────────
# 6. Per-page parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_page(page):
    full_text  = page.extract_text() or ''
    tables     = page.extract_tables()
    meta       = parse_metadata(full_text)

    if not meta.get('hole_id') or not meta.get('end_depth'):
        return None

    clean_text = strip_header(full_text)

    return {
        'hole_id':     meta['hole_id'],
        'lat':         meta.get('lat'),
        'lon':         meta.get('lon'),
        'water_level': meta.get('water_level', 0),
        'end_depth':   meta['end_depth'],
        'runs':        parse_drilling_runs(tables),
        'intervals':   parse_lithology(clean_text, meta['end_depth']),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. Build output rows
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_depth(v):
    return int(v) if v == int(v) else v


def build_interval_rows(bh):
    return [{
        'borehole_id':  bh['hole_id'],
        'y_position':   bh['lat'],
        'x_position':   bh['lon'],
        'depth_top':    _fmt_depth(iv['depth_top']),
        'depth_bottom': _fmt_depth(iv['depth_bottom']),
        'material':     iv['material'],
        'inclination':  0,
        'azimuth':      0,
        'water_level':  bh['water_level'],
    } for iv in bh['intervals']]


def build_run_rows(bh):
    rows = []
    prev = 0.0
    for run in bh['runs']:
        rows.append({
            'borehole_id':   bh['hole_id'],
            'y_position':    bh['lat'],
            'x_position':    bh['lon'],
            'depth_from':    prev,
            'depth_to':      run['depth_to'],
            'method':        run['method'],
            'mat_recovery':  run['mat_recovery'],
            'core_recovery': run['core_recovery'],
            'rqd':           run['rqd'],
            'frac_freq':     run['frac_freq'],
            'spt_n':         run['spt_n'],
            'water_level':   bh['water_level'],
        })
        prev = run['depth_to']
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 8. CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

INTERVAL_COLS = [
    'borehole_id', 'y_position', 'x_position',
    'depth_top', 'depth_bottom', 'material',
    'inclination', 'azimuth', 'water_level',
]
RUN_COLS = [
    'borehole_id', 'y_position', 'x_position',
    'depth_from', 'depth_to', 'method',
    'mat_recovery', 'core_recovery', 'rqd', 'frac_freq', 'spt_n',
    'water_level',
]


def write_csv(rows, fieldnames):
    if not rows:
        return ''
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fieldnames, extrasaction='ignore', lineterminator='\r\n')
    w.writeheader()
    for row in rows:
        w.writerow({k: ('' if row.get(k) is None else row[k]) for k in fieldnames})
    return out.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# 9. Main pipeline (importable)
# ─────────────────────────────────────────────────────────────────────────────

def parse_pdf(pdf_path):
    """Parse all pages. Returns (interval_rows, run_rows)."""
    import pdfplumber
    all_intervals, all_runs = [], []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            bh = parse_page(page)
            if not bh:
                continue
            all_intervals.extend(build_interval_rows(bh))
            all_runs.extend(build_run_rows(bh))
    return all_intervals, all_runs


# ─────────────────────────────────────────────────────────────────────────────
# 10. CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description='Parse borehole log PDF -> CSV')
    ap.add_argument('pdf')
    ap.add_argument('--out-dir', default='.')
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    interval_rows, run_rows = parse_pdf(args.pdf)

    iv_path  = out_dir / 'borehole_intervals.csv'
    run_path = out_dir / 'drilling_runs.csv'

    iv_path.write_text(write_csv(interval_rows, INTERVAL_COLS), encoding='utf-8')
    run_path.write_text(write_csv(run_rows, RUN_COLS), encoding='utf-8')

    print(f'Intervals : {len(interval_rows)} rows -> {iv_path}')
    print(f'Runs      : {len(run_rows)} rows -> {run_path}')

if __name__ == '__main__':
    main()