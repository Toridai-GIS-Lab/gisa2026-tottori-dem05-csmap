#!/usr/bin/env python3
"""Extract ground points from airborne LiDAR LAS/LAZ with PDAL (SMRF / PMF / CSF).

Processing per file (same as the original batch scripts):
  readers.las -> Classification[:]=0 -> ReturnNumber[0:0]=1 -> NumberOfReturns[0:0]=1
  -> filters.smrf | filters.pmf | filters.csf -> keep Classification 2 -> write

usage:
  python extract_ground.py INPUT [INPUT ...] -o OUTPUT_DIR [--method smrf]
  python extract_ground.py /data/H31/laz -o /data/H31_smrf --method smrf --workers 4
  python extract_ground.py a.laz b.laz -o out --method pmf --format laz

INPUT may be files or folders (folders: *.las, *.laz directly inside, not recursive).
Parameters come from ../config/params.json (or --params). A manifest CSV with the
input, output, point counts and status of every file is written to OUTPUT_DIR.

Notes
- Classification is reset to 0 before filtering, so original classes (e.g. 7 =
  noise) are NOT preserved. The SMRF option ignore=Classification[7:7] therefore
  has no effect; it is kept to reproduce the original runs.
- Output is COPC by default (needed by the DEM step); --format laz writes LAZ.
- --ascii-temp copies each input to a temporary ASCII-named file first (work-around
  for PDAL builds on Windows that fail on non-ASCII paths).
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PARAMS = HERE.parent / 'config' / 'params.json'
FILTERS = {'smrf': 'filters.smrf', 'pmf': 'filters.pmf', 'csf': 'filters.csf'}


def build_pipeline(src, dst, method, params, fmt):
    stage = {'type': FILTERS[method], **params}
    if method == 'smrf':
        stage = {'type': 'filters.smrf', 'ignore': 'Classification[7:7]', **params}
    writer = ({'type': 'writers.copc', 'filename': str(dst), 'forward': 'all'} if fmt == 'copc'
              else {'type': 'writers.las', 'filename': str(dst), 'compression': 'laszip', 'forward': 'all'})
    return {'pipeline': [
        {'type': 'readers.las', 'filename': str(src)},
        {'type': 'filters.assign', 'assignment': 'Classification[:]=0'},
        {'type': 'filters.assign', 'assignment': 'ReturnNumber[0:0]=1'},
        {'type': 'filters.assign', 'assignment': 'NumberOfReturns[0:0]=1'},
        stage,
        {'type': 'filters.range', 'limits': 'Classification[2:2]'},
        writer]}


def point_count(pdal, path):
    out = subprocess.run([pdal, 'info', '--metadata', str(path)], check=True, capture_output=True,
                         text=True, encoding='utf-8', errors='replace').stdout
    return int(json.loads(out)['metadata']['count'])


def output_name(src, method, fmt):
    name = src.name
    for ext in ('.copc.laz', '.laz', '.las'):
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break
    if name.endswith('_org'):
        name = name[:-4]
    return f'{name}_{method}' + ('.copc.laz' if fmt == 'copc' else '.laz')


def collect(inputs):
    files = []
    for p in inputs:
        p = Path(p)
        if p.is_dir():
            files += sorted(q for q in p.iterdir() if q.suffix.lower() in ('.las', '.laz'))
        elif p.exists():
            files.append(p)
        else:
            sys.exit(f'not found: {p}')
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('inputs', nargs='+')
    ap.add_argument('-o', '--output-dir', type=Path, required=True)
    ap.add_argument('--method', choices=sorted(FILTERS), default='smrf')
    ap.add_argument('--params', type=Path, default=DEFAULT_PARAMS)
    ap.add_argument('--format', choices=['copc', 'laz'], default='copc')
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--overwrite', action='store_true', help='re-run even if a valid output exists')
    ap.add_argument('--ascii-temp', action='store_true')
    ap.add_argument('--pdal', default=os.environ.get('PDAL_BIN') or shutil.which('pdal') or 'pdal')
    a = ap.parse_args()

    params = json.loads(a.params.read_text(encoding='utf-8'))[a.method]
    files = collect(a.inputs)
    if not files:
        sys.exit('no LAS/LAZ input')
    a.output_dir.mkdir(parents=True, exist_ok=True)
    logs = a.output_dir / '_logs'
    logs.mkdir(exist_ok=True)
    print(f'{len(files)} files, method={a.method}, params={params}, output={a.output_dir}', flush=True)

    def run(item):
        i, src = item
        dst = a.output_dir / output_name(src, a.method, a.format)
        rec = dict(input=str(src), output=str(dst), method=a.method, status='', points_in='', points_out='',
                   seconds='')
        if dst.exists() and not a.overwrite:
            try:
                rec.update(status='SKIPPED_EXISTING', points_out=point_count(a.pdal, dst))
                return rec
            except Exception:
                rec['status'] = 'EXISTING_UNREADABLE'
                return rec
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='pdal_') as tmp:
            tmp = Path(tmp)
            work_src = src
            if a.ascii_temp:
                work_src = tmp / f'input_{i}{src.suffix}'
                shutil.copy2(src, work_src)
            work_dst = tmp / ('output' + ('.copc.laz' if a.format == 'copc' else '.laz'))
            pipe = build_pipeline(work_src, work_dst, a.method, params, a.format)
            pfile = logs / (dst.name + '.pipeline.json')
            pfile.write_text(json.dumps(build_pipeline(src, dst, a.method, params, a.format), indent=2,
                                        ensure_ascii=False), encoding='utf-8')
            run_file = tmp / 'pipeline.json'
            run_file.write_text(json.dumps(pipe, indent=2), encoding='utf-8')
            r = subprocess.run([a.pdal, 'pipeline', str(run_file)], capture_output=True, text=True,
                               encoding='utf-8', errors='replace')
            (logs / (dst.name + '.log')).write_text(r.stdout + r.stderr, encoding='utf-8')
            if r.returncode != 0 or not work_dst.exists():
                rec.update(status='ERROR', seconds=round(time.monotonic() - t0, 1))
                return rec
            part = dst.with_name(dst.name + '.part')
            shutil.move(str(work_dst), part)
            part.replace(dst)
        rec.update(status='OK', points_in=point_count(a.pdal, src), points_out=point_count(a.pdal, dst),
                   seconds=round(time.monotonic() - t0, 1))
        return rec

    records = []
    with ThreadPoolExecutor(a.workers) as pool:
        for n, rec in enumerate(pool.map(run, enumerate(files, 1)), 1):
            records.append(rec)
            print(f"[{n}/{len(files)}] {rec['status']:<16} {Path(rec['input']).name} -> "
                  f"{Path(rec['output']).name} {rec['points_out']}", flush=True)
    manifest = a.output_dir / f'manifest_{a.method}_{time.strftime("%Y%m%d_%H%M%S")}.csv'
    with manifest.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(records[0]))
        w.writeheader()
        w.writerows(records)
    bad = [r for r in records if r['status'] not in ('OK', 'SKIPPED_EXISTING')]
    print(f'done: {len(records) - len(bad)} ok, {len(bad)} failed; manifest {manifest}')
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
