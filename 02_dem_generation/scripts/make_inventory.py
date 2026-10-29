#!/usr/bin/env python3
"""Build inventory.json for tin_dem.py from a folder of ground-point COPC LAZ files.

Each input file becomes one tile. The tile extent (the output grid cell range) is
NOT the point-cloud bounding box: it is taken from the map-sheet code in the file
name (Japanese public-survey "chizu joho level 2500/5000" sheets, mode gsi2500) or
from a CSV that you supply (mode csv).

usage:
  python make_inventory.py INPUT_DIR -o work/inventory.json --epsg 6673
  python make_inventory.py INPUT_DIR -o work/inventory.json --epsg 6673 \
      --tile-mode csv --tiles-csv tiles.csv

tiles.csv columns: file,xmin,ymin,xmax,ymax  (file = file name inside INPUT_DIR,
X = easting, Y = northing).

Outputs inventory.json and inventory_errors.json (must be an empty list before
tin_dem.py will run).
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Japanese Plane Rectangular CS (JGD2011): system 1..19 -> EPSG 6669..6687
JGD2011_FIRST_EPSG = 6669
SHEET_RE = re.compile(r'^(\d{2})([A-T])([A-H])(\d)(\d)([1-4])?', re.IGNORECASE)


def gsi_sheet_bounds(name):
    """Return (system, code, (xmin, ymin, xmax, ymax)) for a level-5000/2500 sheet code.

    Level 50000 sheet: 30 km (N-S) x 40 km (E-W), rows A..T from X=+300 km,
    columns A..H from Y=-160 km. Level 5000: 10 x 10 subdivision (3 km x 4 km).
    Level 2500: 2 x 2 subdivision numbered 1=NW, 2=NE, 3=SW, 4=SE (1.5 km x 2 km).
    Returned coordinates are GIS order: x = easting, y = northing.
    """
    m = SHEET_RE.match(name)
    if not m:
        return None
    system = int(m.group(1))
    row = ord(m.group(2).upper()) - ord('A')
    col = ord(m.group(3).upper()) - ord('A')
    r5, c5 = int(m.group(4)), int(m.group(5))
    top = 300000 - row * 30000 - r5 * 3000
    left = -160000 + col * 40000 + c5 * 4000
    if m.group(6):
        q = int(m.group(6))
        left += 2000 if q in (2, 4) else 0
        top -= 1500 if q in (3, 4) else 0
        w, h = 2000, 1500
    else:
        w, h = 4000, 3000
    return system, m.group(0).upper(), (float(left), float(top - h), float(left + w), float(top))


def pdal_metadata(pdal, path):
    out = subprocess.run([pdal, 'info', '--metadata', str(path)], check=True,
                         capture_output=True, text=True).stdout
    return json.loads(out)['metadata']


def epsg_of(meta):
    srs = meta.get('srs') or {}
    try:
        from osgeo import osr
        osr.UseExceptions()
        wkt = srs.get('horizontal') or srs.get('wkt') or ''
        if wkt:
            ref = osr.SpatialReference()
            ref.ImportFromWkt(wkt)
            ref.AutoIdentifyEPSG()
            code = ref.GetAuthorityCode(None)
            return int(code) if code else None
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input_dir', type=Path)
    ap.add_argument('-o', '--output', type=Path, required=True, help='inventory.json to write')
    ap.add_argument('--epsg', type=int, required=True, help='expected horizontal EPSG of all inputs')
    ap.add_argument('--tile-mode', choices=['gsi2500', 'csv'], default='gsi2500')
    ap.add_argument('--tiles-csv', type=Path)
    ap.add_argument('--pattern', default='*.copc.laz', help='glob inside input_dir (not recursive)')
    ap.add_argument('--group', help='group name (default: input folder name)')
    ap.add_argument('--tolerance', type=float, default=0.01,
                    help='allowed overhang of point bounds beyond the tile extent [m]')
    ap.add_argument('--pdal', default=os.environ.get('PDAL_BIN') or shutil.which('pdal') or 'pdal')
    ap.add_argument('--workers', type=int, default=8)
    a = ap.parse_args()

    files = sorted(a.input_dir.resolve().glob(a.pattern))
    if not files:
        sys.exit(f'no files match {a.pattern} in {a.input_dir}')
    group = a.group or a.input_dir.resolve().name
    csv_bounds = {}
    if a.tile_mode == 'csv':
        if not a.tiles_csv:
            sys.exit('--tiles-csv is required with --tile-mode csv')
        with a.tiles_csv.open(newline='', encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                csv_bounds[r['file']] = tuple(float(r[k]) for k in ('xmin', 'ymin', 'xmax', 'ymax'))

    def one(p):
        err = []
        st = p.stat()
        meta = pdal_metadata(a.pdal, p)
        if not meta.get('copc'):
            err.append('not a COPC file (convert with: pdal translate in.laz out.copc.laz -w writers.copc)')
        epsg = epsg_of(meta)
        if epsg != a.epsg:
            err.append(f'EPSG {epsg} != expected {a.epsg}')
        stem = p.name[:-len('.copc.laz')] if p.name.endswith('.copc.laz') else p.stem
        if a.tile_mode == 'gsi2500':
            g = gsi_sheet_bounds(p.name)
            if g is None:
                return None, dict(path=str(p), errors=['file name does not start with a map-sheet code'])
            system, _, bounds = g
            if JGD2011_FIRST_EPSG + system - 1 != a.epsg:
                err.append(f'sheet system {system:02d} does not match EPSG {a.epsg}')
        else:
            if p.name not in csv_bounds:
                return None, dict(path=str(p), errors=['not listed in tiles csv'])
            bounds = csv_bounds[p.name]
        t = a.tolerance
        hb = (meta['minx'], meta['miny'], meta['maxx'], meta['maxy'])
        if not (hb[0] >= bounds[0] - t and hb[1] >= bounds[1] - t and hb[2] <= bounds[2] + t and hb[3] <= bounds[3] + t):
            err.append(f'point bounds {hb} exceed tile extent {bounds}')
        row = dict(group=group, tile=stem, path=str(p), bounds=list(bounds), epsg=epsg,
                   count=int(meta['count']), bytes=st.st_size, mtime_ns=st.st_mtime_ns,
                   header_bounds=list(hb))
        return row, (dict(path=str(p), errors=err) if err else None)

    rows, errors = [], []
    with ThreadPoolExecutor(a.workers) as pool:
        for row, e in pool.map(one, files):
            if row:
                rows.append(row)
            if e:
                errors.append(e)
    ids = [r['tile'] for r in rows]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        errors.append(dict(path=None, errors=[f'duplicate tile ids: {dup}']))
    ext = sorted({tuple(r['bounds']) for r in rows})
    if len(ext) != len(rows):
        errors.append(dict(path=None, errors=['two or more files share the same tile extent; merge them first']))

    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(rows, indent=1))
    err_path = a.output.with_name(a.output.stem + '_errors.json')
    err_path.write_text(json.dumps(errors, indent=1, ensure_ascii=False))
    print(f'{len(rows)} tiles written to {a.output}; {len(errors)} problems in {err_path}')
    for e in errors[:20]:
        print('  ', e)
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
