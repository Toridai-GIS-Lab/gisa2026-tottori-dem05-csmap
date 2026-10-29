#!/usr/bin/env python3
"""
DEM (任意CRS, float) -> 地理院 標高PNGタイル (XYZ, EPSG:3857, 256px)
依存: GDAL Python バインディング (osgeo) と numpy のみ

方式（オーバービュー不要）:
  1) maxzoom のタイルのみソースDEMから生成（gdal.Warp, bilinear, float のまま）
  2) maxzoom-1 .. minzoom は、子タイル4枚をデコード -> 2x2 平均 (NoData除外) -> 再エンコード

エンコード: x = round(h/0.01); x<0 なら x += 2^24
            R=(x>>16)&255, G=(x>>8)&255, B=x&255, NoData=(128,0,0)
既存タイルはスキップ（中断後に同じコマンドで再開可）。全面NoDataのタイルは出力しない。

usage:
  python gsi_dem_tiles.py SRC.tif OUTDIR --minzoom 8 --maxzoom 18 --workers 10
  python gsi_dem_tiles.py SRC.tif OUTDIR --minzoom 14 --maxzoom 18 --bbox 134.20,35.52,134.24,35.55
"""
import argparse, math, os, time, warnings
from multiprocessing import Pool

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()
gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")   # .aux.xml を作らない
gdal.SetConfigOption("GTIFF_SRS_SOURCE", "EPSG")  # GeoTIFFキーとEPSG定義の差異警告を抑制（投影パラメータは同一）

TILE = 256
ORIGIN = 20037508.342789244
NODATA = -9999.0
_G = {}


# ---------- タイル計算 (WebMercatorQuad / XYZ) ----------
def tile_bounds(z, x, y):
    size = 2 * ORIGIN / 2 ** z
    left = -ORIGIN + x * size
    top = ORIGIN - y * size
    return left, top - size, left + size, top          # minx, miny, maxx, maxy


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lr = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lr)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_in_bbox(w, s, e, n, z):
    x0, y0 = lonlat_to_tile(w, n, z)
    x1, y1 = lonlat_to_tile(e, s, z)
    return [(z, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


# ---------- 地理院形式 ----------
def encode_gsi(h):
    """h: float (NaN=NoData) -> uint8 (3, H, W)"""
    invalid = ~np.isfinite(h)
    x = np.round(np.where(invalid, 0, h).astype(np.float64) * 100.0).astype(np.int64)
    x[x < 0] += 1 << 24
    rgb = np.stack([(x >> 16) & 255, (x >> 8) & 255, x & 255]).astype(np.uint8)
    rgb[0][invalid], rgb[1][invalid], rgb[2][invalid] = 128, 0, 0
    return rgb


def decode_gsi(rgb):
    """rgb: uint8 (3, H, W) -> float (NaN=NoData)"""
    r, g, b = (rgb[i].astype(np.int64) for i in range(3))
    x = (r << 16) + (g << 8) + b
    na = (r == 128) & (g == 0) & (b == 0)
    x = np.where(x >= (1 << 23), x - (1 << 24), x)
    h = x * 0.01
    h[na] = np.nan
    return h


def read_png(fn):
    ds = gdal.Open(fn)
    a = ds.ReadAsArray()[:3]
    ds = None
    return a


def tile_path(z, x, y):
    return os.path.join(_G["out"], str(z), str(x), f"{y}.png")


def save(fn, h):
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    rgb = encode_gsi(h)
    mem = gdal.GetDriverByName("MEM").Create("", TILE, TILE, 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(rgb[i])
    tmp = fn + ".tmp"
    gdal.GetDriverByName("PNG").CreateCopy(tmp, mem, options=["ZLEVEL=6"])
    mem = None
    os.replace(tmp, fn)


# ---------- worker ----------
def _init(path, outdir):
    _G["path"] = path
    _G["out"] = outdir
    gdal.SetCacheMax(512 * 1024 * 1024)


def work_base(t):
    """最大ズーム: ソースDEMから直接生成"""
    z, x, y = t
    fn = tile_path(z, x, y)
    if os.path.exists(fn):
        return (t, True)
    if "src" not in _G:
        _G["src"] = gdal.Open(_G["path"])
    src = _G["src"]
    snd = src.GetRasterBand(1).GetNoDataValue()
    ds = gdal.Warp("", src, format="MEM", dstSRS="EPSG:3857",
                   outputBounds=tile_bounds(z, x, y), width=TILE, height=TILE,
                   resampleAlg="bilinear", outputType=gdal.GDT_Float32,
                   srcNodata=snd if snd is not None else NODATA, dstNodata=NODATA)
    h = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    ds = None
    h[(h == NODATA) | ~np.isfinite(h)] = np.nan
    if np.isnan(h).all():
        return (t, False)
    save(fn, h)
    return (t, True)


def work_parent(t):
    """下位ズーム: 子タイル4枚を 2x2 平均"""
    z, x, y = t
    fn = tile_path(z, x, y)
    if os.path.exists(fn):
        return (t, True)
    big = np.full((TILE * 2, TILE * 2), np.nan)
    for dx in (0, 1):
        for dy in (0, 1):
            cf = tile_path(z + 1, 2 * x + dx, 2 * y + dy)
            if os.path.exists(cf):
                big[dy * TILE:(dy + 1) * TILE, dx * TILE:(dx + 1) * TILE] = decode_gsi(read_png(cf))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        h = np.nanmean(big.reshape(TILE, 2, TILE, 2), axis=(1, 3))
    if np.isnan(h).all():
        return (t, False)
    save(fn, h)
    return (t, True)


# ---------- main ----------
def run(pool, func, tiles, label):
    t0 = time.time()
    jobs = sorted(tiles, key=lambda t: (t[2], t[1]))  # 行方向に並べてキャッシュ効率を上げる
    keep = []
    for i, (t, ok) in enumerate(pool.imap_unordered(func, jobs, chunksize=64), 1):
        if ok:
            keep.append(t)
        if i % 20000 == 0:
            print(f"  {label}: {i}/{len(jobs)}  {time.time()-t0:.0f}s", flush=True)
    print(f"{label}: candidates={len(jobs)} written={len(keep)} {time.time()-t0:.1f}s", flush=True)
    return keep


def src_bounds_lonlat(path):
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    xs = [gt[0], gt[0] + gt[1] * ds.RasterXSize]
    ys = [gt[3], gt[3] + gt[5] * ds.RasterYSize]
    s_srs = ds.GetSpatialRef()
    t_srs = osr.SpatialReference(); t_srs.ImportFromEPSG(4326)
    t_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(s_srs, t_srs)
    return ct.TransformBounds(min(xs), min(ys), max(xs), max(ys), 21)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("outdir")
    ap.add_argument("--minzoom", type=int, default=8)
    ap.add_argument("--maxzoom", type=int, default=18)
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--bbox", help="lon_min,lat_min,lon_max,lat_max (範囲を限定)")
    a = ap.parse_args()

    w, s, e, n = src_bounds_lonlat(a.src)
    if a.bbox:
        bw, bs, be, bn = map(float, a.bbox.split(","))
        w, s, e, n = max(w, bw), max(s, bs), min(e, be), min(n, bn)
    print(f"GDAL {gdal.__version__}  bounds(4326): {w:.5f},{s:.5f},{e:.5f},{n:.5f}", flush=True)

    with Pool(a.workers, initializer=_init, initargs=(a.src, a.outdir)) as pool:
        keep = run(pool, work_base, tiles_in_bbox(w, s, e, n, a.maxzoom), f"z{a.maxzoom}")
        for z in range(a.maxzoom - 1, a.minzoom - 1, -1):
            parents = {(z, x // 2, y // 2) for (_, x, y) in keep}
            keep = run(pool, work_parent, parents, f"z{z}")


if __name__ == "__main__":
    main()
