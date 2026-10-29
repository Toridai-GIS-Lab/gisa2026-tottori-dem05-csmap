#!/usr/bin/env python3
"""Create buffered, integer-XY TIN DEMs and a hillshade from ground-point COPC tiles.

For every tile in inventory.json (see make_inventory.py):
  1. read the tile and up to 8 neighbours (readers.copc with bounds), crop to
     tile extent + BUFFER, keep Withheld == 0;
  2. shift XY to the tile origin and round to integer centimetres (work copy only),
     triangulate (filters.delaunay), shift back, rasterise with filters.faceraster
     (max_triangle_edge_length = MAX_EDGE);
  3. compute hillshade on the buffered DEM, cut both back to the tile extent
     without resampling;
  4. check the mesh (orientation, zero-area faces, area sum == convex hull area).
Then all tiles of a group are mosaicked into one DEM and one hillshade.

usage:
  python tin_dem.py --inventory work/inventory.json --output work/dem
  python tin_dem.py --inventory work/inventory.json --output work/dem_test --only 05NB331_grd

Results are reused only when inputs, parameters and this script are unchanged.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict

import numpy as np
from osgeo import gdal, osr
from scipy.spatial import ConvexHull

gdal.UseExceptions()
gdal.SetCacheMax(128 * 1024 ** 2)
SCRIPT = pathlib.Path(__file__).resolve()
NODATA = -9999


def save(p, v):
    p = pathlib.Path(p)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(v, indent=2, ensure_ascii=True, allow_nan=False))
    tmp.replace(p)


def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(8388608), b''):
            h.update(b)
    return h.hexdigest()


def srs_of(epsg):
    ref = osr.SpatialReference()
    ref.ImportFromEPSG(epsg)
    ref.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return ref


def read_ply_header(path):
    with open(path, 'rb') as fp:
        header = []
        while True:
            line = fp.readline().decode().strip()
            header.append(line)
            if line == 'end_header':
                break
        return header, fp.tell()


class Runner:
    def __init__(self, a):
        self.a = a
        self.cell = a.cell
        self.buffer = a.buffer
        self.max_edge = a.max_edge
        self.scale = a.xy_scale
        self.memory_limit = int(a.memory_limit_gib * 1024 ** 3)
        self.memory_used = 0
        self.cond = threading.Condition()

    def validate(self, p, b, epsg):
        cell = self.cell
        ds = gdal.Open(str(p))
        w = round((b[2] - b[0]) / cell)
        h = round((b[3] - b[1]) / cell)
        assert ds.GetGeoTransform() == (b[0], cell, 0, b[3], 0, -cell)
        assert (ds.RasterXSize, ds.RasterYSize, ds.RasterCount) == (w, h, 1)
        band = ds.GetRasterBand(1)
        assert band.DataType == gdal.GDT_Float32 and band.GetNoDataValue() == NODATA
        assert ds.GetSpatialRef().IsSame(srs_of(epsg))
        valid = 0
        lo = hi = None
        for y in range(0, h, 128):
            arr = ds.ReadAsArray(0, y, w, min(128, h - y))
            assert np.isfinite(arr).all()
            m = arr != NODATA
            valid += int(m.sum())
            if m.any():
                mn, mx = float(arr[m].min()), float(arr[m].max())
                lo = mn if lo is None else min(lo, mn)
                hi = mx if hi is None else max(hi, mx)
        return dict(valid_cells=valid, nodata_cells=w * h - valid, zmin=lo, zmax=hi)

    def memory_job(self, fn, t, rows):
        b = t['bounds']
        bf = self.buffer
        neighbors = [v for v in rows if v['bounds'][2] > b[0] - bf and v['bounds'][0] < b[2] + bf
                     and v['bounds'][3] > b[1] - bf and v['bounds'][1] < b[3] + bf]
        estimate = int(sum(v['count'] for v in neighbors) * 48 + t['count'] * 200 + 1024 ** 3)
        estimate = min(estimate, self.memory_limit)
        with self.cond:
            self.cond.wait_for(lambda: self.memory_used + estimate <= self.memory_limit)
            self.memory_used += estimate
        try:
            return fn(t)
        finally:
            with self.cond:
                self.memory_used -= estimate
                self.cond.notify_all()

    def group(self, name, records):
        a = self.a
        cell, buffer_m, max_edge, scale = self.cell, self.buffer, self.max_edge, self.scale
        start = time.monotonic()
        R = a.output / name
        R.mkdir(parents=True, exist_ok=True)
        for n in ('DEM_tiles', 'hillshade_tiles', 'jobs', 'checks', 'DEM', 'DEM_hillshade'):
            (R / n).mkdir(exist_ok=True)
        rows = []
        for v in records:
            t = dict(v)
            t.update(tile_id=v['tile'], **dict(zip(['xmin', 'ymin', 'xmax', 'ymax'], v['bounds'])))
            rows.append(t)
        epsgs = {v['epsg'] for v in rows}
        assert len(epsgs) == 1, f'mixed EPSG in group {name}: {epsgs}'
        epsg = epsgs.pop()
        for t in rows:
            st = pathlib.Path(t['path']).stat()
            assert (st.st_size, st.st_mtime_ns) == (t['bytes'], t['mtime_ns']), \
                f"input changed since inventory: {t['path']}"
        print(f'HASH INPUTS {name} ({len(rows)} files)', flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            hashes = list(pool.map(lambda v: sha(v['path']), rows))
        inputs = [dict(path=t['path'], sha256=h, bytes=t['bytes'], mtime_ns=t['mtime_ns'])
                  for t, h in zip(rows, hashes)]
        signature = hashlib.sha256(json.dumps(dict(
            inputs=inputs, runner=sha(SCRIPT), cell=cell, buffer=buffer_m,
            max_triangle_edge_length=max_edge, xy_scale=scale), sort_keys=True).encode()).hexdigest()
        save(R / 'configuration.json', dict(
            signature=signature, source_group=name, inputs=inputs, cell_m=cell, buffer_m=buffer_m,
            epsg=epsg, workers=a.workers, reader_requests=8, memory_limit_gib=a.memory_limit_gib,
            reader_range_query=True, max_triangle_edge_length=max_edge, xy_scale=scale,
            pdal=a.pdal, script_sha256=sha(SCRIPT)))
        save(R / 'tile_inventory.json', rows)
        dem = R / 'DEM' / (name + f'_TIN_{str(cell).replace(".", "p")}m.tif')
        shade = R / 'DEM_hillshade' / (name + '_TIN_hillshade.tif')
        complete = R / 'summary.json'
        only = set(a.only or [])
        if complete.exists() and not only:
            prev = json.loads(complete.read_text())
            if prev.get('signature') == signature and dem.exists() and shade.exists() \
                    and prev.get('dem_sha256') == sha(dem) and prev.get('hillshade_sha256') == sha(shade):
                print('REUSED GROUP ' + name, flush=True)
                return prev

        def job(t):
            tile = t['tile_id']
            b = tuple(float(t[k]) for k in ('xmin', 'ymin', 'xmax', 'ymax'))
            bb = (b[0] - buffer_m, b[1] - buffer_m, b[2] + buffer_m, b[3] + buffer_m)
            width = round((b[2] - b[0]) / cell)
            height = round((b[3] - b[1]) / cell)
            pad = round(buffer_m / cell)
            dest = R / 'DEM_tiles' / (tile + '_TIN.tif')
            tshade = R / 'hillshade_tiles' / (tile + '_hillshade.tif')
            check = R / 'checks' / (tile + '.json')
            if check.exists() and dest.exists() and tshade.exists():
                old = json.loads(check.read_text())
                if old['signature'] == signature and old['dem_sha256'] == sha(dest) \
                        and old['hillshade_sha256'] == sha(tshade):
                    return dict(old, reused=True)
            if dest.exists() or tshade.exists():
                raise RuntimeError('Incomplete output needs inspection: ' + tile)
            d = R / 'jobs' / tile
            d.mkdir(exist_ok=True)
            raw = d / 'buffered.tif'
            rawshade = d / 'buffered_hillshade.tif'
            if shutil.disk_usage(R).free < a.min_free_gib * 1024 ** 3:
                raise RuntimeError(f'Less than {a.min_free_gib} GiB free disk space')
            neighbors = sorted([v for v in rows if float(v['xmax']) > bb[0] and float(v['xmin']) < bb[2]
                                and float(v['ymax']) > bb[1] and float(v['ymin']) < bb[3]],
                               key=lambda v: v['tile_id'])
            assert 1 <= len(neighbors) <= 9, (tile, len(neighbors))
            bounds = f'([{bb[0]},{bb[2]}],[{bb[1]},{bb[3]}])'
            source = d / 'cropped_points.ply'
            integer = d / 'integer_points.ply'
            mesh = d / 'integer_mesh.ply'
            # 1. points of this tile + neighbours inside the buffered extent
            readers = [dict(type='readers.copc', filename=v['path'], tag=f'r{i}', requests=8, bounds=bounds)
                       for i, v in enumerate(neighbors)]
            extraction = readers + [
                dict(type='filters.merge', inputs=[f'r{i}' for i in range(len(neighbors))]),
                dict(type='filters.crop', bounds=bounds),
                dict(type='filters.expression', expression='Withheld == 0'),
                dict(type='writers.ply', filename=str(source), storage_mode='little endian', dims=['X', 'Y', 'Z'])]
            save(d / 'extract.json', dict(pipeline=extraction))
            ts = time.monotonic()
            with (d / 'extract.log').open('w') as log:
                subprocess.run([a.pdal, 'pipeline', str(d / 'extract.json'), '--dims', 'X,Y,Z,Withheld'],
                               stdout=log, stderr=subprocess.STDOUT, check=True)
            # 2. integer XY (work copy only)
            header, offset = read_ply_header(source)
            n = int(next(v for v in header if v.startswith('element vertex')).split()[-1])
            assert n >= 3, (tile, 'fewer than 3 points')
            xyz = np.memmap(source, mode='r', dtype='<f8', offset=offset, shape=(n, 3))
            xy = np.empty((n, 2))
            rounding = 0.0
            with integer.open('wb') as fp:
                fp.write(f'ply\nformat binary_little_endian 1.0\nelement vertex {n}\nproperty double x\n'
                         f'property double y\nproperty double z\nend_header\n'.encode())
                for st in range(0, n, 200000):
                    arr = np.array(xyz[st:st + 200000])
                    arr[:, :2] = np.rint((arr[:, :2] - [b[0], b[1]]) * scale)
                    rounding = max(rounding, float(np.max(np.abs(
                        arr[:, :2] / scale + [b[0], b[1]] - xyz[st:st + 200000, :2]))))
                    xy[st:st + len(arr)] = arr[:, :2]
                    fp.write(arr.astype('<f8').tobytes())
            assert rounding < 1e-8, (tile, f'XY is not on a 1/{scale} m grid (max rounding {rounding}); '
                                           'check the recording unit or change --xy-scale')
            del xyz
            hull_area = ConvexHull(xy).volume / scale ** 2
            del xy
            inv = 1.0 / scale
            fac = dict(type='filters.faceraster', resolution=cell, origin_x=bb[0], origin_y=bb[1],
                       width=width + 2 * pad, height=height + 2 * pad)
            if max_edge > 0:
                fac['max_triangle_edge_length'] = max_edge
            pipeline = [
                dict(type='readers.ply', filename=str(integer), override_srs=f'EPSG:{epsg}'),
                dict(type='filters.delaunay'),
                dict(type='writers.ply', filename=str(mesh), storage_mode='little endian',
                     dims=['X', 'Y', 'Z'], faces=True),
                dict(type='filters.transformation',
                     matrix=f'{inv} 0 0 {b[0]} 0 {inv} 0 {b[1]} 0 0 1 0 0 0 0 1'),
                fac,
                dict(type='writers.raster', filename=str(raw), data_type='float', nodata=NODATA,
                     gdalopts=['TILED=YES', 'COMPRESS=DEFLATE', 'PREDICTOR=3'])]
            save(d / 'pipeline.json', dict(pipeline=pipeline))
            with (d / 'pdal.log').open('w') as log:
                subprocess.run([a.pdal, 'pipeline', str(d / 'pipeline.json')],
                               stdout=log, stderr=subprocess.STDOUT, check=True)
            # 3. mesh checks (on the full TIN before the edge limit)
            header, offset = read_ply_header(mesh)
            nv = int(next(v for v in header if v.startswith('element vertex')).split()[-1])
            nf = int(next(v for v in header if v.startswith('element face')).split()[-1])
            assert nv == n
            vertices = np.memmap(mesh, mode='r', dtype='<f8', offset=offset, shape=(n, 3))
            faces = np.memmap(mesh, mode='r', dtype=[('n', 'u1'), ('i', '<u4', (3,))],
                              offset=offset + n * 24, shape=(nf,))
            area = 0.0
            neg = zero = 0
            long_faces = 0
            long_area = 0.0
            kept_max = 0.0
            lim = (max_edge * scale) ** 2 if max_edge > 0 else np.inf
            for st in range(0, nf, 200000):
                part = faces[st:st + 200000]
                assert np.all(part['n'] == 3)
                pts = vertices[part['i'], :2]
                u = pts[:, 1] - pts[:, 0]
                v = pts[:, 2] - pts[:, 0]
                det = u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]
                area += float(np.abs(det).sum()) / 2 / scale ** 2
                neg += int((det < 0).sum())
                zero += int((det == 0).sum())
                edge2 = np.maximum.reduce([np.sum((pts[:, 1] - pts[:, 0]) ** 2, axis=1),
                                           np.sum((pts[:, 2] - pts[:, 1]) ** 2, axis=1),
                                           np.sum((pts[:, 0] - pts[:, 2]) ** 2, axis=1)])
                long = edge2 > lim
                long_faces += int(long.sum())
                long_area += float(np.abs(det[long]).sum()) / 2 / scale ** 2
                if (~long).any():
                    kept_max = max(kept_max, float(np.sqrt(edge2[~long].max())) / scale)
            del vertices, faces
            assert abs(area - hull_area) < max(.001, hull_area * 1e-9), (tile, area, hull_area)
            assert neg == 0 and zero == 0, (tile, neg, zero)
            mesh_check = dict(points=n, faces=nf, triangle_area_m2=area, hull_area_m2=hull_area,
                              negative_faces=neg, zero_area_faces=zero, xy_roundtrip_max_m=rounding,
                              neighbors=[v['tile_id'] for v in neighbors],
                              max_triangle_edge_length_m=max_edge, xy_faces_over_limit=long_faces,
                              xy_area_over_limit_m2=long_area, xy_retained_max_edge_m=kept_max)
            save(d / 'mesh_check.json', mesh_check)
            # 4. hillshade on the buffered surface, then crop both without resampling
            x = gdal.DEMProcessing(str(rawshade), str(raw), 'hillshade', azimuth=a.azimuth,
                                   altitude=a.altitude, zFactor=1, scale=1,
                                   creationOptions=['TILED=YES', 'COMPRESS=DEFLATE'])
            x = None
            for out, src, opts in [(dest, raw, ['PREDICTOR=3']), (tshade, rawshade, [])]:
                x = gdal.Translate(str(out), str(src), srcWin=[pad, pad, width, height],
                                   creationOptions=['TILED=YES', 'COMPRESS=DEFLATE'] + opts)
                x = None
            stats = self.validate(dest, b, epsg)
            ss = gdal.Open(str(tshade))
            assert ss.GetGeoTransform() == (b[0], cell, 0, b[3], 0, -cell)
            assert ss.GetRasterBand(1).DataType == gdal.GDT_Byte
            ss = None
            result = dict(tile=tile, status='PASS', signature=signature, dem_sha256=sha(dest),
                          hillshade_sha256=sha(tshade), elapsed_seconds=round(time.monotonic() - ts, 2),
                          reused=False, mesh=mesh_check, **stats)
            save(check, result)
            for f in (raw, rawshade, source, integer, mesh):
                f.unlink()
            return result

        targets = [t for t in rows if not only or t['tile_id'] in only]
        if only and len(targets) != len(only):
            raise SystemExit(f'unknown tile ids: {sorted(only - {t["tile_id"] for t in rows})}')
        results, errors = [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
            future = {pool.submit(self.memory_job, job, t, rows): t for t in targets}
            for f in concurrent.futures.as_completed(future):
                tile = future[f]['tile_id']
                try:
                    results.append(f.result())
                    print(f'TILE {name} {len(results)}/{len(targets)} {tile} PASS', flush=True)
                except Exception as exc:
                    errors.append(dict(tile=tile, error=str(exc), traceback=traceback.format_exc()))
                    print(f'TILE FAIL {name} {tile} {exc}', flush=True)
                save(R / 'progress.json', dict(stage='TILES', completed=len(results), failed=len(errors),
                                               total=len(targets)))
        if errors:
            save(R / 'failures.json', errors)
            raise RuntimeError(f'{name}: {len(errors)} failed tiles')
        if only:
            print('--only given: tile rasters kept in DEM_tiles/ and hillshade_tiles/, no mosaic', flush=True)
            return dict(status='TILES_ONLY', group=name, tiles=len(results))

        # 5. mosaic
        tile_bytes = sum((R / 'DEM_tiles' / (t['tile_id'] + '_TIN.tif')).stat().st_size
                         + (R / 'hillshade_tiles' / (t['tile_id'] + '_hillshade.tif')).stat().st_size
                         for t in rows)
        assert shutil.disk_usage(R).free > tile_bytes * 1.35 + a.min_free_gib * 1024 ** 3, \
            'Not enough free disk space for verified final mosaic'
        save(R / 'progress.json', dict(stage='MERGE', completed=len(rows)))
        print('MERGE ' + name, flush=True)
        files = [str(R / 'DEM_tiles' / (t['tile_id'] + '_TIN.tif')) for t in rows]
        vrt = R / 'dem_tiles.vrt'
        ds = gdal.BuildVRT(str(vrt), files)
        ds = None
        ds = gdal.Translate(str(dem), str(vrt), creationOptions=['TILED=YES', 'COMPRESS=DEFLATE',
                                                                 'PREDICTOR=3', 'BIGTIFF=YES'])
        ds = None
        gdal.SetCacheMax(2048 * 1024 ** 2)
        save(R / 'progress.json', dict(stage='HILLSHADE', completed=len(rows)))
        print('HILLSHADE ' + name, flush=True)
        ds = gdal.DEMProcessing(str(shade), str(dem), 'hillshade', azimuth=a.azimuth, altitude=a.altitude,
                                zFactor=1, scale=1,
                                creationOptions=['TILED=YES', 'COMPRESS=DEFLATE', 'BIGTIFF=YES'])
        ds = None
        whole = gdal.Open(str(dem))
        hs = gdal.Open(str(shade))
        gt = whole.GetGeoTransform()
        assert gt[1] == cell and gt[5] == -cell and hs.GetGeoTransform() == gt
        assert (hs.RasterXSize, hs.RasterYSize) == (whole.RasterXSize, whole.RasterYSize)
        assert whole.GetSpatialRef().IsSame(srs_of(epsg))
        samples = 0
        for t in rows:
            src = gdal.Open(str(R / 'DEM_tiles' / (t['tile_id'] + '_TIN.tif')))
            w, h = src.RasterXSize, src.RasterYSize
            col = round((float(t['xmin']) - gt[0]) / cell)
            row = round((gt[3] - float(t['ymax'])) / cell)
            for x, y in [(0, 0), (max(0, w // 2 - 50), max(0, h // 2 - 50)), (max(0, w - 100), max(0, h - 100))]:
                ww, hh = min(100, w - x), min(100, h - y)
                assert np.array_equal(src.ReadAsArray(x, y, ww, hh), whole.ReadAsArray(col + x, row + y, ww, hh))
                samples += 1
            src = None
        width, height = whole.RasterXSize, whole.RasterYSize
        whole = hs = None
        for v in inputs:
            assert sha(v['path']) == v['sha256'], f"input changed during processing: {v['path']}"
        if not a.keep_tiles:
            for folder in ['DEM_tiles', 'hillshade_tiles']:
                for f in (R / folder).glob('*.tif'):
                    f.unlink()
                (R / folder).rmdir()
        vrt.unlink()
        result = dict(status='COMPLETE_WITH_COVERAGE_CAVEATS', signature=signature, group=name, tiles=len(rows),
                      inputs_unchanged=True, cell_m=cell, buffer_m=buffer_m, width=width, height=height,
                      epsg=epsg, max_triangle_edge_length=max_edge, mosaic_sample_windows=samples,
                      valid_cells=sum(v['valid_cells'] for v in results), dem=str(dem), hillshade=str(shade),
                      dem_sha256=sha(dem), hillshade_sha256=sha(shade),
                      elapsed_seconds=round(time.monotonic() - start, 2),
                      notes=f'Triangles with an edge exceeding {max_edge} m are excluded from rasterization. '
                            'NoData may increase in sparsely supported regions; no elevation smoothing or source edits.')
        save(complete, result)
        save(R / 'progress.json', dict(stage='COMPLETE', status=result['status'], completed=len(rows)))
        return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--inventory', type=pathlib.Path, required=True)
    ap.add_argument('--output', type=pathlib.Path, required=True, help='output folder (created)')
    ap.add_argument('--pdal', default=os.environ.get('PDAL_BIN') or shutil.which('pdal') or 'pdal')
    ap.add_argument('--cell', type=float, default=0.5, help='cell size [m]')
    ap.add_argument('--buffer', type=float, default=5.0, help='neighbour read width around a tile [m]')
    ap.add_argument('--max-edge', type=float, default=50.0,
                    help='max_triangle_edge_length for faceraster [m]; 0 = unlimited')
    ap.add_argument('--xy-scale', type=float, default=100.0,
                    help='XY recording resolution as 1/m (100 = 1 cm); used for integer triangulation')
    ap.add_argument('--azimuth', type=float, default=315.0)
    ap.add_argument('--altitude', type=float, default=45.0)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--memory-limit-gib', type=float, default=16.0,
                    help='upper bound of the estimated memory of concurrently running tiles')
    ap.add_argument('--min-free-gib', type=float, default=12.0)
    ap.add_argument('--only', nargs='+', metavar='TILE', help='process only these tile ids (no mosaic)')
    ap.add_argument('--keep-tiles', action='store_true', help='keep per-tile rasters after the mosaic')
    a = ap.parse_args()
    a.output = a.output.resolve()
    if shutil.which(a.pdal) is None and not pathlib.Path(a.pdal).exists():
        sys.exit(f'PDAL not found: {a.pdal} (use --pdal or PDAL_BIN)')

    errors_file = a.inventory.with_name(a.inventory.stem + '_errors.json')
    if errors_file.exists() and json.loads(errors_file.read_text()):
        sys.exit(f'{errors_file} is not empty; fix the inventory first')
    a.output.mkdir(parents=True, exist_ok=True)
    lock = a.output / 'RUNNING.lock'
    with lock.open('x') as f:
        f.write(str(os.getpid()))
    try:
        groups = defaultdict(list)
        records = json.loads(a.inventory.read_text())
        for v in records:
            groups[v['group']].append(v)
        save(a.output / 'inventory.json', records)
        runner = Runner(a)
        results, errors = [], []
        for name, recs in sorted(groups.items()):
            print(f'GROUP START {name} {len(recs)}', flush=True)
            try:
                results.append(runner.group(name, recs))
                print('GROUP COMPLETE ' + name, flush=True)
            except Exception as exc:
                errors.append(dict(group=name, error=str(exc), traceback=traceback.format_exc()))
                print('GROUP FAIL ' + name + ' ' + str(exc), flush=True)
        save(a.output / 'summary.json', dict(
            status='COMPLETE_WITH_COVERAGE_CAVEATS' if not errors else 'PARTIAL_FAILURE',
            completed_groups=len(results), failed_groups=len(errors), results=results, errors=errors))
        print(f'FINISHED {len(results)} groups, {len(errors)} errors', flush=True)
    finally:
        lock.unlink()
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
