#!/usr/bin/env python3
"""Create a CS relief map (RGBA GeoTIFF) from a DEM.

Adopted parameters (comparison image cs_s003_c006):
  slope:     Gaussian sigma 1.5 px (0.75 m at 0.5 m cells), radius 5 px
  curvature: Gaussian sigma 4.5 px (2.25 m), radius 14 px, negative Laplacian,
             coefficient 1.1
  RGB = (0.125H + 0.125C1 + 0.25S1 + 0.5C2) * S2/255; NoData -> transparent

NoData is never mixed in as zero elevation: the Gaussian is applied to values and
to the valid-weight separately and normalised. Each block is computed with a
16-pixel halo so that block boundaries do not show.

usage:
  python make_csmap.py DEM.tif OUT.tif
  python make_csmap.py DEM.tif OUT.tif --workers 8 --no-overviews

Note: sigma is given in PIXELS. With a cell size other than 0.5 m the same pixel
sigma means a different ground distance (--slope-sigma / --curv-sigma to change).
"""
import argparse
import collections
import concurrent.futures
import hashlib
import json
import math
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
from osgeo import gdal
from scipy.ndimage import gaussian_filter

gdal.UseExceptions()
HALO = 16


def ramp(x, a, b, first, last):
    f = np.clip((x - a) / (b - a), 0, 1).astype(np.float32)
    c0 = np.asarray(first, dtype=np.float32)
    c1 = np.asarray(last, dtype=np.float32)
    return np.rint(c0[None, None, :] + f[..., None] * (c1 - c0)).astype(np.float32)


def ramp3(x, a, b, low, mid, high):
    center = (a + b) / 2
    lower = ramp(x, a, center, low, mid)
    upper = ramp(x, center, b, mid, high)
    return np.where((x > center)[..., None], upper, lower)


def colorize(dem, slope, curvature, valid, k):
    h = ramp(dem, 0, 3000, (100, 100, 100), (255, 255, 255))                        # elevation: gray -> white
    c1 = ramp(curvature, -0.25 / k, 0.05 / k, (42, 92, 170), (255, 255, 255))       # curvature: blue -> white
    s1 = ramp(slope, 0, 60, (255, 255, 255), (189, 74, 29))                         # slope: white -> brown
    c2 = ramp3(curvature, -0.20 / k, 0.20 / k, (0, 0, 255), (255, 255, 240), (255, 0, 0))
    s2 = ramp(slope, 0, 90, (255, 255, 255), (0, 0, 0))                             # slope: white -> black
    rgb = 0.125 * h + 0.125 * c1 + 0.25 * s1 + 0.5 * c2
    rgb *= s2 / 255.0
    rgba = np.zeros((*dem.shape, 4), dtype=np.uint8)
    rgba[..., :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    rgba[..., :3][~valid] = 0
    rgba[..., 3] = np.where(valid, 255, 0).astype(np.uint8)
    return rgba


def read_boundless(ds, col, row, w, h):
    """Read band 1 as float64 with NaN outside the raster and at NoData."""
    out = np.full((h, w), np.nan)
    c0, r0 = max(col, 0), max(row, 0)
    c1, r1 = min(col + w, ds.RasterXSize), min(row + h, ds.RasterYSize)
    if c1 > c0 and r1 > r0:
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray(c0, r0, c1 - c0, r1 - r0).astype('float64')
        nd = band.GetNoDataValue()
        if nd is not None:
            arr[arr == nd] = np.nan
        out[r0 - row:r1 - row, c0 - col:c1 - col] = arr
    return out


def calculate(ds, col, row, w, h, p):
    z = read_boundless(ds, col - HALO, row - HALO, w + 2 * HALO, h + 2 * HALO)
    valid = np.isfinite(z)
    if not valid.any():
        return np.zeros((h, w, 4), dtype=np.uint8)

    def smooth(sigma):
        radius = int(np.ceil(3 * sigma))
        num = gaussian_filter(np.where(valid, z, 0), sigma, mode='constant', radius=radius)
        den = gaussian_filter(valid.astype(float), sigma, mode='constant', radius=radius)
        return np.divide(num, den, out=np.zeros_like(num), where=den > 1e-12)

    a = HALO
    b = a + h
    c = a + w
    gt = ds.GetGeoTransform()
    dx, dy = gt[1], -gt[5]
    s = smooth(p.slope_sigma)
    slope = np.degrees(np.arctan(np.hypot((s[a:b, a:c] - s[a:b, a + 1:c + 1]) / dx,
                                          (s[a:b, a:c] - s[a + 1:b + 1, a:c]) / dy))).astype('float32')
    s = smooth(p.curv_sigma)
    center = s[a:b, a:c]
    curv = ((2 * center - s[a:b, a - 1:c - 1] - s[a:b, a + 1:c + 1]) / dx ** 2
            + (2 * center - s[a - 1:b - 1, a:c] - s[a + 1:b + 1, a:c]) / dy ** 2).astype('float32')
    return colorize(np.nan_to_num(z[a:b, a:c]).astype('float32'), slope, curv, valid[a:b, a:c], p.coefficient)


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(8388608), b''):
            h.update(block)
    return h.hexdigest()


def self_check(ds, p):
    """Same pixels computed with different block splits must be identical."""
    W, H = ds.RasterXSize, ds.RasterYSize
    n = 0
    for fx, fy in [(.5, .5), (.25, .4), (.75, .6), (0, 0)]:
        col = min(int(W * fx), max(0, W - 512))
        row = min(int(H * fy), max(0, H - 512))
        w, h = min(512, W - col), min(512, H - row)
        whole = calculate(ds, col, row, w, h, p)
        pieces = np.zeros_like(whole)
        for yy in range(0, h, 256):
            for xx in range(0, w, 256):
                ww, hh = min(256, w - xx), min(256, h - yy)
                pieces[yy:yy + hh, xx:xx + ww] = calculate(ds, col + xx, row + yy, ww, hh, p)
        assert np.array_equal(whole, pieces), f'block split mismatch at {col},{row}'
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dem', type=Path)
    ap.add_argument('output', type=Path)
    ap.add_argument('--slope-sigma', type=float, default=1.5, help='[px]')
    ap.add_argument('--curv-sigma', type=float, default=4.5, help='[px]')
    ap.add_argument('--coefficient', type=float, default=1.1)
    ap.add_argument('--block', type=int, default=1024)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--min-free-gib', type=float, default=12.0)
    ap.add_argument('--no-overviews', action='store_true')
    p = ap.parse_args()

    start = time.monotonic()
    out = p.output.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.stem + '.partial.tif')
    lock = out.with_name(out.stem + '.lock')
    if out.exists() or partial.exists() or lock.exists():
        sys.exit(f'{out} (or its .partial/.lock) already exists; choose a new output')
    lock.write_text('running')
    try:
        src = gdal.Open(str(p.dem))
        gt = src.GetGeoTransform()
        assert gt[2] == 0 and gt[4] == 0, 'rotated rasters are not supported'
        dem_sha = sha(p.dem)
        n_check = self_check(src, p)
        print(f'SELF CHECK PASS ({n_check} windows)', flush=True)
        params = dict(source_dem=str(p.dem.resolve()), source_dem_sha256=dem_sha, output=str(out),
                      cell_m=gt[1], slope_sigma_px=p.slope_sigma, slope_sigma_m=p.slope_sigma * gt[1],
                      curvature_sigma_px=p.curv_sigma, curvature_sigma_m=p.curv_sigma * gt[1],
                      halo_px=HALO, curvature='negative Laplacian', coefficient=p.coefficient,
                      composition='(0.125H + 0.125C1 + 0.25S1 + 0.5C2) * S2/255',
                      workers=p.workers, block_px=p.block, script_sha256=sha(__file__))
        meta_path = out.with_suffix('.json')
        meta_path.write_text(json.dumps(params, indent=2))

        W, H = src.RasterXSize, src.RasterYSize
        drv = gdal.GetDriverByName('GTiff')
        dst = drv.Create(str(partial), W, H, 4, gdal.GDT_Byte,
                         options=['COMPRESS=DEFLATE', 'PREDICTOR=2', 'TILED=YES', 'BLOCKXSIZE=512',
                                  'BLOCKYSIZE=512', 'BIGTIFF=YES', 'PHOTOMETRIC=RGB', 'ALPHA=YES'])
        dst.SetGeoTransform(gt)
        dst.SetSpatialRef(src.GetSpatialRef())
        for i, ci in enumerate([gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand, gdal.GCI_AlphaBand], 1):
            dst.GetRasterBand(i).SetColorInterpretation(ci)
        dst.SetMetadata(dict(slope_sigma_pixels=str(p.slope_sigma), curvature_sigma_pixels=str(p.curv_sigma),
                             curvature_coefficient=str(p.coefficient), source_dem=p.dem.name))
        B = p.block
        windows = ((c, r, min(B, W - c), min(B, H - r)) for r in range(0, H, B) for c in range(0, W, B))
        total = math.ceil(W / B) * math.ceil(H / B)
        local = threading.local()

        def worker(win):
            if not hasattr(local, 'ds'):
                local.ds = gdal.Open(str(p.dem))
            return win, calculate(local.ds, *win, p)

        valid_pixels = dem_valid = done = 0
        last = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=p.workers) as pool:
            pending = collections.deque()
            for _ in range(p.workers * 2):
                win = next(windows, None)
                if win is not None:
                    pending.append(pool.submit(worker, win))
            while pending:
                (c, r, w, h), arr = pending.popleft().result()
                for i in range(4):
                    dst.GetRasterBand(i + 1).WriteArray(arr[:, :, i], c, r)
                valid_pixels += int(np.count_nonzero(arr[:, :, 3]))
                dem_valid += int(np.isfinite(read_boundless(src, c, r, w, h)).sum())
                done += 1
                win = next(windows, None)
                if win is not None:
                    pending.append(pool.submit(worker, win))
                if time.monotonic() - last >= 30 or done == total:
                    assert shutil.disk_usage(out.parent).free > p.min_free_gib * 1024 ** 3, 'low disk space'
                    print(f'CSMAP {done}/{total} {done / total * 100:.1f}%', flush=True)
                    last = time.monotonic()
        dst.FlushCache()
        dst = None
        assert valid_pixels == dem_valid, (valid_pixels, dem_valid)

        print('VERIFY OUTPUT', flush=True)
        chk = gdal.Open(str(partial))
        alpha = chk.GetRasterBand(4)
        bx, by = alpha.GetBlockSize()
        output_valid = 0
        for r in range(0, H, by):
            for c in range(0, W, bx):
                w, h = min(bx, W - c), min(by, H - r)
                a4 = chk.ReadAsArray(c, r, w, h)
                al = a4[3]
                assert np.all((al == 0) | (al == 255))
                assert np.all(a4[:3, al == 0] == 0)
                output_valid += int(np.count_nonzero(al))
        assert output_valid == dem_valid
        for fx, fy in [(.5, .5), (.3, .7), (.8, .2)]:
            c = (int(W * fx) // B) * B - 32
            r = (int(H * fy) // B) * B - 32
            c, r = max(0, min(c, W - 128)), max(0, min(r, H - 128))
            w, h = min(128, W - c), min(128, H - r)
            assert np.array_equal(chk.ReadAsArray(c, r, w, h), np.moveaxis(calculate(src, c, r, w, h, p), -1, 0))
        chk = None
        assert sha(p.dem) == dem_sha, 'source DEM changed during processing'
        partial.rename(out)
        params.update(seconds=round(time.monotonic() - start, 2), valid_pixels=valid_pixels,
                      output_bytes=out.stat().st_size, output_sha256=sha(out))
        meta_path.write_text(json.dumps(params, indent=2))
        if not p.no_overviews:
            print('CREATE EXTERNAL OVERVIEWS', flush=True)
            subprocess.run(['gdaladdo', '--config', 'GDAL_CACHEMAX', '2048', '--config', 'COMPRESS_OVERVIEW',
                            'DEFLATE', '--config', 'BIGTIFF_OVERVIEW', 'YES', '-ro', '-r', 'average', str(out),
                            '2', '4', '8', '16', '32', '64', '128', '256', '512', '1024'], check=True)
        print('FINISHED CSMAP', flush=True)
    finally:
        lock.unlink()


if __name__ == '__main__':
    main()
