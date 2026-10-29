#!/usr/bin/env python3
"""Run the method comparison for all samples in a samples.json.

For every sample: convert the reference (UAV) text to COPC if needed, extract ground
points with each method, evaluate against the reference, and write a summary.

usage:
  python run_samples.py config/samples.json -o results/
  python run_samples.py config/samples.json -o results/ --methods smrf pmf --radius 0.5

Relative paths in samples.json are resolved from the folder that contains it.
Outputs: results/<sample_id>/extracted/*.copc.laz, results/<sample_id>/eval_<method>.json,
results/summary.csv and results/summary.md
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('samples', type=Path)
    ap.add_argument('-o', '--output-dir', type=Path, required=True)
    ap.add_argument('--methods', nargs='+')
    ap.add_argument('--radius', type=float, default=0.5)
    ap.add_argument('--params', type=Path, default=HERE.parent / 'config' / 'params.json')
    ap.add_argument('--pdal', default=os.environ.get('PDAL_BIN') or shutil.which('pdal') or 'pdal')
    ap.add_argument('--overwrite', action='store_true')
    a = ap.parse_args()

    cfg = json.loads(a.samples.read_text(encoding='utf-8'))
    base = a.samples.resolve().parent
    methods = a.methods or cfg['methods']
    srs = cfg.get('srs', 'EPSG:6673')
    py = sys.executable
    rows = []
    for s in cfg['samples']:
        sid = s['sample_id']
        out = a.output_dir / sid
        out.mkdir(parents=True, exist_ok=True)
        aerial = (base / s['aerial_lidar']).resolve()
        ref = (base / s['reference']).resolve()
        print(f'--- {sid} {s.get("name", "")}', flush=True)
        if not aerial.exists() or not ref.exists():
            print(f'  missing input: {aerial if not aerial.exists() else ref}', flush=True)
            continue
        if ref.suffix.lower() not in ('.las', '.laz'):
            ref_copc = out / f'{sid}_reference.copc.laz'
            if not ref_copc.exists() or a.overwrite:
                subprocess.run([py, str(HERE / 'convert_text.py'), str(ref), str(ref_copc), '--srs', srs,
                                '--pdal', a.pdal], check=True)
            ref = ref_copc
        for m in methods:
            cmd = [py, str(HERE / 'extract_ground.py'), str(aerial), '-o', str(out / 'extracted'),
                   '--method', m, '--params', str(a.params), '--pdal', a.pdal]
            if a.overwrite:
                cmd.append('--overwrite')
            if subprocess.run(cmd).returncode != 0:
                print(f'  {m}: extraction failed', flush=True)
                continue
            ext = next((out / 'extracted').glob(f'*_{m}.copc.laz'))
            ev = out / f'eval_{m}.json'
            subprocess.run([py, str(HERE / 'evaluate_ground.py'), str(ext), str(ref), '--radius', str(a.radius),
                            '-o', str(ev), '--pdal', a.pdal], check=True, stdout=subprocess.DEVNULL)
            r = json.loads(ev.read_text(encoding='utf-8'))
            rows.append(dict(sample_id=sid, name=s.get('name', ''), method=m,
                             extracted_points=r['extracted_points'], evaluated_points=r['evaluated_points'],
                             reference_covered_ratio=round(r['reference_covered_ratio'] or 0, 4),
                             mean_dz_m=round(r['dz'].get('mean_m', float('nan')), 4),
                             rmse_m=round(r['dz'].get('rmse_m', float('nan')), 4),
                             p95_abs_m=round(r['dz'].get('p95_abs_m', float('nan')), 4)))
            print(f"  {m}: points {r['extracted_points']:,}  RMSE {rows[-1]['rmse_m']} m", flush=True)
    if not rows:
        sys.exit('no results')
    with (a.output_dir / 'summary.csv').open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    keys = list(rows[0])
    md = ['| ' + ' | '.join(keys) + ' |', '|' + '---|' * len(keys)]
    md += ['| ' + ' | '.join(str(r[k]) for k in keys) + ' |' for r in rows]
    (a.output_dir / 'summary.md').write_text('\n'.join(md) + '\n', encoding='utf-8')
    print('\n'.join(md))


if __name__ == '__main__':
    main()
