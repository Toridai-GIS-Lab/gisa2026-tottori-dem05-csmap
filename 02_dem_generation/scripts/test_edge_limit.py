#!/usr/bin/env python3
"""Check how filters.faceraster's max_triangle_edge_length treats an edge of exactly the limit.

A right triangle (0,0)-(30,0)-(0,40 m) has a hypotenuse of exactly 50 m; moving the
third vertex to 40.01 m makes it slightly longer than 50 m. Expected with PDAL 2.10:
exact50 -> rasterised, over50 -> empty, over50_unlimited -> rasterised.

usage: python test_edge_limit.py [--pdal PATH] [--workdir DIR]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from osgeo import gdal

gdal.UseExceptions()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pdal', default=os.environ.get('PDAL_BIN') or shutil.which('pdal') or 'pdal')
    ap.add_argument('--workdir', type=Path, default=Path('edge_limit_test'))
    a = ap.parse_args()
    r = a.workdir
    r.mkdir(parents=True, exist_ok=True)
    result = []
    # integer-centimetre coordinates, scaled back to metres as in tin_dem.py
    for name, y, limit in [('exact50', 40, 50), ('over50', 40.01, 50), ('over50_unlimited', 40.01, None)]:
        src = r / (name + '.csv')
        src.write_text('X,Y,Z\n0,0,100\n3000,0,100\n0,' + str(round(y * 100)) + ',100\n')
        fac = dict(type='filters.faceraster', resolution=.5, origin_x=0, origin_y=0, width=80, height=81)
        if limit is not None:
            fac['max_triangle_edge_length'] = limit
        pipe = [dict(type='readers.text', filename=str(src)), dict(type='filters.delaunay'),
                dict(type='filters.transformation', matrix='0.01 0 0 0 0 0.01 0 0 0 0 1 0 0 0 0 1'), fac,
                dict(type='writers.raster', filename=str(r / (name + '.tif')), data_type='float', nodata=-9999)]
        p = r / (name + '.json')
        p.write_text(json.dumps(dict(pipeline=pipe), indent=2))
        subprocess.run([a.pdal, 'pipeline', str(p)], check=True, capture_output=True)
        d = gdal.Open(str(r / (name + '.tif')))
        arr = d.ReadAsArray()
        result.append(dict(case=name, valid_cells=int((arr != -9999).sum())))
        d = None
    (r / 'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    ok = result[0]['valid_cells'] > 0 and result[1]['valid_cells'] == 0 and result[2]['valid_cells'] > 0
    print('PASS' if ok else 'UNEXPECTED: check your PDAL version')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
