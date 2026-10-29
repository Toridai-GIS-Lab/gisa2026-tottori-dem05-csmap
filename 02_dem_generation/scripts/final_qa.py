#!/usr/bin/env python3
"""Validate the mosaicked DEM / hillshade of one tin_dem.py group and write records.

Checks: all tile checks PASS (mesh orientation, zero-area faces, area == hull,
edge limit), file hashes, grid (origin, cell, size from the tile inventory), CRS,
data types and NoData. Writes final_validation.json, products.csv and a small
preview PNG. Optionally adds external overviews to the hillshade.

usage:
  python final_qa.py work/dem/<group> [--overviews]
"""
import argparse
import csv
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from osgeo import gdal, osr

gdal.UseExceptions()


def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(8388608), b''):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('group_dir', type=Path)
    ap.add_argument('--overviews', action='store_true', help='build external overviews (2..1024) for the hillshade')
    a = ap.parse_args()
    R = a.group_dir
    v = json.loads((R / 'summary.json').read_text())
    assert v['status'].startswith('COMPLETE'), v['status']
    rows = json.loads((R / 'tile_inventory.json').read_text())
    checks = sorted((R / 'checks').glob('*.json'))
    assert len(checks) == len(rows) == v['tiles'], (len(checks), len(rows), v['tiles'])
    max_edge = v['max_triangle_edge_length']
    for p in checks:
        c = json.loads(p.read_text())
        assert c['status'] == 'PASS' and c['signature'] == v['signature'], p
        m = c['mesh']
        assert m['negative_faces'] == 0 and m['zero_area_faces'] == 0, p
        assert abs(m['triangle_area_m2'] - m['hull_area_m2']) < max(.001, m['hull_area_m2'] * 1e-9), p
        if max_edge > 0:
            assert m['max_triangle_edge_length_m'] == max_edge and m['xy_retained_max_edge_m'] <= max_edge, p
    for key in ['dem', 'hillshade']:
        assert sha(v[key]) == v[key + '_sha256'], key
        print('HASH PASS', key, flush=True)
    dem, hs = gdal.Open(v['dem']), gdal.Open(v['hillshade'])
    ref = osr.SpatialReference()
    ref.ImportFromEPSG(v['epsg'])
    ref.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    cell = v['cell_m']
    xmin = min(t['bounds'][0] for t in rows)
    ymax = max(t['bounds'][3] for t in rows)
    xmax = max(t['bounds'][2] for t in rows)
    ymin = min(t['bounds'][1] for t in rows)
    assert dem.GetGeoTransform() == hs.GetGeoTransform() == (xmin, cell, 0, ymax, 0, -cell)
    size = (round((xmax - xmin) / cell), round((ymax - ymin) / cell))
    assert (dem.RasterXSize, dem.RasterYSize) == (hs.RasterXSize, hs.RasterYSize) == size
    assert dem.GetSpatialRef().IsSame(ref) and hs.GetSpatialRef().IsSame(ref)
    assert dem.GetRasterBand(1).DataType == gdal.GDT_Float32 and dem.GetRasterBand(1).GetNoDataValue() == -9999
    assert hs.GetRasterBand(1).DataType == gdal.GDT_Byte and hs.GetRasterBand(1).GetNoDataValue() == 0
    w = min(2000, hs.RasterXSize)
    gdal.Translate(str(R / 'preview_hillshade.png'), hs, format='PNG', width=w, height=0)
    with (R / 'products.csv').open('w', encoding='utf-8-sig', newline='') as f:
        wr = csv.writer(f)
        wr.writerow(['group', 'type', 'path', 'cell_m', 'width', 'height', 'bytes', 'sha256'])
        for kind in ['dem', 'hillshade']:
            wr.writerow([v['group'], kind, v[kind], cell, size[0], size[1], Path(v[kind]).stat().st_size,
                         v[kind + '_sha256']])
    dem = hs = None
    if a.overviews:
        subprocess.run(['gdaladdo', '--config', 'GDAL_CACHEMAX', '2048', '--config', 'COMPRESS_OVERVIEW', 'DEFLATE',
                        '--config', 'BIGTIFF_OVERVIEW', 'YES', '-ro', '-r', 'average', v['hillshade'],
                        '2', '4', '8', '16', '32', '64', '128', '256', '512', '1024'], check=True)
        assert sha(v['hillshade']) == v['hillshade_sha256'], 'hillshade changed by overview creation'
    result = dict(status='PASS', group=v['group'], tiles=len(rows), cell_m=cell, buffer_m=v['buffer_m'],
                  max_triangle_edge_length_m=max_edge, epsg=v['epsg'], width=size[0], height=size[1],
                  valid_cells=v['valid_cells'], mesh_checks=len(checks),
                  mosaic_sample_windows=v['mosaic_sample_windows'], overviews=a.overviews,
                  available_gib=shutil.disk_usage(R).free / 1024 ** 3,
                  note='Structural and mesh checks only; no field-survey elevation accuracy guarantee. '
                       'Faces over the edge limit are not rasterized; sparse coverage may have more NoData.')
    (R / 'final_validation.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
