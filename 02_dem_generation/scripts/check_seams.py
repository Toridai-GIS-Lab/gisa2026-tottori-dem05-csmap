#!/usr/bin/env python3
"""Describe elevation jumps across tile boundaries of a mosaicked DEM.

For every pair of edge-sharing tiles (east and north neighbours with the same
extent along the boundary) the two cells facing each other across the boundary
are compared. Differences are between neighbouring cells, so steep real terrain
also produces large values: this is a screening tool, not an error estimate.

usage:
  python check_seams.py work/dem/<group>            # folder written by tin_dem.py

writes boundary_gradients.csv, boundary_summary.json and boundary_jump_locations.csv
into that folder.
"""
import argparse
import csv
import json
from pathlib import Path

import numpy as np
from osgeo import gdal

gdal.UseExceptions()
NODATA = -9999


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('group_dir', type=Path)
    ap.add_argument('--threshold', type=float, default=5.0, help='jump to report [m]')
    a = ap.parse_args()
    R = a.group_dir
    summary = json.loads((R / 'summary.json').read_text())
    rows = json.loads((R / 'tile_inventory.json').read_text())
    ds = gdal.Open(summary['dem'])
    gt = ds.GetGeoTransform()
    cell = gt[1]
    lookup = {(float(t['xmin']), float(t['ymin']), float(t['xmax']), float(t['ymax'])): t for t in rows}

    def col(x):
        return round((x - gt[0]) / cell)

    def row(y):
        return round((gt[3] - y) / cell)

    results, locations = [], []
    for t in rows:
        x0, y0, x1, y1 = (float(t[k]) for k in ('xmin', 'ymin', 'xmax', 'ymax'))
        w, h = x1 - x0, y1 - y0
        for direction, key in [('east', (x1, y0, x1 + w, y1)), ('north', (x0, y1, x1, y1 + h))]:
            other = lookup.get(key)
            if other is None:
                continue
            if direction == 'east':
                c = col(x1)
                arr = ds.ReadAsArray(c - 2, row(y1), 4, round(h / cell))  # rows x 4
                u, v = arr[:, 0:2], arr[:, 2:4]
                p, q = u[:, 1], v[:, 0]
                inner = np.r_[np.abs(u[:, 1] - u[:, 0])[(u != NODATA).all(axis=1)],
                              np.abs(v[:, 1] - v[:, 0])[(v != NODATA).all(axis=1)]]
            else:
                r = row(y1)
                arr = ds.ReadAsArray(col(x0), r - 2, round(w / cell), 4)  # 4 x cols
                v, u = arr[0:2], arr[2:4]   # v: north tile bottom rows, u: this tile top rows
                p, q = u[0], v[1]
                inner = np.r_[np.abs(u[1] - u[0])[(u != NODATA).all(axis=0)],
                              np.abs(v[1] - v[0])[(v != NODATA).all(axis=0)]]
            m = (p != NODATA) & (q != NODATA)
            d = np.abs(p[m] - q[m])
            results.append(dict(
                tile_a=t['tile_id'], tile_b=other['tile_id'], direction=direction, valid_pairs=int(m.sum()),
                jump_gt_threshold_pairs=int((d > a.threshold).sum()),
                max_jump_m=float(d.max()) if len(d) else None,
                p95_jump_m=float(np.percentile(d, 95)) if len(d) else None,
                p95_inner_gradient_m=float(np.percentile(inner, 95)) if len(inner) else None))
            big = m & (np.abs(p - q) > a.threshold)
            for i in np.flatnonzero(big):
                bx = x1 if direction == 'east' else x0 + (int(i) + .5) * cell
                by = y1 - (int(i) + .5) * cell if direction == 'east' else y1
                locations.append(dict(tile_a=t['tile_id'], tile_b=other['tile_id'], direction=direction,
                                      boundary_x=bx, boundary_y=by, z_a=float(p[i]), z_b=float(q[i]),
                                      jump_m=float(abs(p[i] - q[i]))))
    if not results:
        raise SystemExit('no edge-sharing tile pairs found')
    with (R / 'boundary_gradients.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)
    with (R / 'boundary_jump_locations.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['tile_a', 'tile_b', 'direction', 'boundary_x', 'boundary_y',
                                          'z_a', 'z_b', 'jump_m'])
        w.writeheader()
        w.writerows(sorted(locations, key=lambda v: -v['jump_m']))
    out = dict(threshold_m=a.threshold, boundaries=len(results),
               jump_pairs=sum(v['jump_gt_threshold_pairs'] for v in results),
               boundaries_with_jump=sum(v['jump_gt_threshold_pairs'] > 0 for v in results),
               top_boundaries=sorted([v for v in results if v['max_jump_m'] is not None],
                                     key=lambda v: v['max_jump_m'], reverse=True)[:20],
               note='Differences are between neighbouring cells, not same-coordinate errors '
                    'or ground-truth elevation errors.')
    (R / 'boundary_summary.json').write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != 'top_boundaries'}, indent=2))


if __name__ == '__main__':
    main()
