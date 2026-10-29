#!/usr/bin/env python3
"""
PMTiles と元の XYZ ディレクトリの照合: タイル数、無作為抽出タイルのバイト一致、範囲外タイルが None になること。

usage: python verify_pmtiles.py tiles/dem_png pmtiles/tottori_dem05_gsi.pmtiles [--samples 300]
"""
import argparse, os, random, sys
from pmtiles.reader import Reader, MmapSource


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tiledir"); ap.add_argument("pmtiles")
    ap.add_argument("--samples", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    n_files = sum(len(fs) for _, _, fs in os.walk(a.tiledir) if fs and any(f.endswith(".png") for f in fs))
    with open(a.pmtiles, "rb") as f:
        r = Reader(MmapSource(f)); h = r.header()
        random.seed(a.seed); ok = n = 0
        zs = [z for z in os.listdir(a.tiledir) if z.isdigit()]
        while n < a.samples:
            z = random.choice(zs); x = random.choice(os.listdir(os.path.join(a.tiledir, z)))
            ys = os.listdir(os.path.join(a.tiledir, z, x))
            if not ys: continue
            y = random.choice(ys); n += 1
            with open(os.path.join(a.tiledir, z, x, y), "rb") as tf:
                ok += r.get(int(z), int(x), int(y[:-4])) == tf.read()
        print(f"{os.path.basename(a.pmtiles)}: addressed {h['addressed_tiles_count']} / XYZ files {n_files}, "
              f"z{h['min_zoom']}-{h['max_zoom']}, clustered {h['clustered']}, byte match {ok}/{n}, "
              f"empty->None {r.get(18, 0, 0) is None}")
        good = h["addressed_tiles_count"] == n_files and ok == n
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
