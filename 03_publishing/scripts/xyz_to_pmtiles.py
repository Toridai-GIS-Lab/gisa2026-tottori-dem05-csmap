#!/usr/bin/env python3
"""
XYZ タイルディレクトリ ({z}/{x}/{y}.png) -> PMTiles v3（無変換でタイルを格納）
依存: pmtiles (pip install pmtiles)

usage:
  python xyz_to_pmtiles.py tiles/dem_png tiles/pmtiles/dem_png.pmtiles \
      --name "..." --description "..." --attribution "..."
"""
import argparse, math, os, sys, tempfile, time

from pmtiles.tile import zxy_to_tileid, tileid_to_zxy, TileType, Compression
from pmtiles.writer import Writer


def tile_lonlat_bounds(z, x, y):
    n = 2 ** z
    lon0 = x / n * 360 - 180
    lon1 = (x + 1) / n * 360 - 180
    lat = lambda t: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / n))))
    return lon0, lat(y + 1), lon1, lat(y)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tiledir"); ap.add_argument("output")
    ap.add_argument("--name", default="")
    ap.add_argument("--description", default="")
    ap.add_argument("--attribution", default="")
    ap.add_argument("--extra", action="append", default=[], help="追加メタデータ KEY=VALUE")
    a = ap.parse_args()

    t0 = time.time()
    entries = []
    for zs in os.listdir(a.tiledir):
        if not zs.isdigit():
            continue
        z = int(zs)
        for xs in os.listdir(os.path.join(a.tiledir, zs)):
            if not xs.isdigit():
                continue
            x = int(xs)
            for fn in os.listdir(os.path.join(a.tiledir, zs, xs)):
                if fn.endswith(".png") and fn[:-4].isdigit():
                    y = int(fn[:-4])
                    entries.append((zxy_to_tileid(z, x, y), z, x, y))
    entries.sort()
    if not entries:
        sys.exit("no tiles")
    minz, maxz = entries[0][1], entries[-1][1]
    print(f"{len(entries)} tiles, z{minz}-{maxz}, listing {time.time()-t0:.0f}s", flush=True)

    # 範囲は最大ズームのタイルから計算
    w = s = 1e9; e = n = -1e9
    for _, z, x, y in entries:
        if z == maxz:
            b = tile_lonlat_bounds(z, x, y)
            w, s, e, n = min(w, b[0]), min(s, b[1]), max(e, b[2]), max(n, b[3])

    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    tempfile.tempdir = os.path.dirname(os.path.abspath(a.output))  # 一時ファイルも同じディスクに
    tmp_out = a.output + ".part"
    with open(tmp_out, "wb") as f:
        wr = Writer(f)
        for i, (tid, z, x, y) in enumerate(entries, 1):
            with open(os.path.join(a.tiledir, str(z), str(x), f"{y}.png"), "rb") as tf:
                wr.write_tile(tid, tf.read())
            if i % 20000 == 0:
                print(f"  {i}/{len(entries)}  {time.time()-t0:.0f}s", flush=True)
        header = {
            "tile_type": TileType.PNG, "tile_compression": Compression.NONE,
            "min_zoom": minz, "max_zoom": maxz,
            "min_lon_e7": int(w * 1e7), "min_lat_e7": int(s * 1e7),
            "max_lon_e7": int(e * 1e7), "max_lat_e7": int(n * 1e7),
            "center_zoom": max(minz, min(maxz, 10)),
            "center_lon_e7": int((w + e) / 2 * 1e7), "center_lat_e7": int((s + n) / 2 * 1e7),
        }
        meta = {"name": a.name, "description": a.description, "attribution": a.attribution,
                "format": "png", "minzoom": minz, "maxzoom": maxz,
                "bounds": [round(w, 6), round(s, 6), round(e, 6), round(n, 6)]}
        for kv in a.extra:
            k, v = kv.split("=", 1); meta[k] = v
        wr.finalize(header, meta)
    os.replace(tmp_out, a.output)
    print(f"done: {a.output}  {os.path.getsize(a.output)/2**30:.2f} GiB  {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
