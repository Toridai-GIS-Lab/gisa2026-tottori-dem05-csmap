#!/usr/bin/env python3
"""Evaluate extracted ground points against reference ground points (e.g. UAV).

For every extracted point the nearest reference point in XY is searched. If it lies
within --radius, the vertical difference dz = z_extracted - z_reference is used.
Points without a reference within the radius (outside the UAV area or in gaps) are
not evaluated. Statistics: count, mean, median, RMSE, MAE and 95th percentile of
|dz|, plus the share of reference points that have an extracted point within the
radius (a coverage indicator).

usage:
  python evaluate_ground.py EXTRACTED.laz REFERENCE.laz [--radius 0.5] [-o result.json]

This replaces the original evaluation (03*_evaluate_*.json), which merged both
clouds before filters.nndistance; that mixes distances within the reference cloud
with the distances of interest and must not be read as an extraction error.
"""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree


def read_xyz(pdal, path):
    with tempfile.TemporaryDirectory() as tmp:
        ply = Path(tmp) / 'pts.ply'
        subprocess.run([pdal, 'translate', str(path), str(ply), '-w', 'writers.ply',
                        '--writers.ply.storage_mode=little endian', '--writers.ply.dims=X,Y,Z'],
                       check=True, capture_output=True)
        with ply.open('rb') as fp:
            n = 0
            while True:
                line = fp.readline().decode().strip()
                if line.startswith('element vertex'):
                    n = int(line.split()[-1])
                if line == 'end_header':
                    break
            return np.fromfile(fp, dtype='<f8', count=n * 3).reshape(n, 3)


def stats(dz):
    if dz.size == 0:
        return dict(n=0)
    ad = np.abs(dz)
    return dict(n=int(dz.size), mean_m=float(dz.mean()), median_m=float(np.median(dz)),
                rmse_m=float(np.sqrt(np.mean(dz ** 2))), mae_m=float(ad.mean()),
                p95_abs_m=float(np.percentile(ad, 95)), max_abs_m=float(ad.max()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('extracted', type=Path)
    ap.add_argument('reference', type=Path)
    ap.add_argument('--radius', type=float, default=0.5, help='max XY distance to the reference point [m]')
    ap.add_argument('-o', '--output', type=Path)
    ap.add_argument('--pdal', default=os.environ.get('PDAL_BIN') or shutil.which('pdal') or 'pdal')
    a = ap.parse_args()

    ext = read_xyz(a.pdal, a.extracted)
    ref = read_xyz(a.pdal, a.reference)
    tree = cKDTree(ref[:, :2])
    d, idx = tree.query(ext[:, :2], distance_upper_bound=a.radius)
    hit = np.isfinite(d)
    dz = ext[hit, 2] - ref[idx[hit], 2]
    d2, _ = cKDTree(ext[:, :2]).query(ref[:, :2], distance_upper_bound=a.radius)
    result = dict(extracted=str(a.extracted), reference=str(a.reference), radius_m=a.radius,
                  extracted_points=int(len(ext)), reference_points=int(len(ref)),
                  evaluated_points=int(hit.sum()),
                  reference_covered_ratio=float(np.isfinite(d2).mean()) if len(ref) else None,
                  dz=stats(dz),
                  definition='dz = z(extracted) - z(nearest reference point in XY within radius)')
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
