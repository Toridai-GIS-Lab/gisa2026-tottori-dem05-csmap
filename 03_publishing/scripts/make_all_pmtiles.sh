#!/bin/bash
# XYZ タイル → PMTiles（dem_png / hillshade / cs_topo）
#   DATA_DIR=/path/to/data bash scripts/make_all_pmtiles.sh 2>&1 | tee make_all_pmtiles.log
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/config.sh"
cd "$DATA_DIR"
mkdir -p "$PMTILES_DIR"
SRC="${ATTRIBUTION:-鳥取県 航空レーザ測量点群（PDL1.0）を加工して作成: 鳥取大学 地理情報科学研究室}"

echo "===== dem_png $(date) ====="
time python "$SCRIPT_DIR/xyz_to_pmtiles.py" "$TILE_DIR/dem_png" "$PMTILES_DIR/tottori_dem05_gsi.pmtiles" \
  --name "Tottori LiDAR 0.5m Elevation PNG (GSI format)" \
  --description "標高PNG（国土地理院形式, 0.01m）: x=R*2^16+G*2^8+B, x<2^23: h=0.01x, x>2^23: h=0.01(x-2^24), (128,0,0)=NoData" \
  --attribution "$SRC" --extra encoding=gsi-elevation-png --extra type=overlay

echo "===== hillshade $(date) ====="
time python "$SCRIPT_DIR/xyz_to_pmtiles.py" "$TILE_DIR/hillshade" "$PMTILES_DIR/tottori_hillshade05.pmtiles" \
  --name "Tottori LiDAR 0.5m Hillshade" --description "陰影図" \
  --attribution "$SRC" --extra type=overlay

echo "===== cs_topo $(date) ====="
time python "$SCRIPT_DIR/xyz_to_pmtiles.py" "$TILE_DIR/cs_topo" "$PMTILES_DIR/tottori_csmap05.pmtiles" \
  --name "Tottori LiDAR 0.5m CS Map" --description "CS立体図" \
  --attribution "$SRC" --extra type=overlay

ls -la "$PMTILES_DIR/"
