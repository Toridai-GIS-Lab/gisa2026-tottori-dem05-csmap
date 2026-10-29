#!/usr/bin/env python3
"""
標高PNG（地理院形式）タイルの検証: z18 タイルを無作為抽出し、元DEMを独立に bilinear 補間した値と比較する。
期待値: max |diff| ≈ 0.005 m（0.01 m への丸め＋float32 誤差）。0.006 m 以下で終了コード 0

usage: python verify_dem_tiles.py SRC_DEM.tif tiles/dem_png [--tiles 150] [--seed 42]
"""
import argparse, os, random, sys
import numpy as np
from osgeo import gdal, osr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gsi_dem_tiles import decode_gsi, read_png, tile_bounds

gdal.UseExceptions()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("tiledir")
    ap.add_argument("--zoom", type=int, default=18)
    ap.add_argument("--tiles", type=int, default=150)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    src = gdal.Open(a.src); band = src.GetRasterBand(1); gt = src.GetGeoTransform()
    nd = band.GetNoDataValue()
    s3857 = osr.SpatialReference(); s3857.ImportFromEPSG(3857); s3857.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ss = src.GetSpatialRef().Clone(); ss.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(s3857, ss)

    random.seed(a.seed)
    zdir = os.path.join(a.tiledir, str(a.zoom)); xs = os.listdir(zdir)
    mx = 0.0; n = 0; done = 0; hmin, hmax = 1e9, -1e9
    while done < a.tiles:
        x = random.choice(xs); ys = os.listdir(os.path.join(zdir, x))
        if not ys: continue
        y = random.choice(ys)[:-4]; done += 1
        d = decode_gsi(read_png(os.path.join(zdir, x, f"{y}.png")))
        l, b, r, t = tile_bounds(a.zoom, int(x), int(y)); ps = (r - l) / 256
        jj, ii = np.meshgrid(np.arange(3, 256, 25), np.arange(3, 256, 25))
        X = l + (jj + .5) * ps; Y = t - (ii + .5) * ps
        pts = np.array(ct.TransformPoints(np.c_[X.ravel(), Y.ravel()]))
        c = (pts[:, 0] - gt[0]) / gt[1] - .5; rr = (pts[:, 1] - gt[3]) / gt[5] - .5
        c0 = np.floor(c).astype(int); r0 = np.floor(rr).astype(int)
        wx, wy = int(c0.min()), int(r0.min())
        ww, wh = int(c0.max() - c0.min() + 2), int(r0.max() - r0.min() + 2)
        win = np.full((wh, ww), np.nan)  # タイルがラスタ範囲外にはみ出す部分は NaN（比較対象外）
        x0, y0 = max(wx, 0), max(wy, 0)
        x1, y1 = min(wx + ww, src.RasterXSize), min(wy + wh, src.RasterYSize)
        if x1 > x0 and y1 > y0:
            part = band.ReadAsArray(x0, y0, x1 - x0, y1 - y0).astype(float)
            if nd is not None: part[part == nd] = np.nan
            win[y0 - wy:y1 - wy, x0 - wx:x1 - wx] = part
        R0 = r0 - r0.min(); C0 = c0 - c0.min(); fr = rr - r0; fc = c - c0
        v = (win[R0, C0] * (1 - fr) * (1 - fc) + win[R0, C0 + 1] * (1 - fr) * fc
             + win[R0 + 1, C0] * fr * (1 - fc) + win[R0 + 1, C0 + 1] * fr * fc)
        diff = d[ii, jj].ravel() - v; m = np.isfinite(diff)
        if m.any(): mx = max(mx, float(np.abs(diff[m]).max())); n += int(m.sum())
        f = d[np.isfinite(d)]
        if f.size: hmin = min(hmin, f.min()); hmax = max(hmax, f.max())
    print(f"tiles {done}  points {n}  max|diff| {mx:.4f} m  sampled h {hmin:.2f}..{hmax:.2f} m")
    sys.exit(0 if mx <= 0.006 else 1)


if __name__ == "__main__":
    main()
