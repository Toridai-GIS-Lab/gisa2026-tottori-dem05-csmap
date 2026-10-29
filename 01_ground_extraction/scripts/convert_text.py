#!/usr/bin/env python3
"""Convert an XYZ text point file (e.g. UAV ground points) to COPC LAZ.

usage:
  python convert_text.py kouchi_ground.txt kouchi_ground.copc.laz --srs EPSG:6673
  python convert_text.py pts.csv pts.copc.laz --separator , --header "X,Y,Z" --skip 1

The coordinate system is assigned (not reprojected). Default input: space-separated
"X Y Z" without a header line, as in the original UAV files.
"""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src', type=Path)
    ap.add_argument('dst', type=Path)
    ap.add_argument('--srs', default='EPSG:6673')
    ap.add_argument('--separator', default=' ')
    ap.add_argument('--header', default='X Y Z', help='column names, joined by the separator')
    ap.add_argument('--skip', type=int, default=0, help='lines to skip at the top of the file')
    ap.add_argument('--scale', type=float, default=0.01, help='LAS coordinate scale [m]')
    ap.add_argument('--pdal', default=os.environ.get('PDAL_BIN') or shutil.which('pdal') or 'pdal')
    a = ap.parse_args()
    reader = {'type': 'readers.text', 'filename': str(a.src), 'separator': a.separator,
              'header': a.header, 'override_srs': a.srs}
    if a.skip:
        reader['skip'] = a.skip
    pipe = {'pipeline': [reader, {'type': 'writers.copc', 'filename': str(a.dst), 'forward': 'all',
                                  'scale_x': a.scale, 'scale_y': a.scale, 'scale_z': a.scale}]}
    a.dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(pipe, f)
    try:
        subprocess.run([a.pdal, 'pipeline', f.name], check=True)
    finally:
        os.unlink(f.name)
    print('written', a.dst)


if __name__ == '__main__':
    main()
