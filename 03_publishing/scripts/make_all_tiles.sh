#!/bin/bash
# 0.5 m DEM / 陰影図 / CS立体図 から XYZ タイル（z8-18）を作成
#
#   本番:       DATA_DIR=/path/to/data bash scripts/make_all_tiles.sh 2>&1 | tee make_all_tiles.log
#   ドライラン: DATA_DIR=/path/to/data DRYRUN=1 bash scripts/make_all_tiles.sh  （小範囲, z16-18 → tiles/_dryrun/）
#   （macOS でスリープを防ぐ場合は先頭に caffeinate -i を付ける）
#
# 中断しても同じコマンドで再実行すれば、既存タイルをスキップして再開する。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/config.sh"
cd "$DATA_DIR"

OUT=$TILE_DIR
DEM_RANGE=()
GDAL_RANGE=()
if [ "${DRYRUN:-0}" = "1" ]; then
  echo "##### DRYRUN: $DRYRUN_BBOX, z16-18, 出力 $TILE_DIR/_dryrun/ #####"
  MINZ=16
  OUT=$TILE_DIR/_dryrun
  DEM_RANGE=(--bbox "$DRYRUN_BBOX")
  # 上の bbox を z18 タイル番号に換算したもの（gdal raster tile は経緯度で範囲指定できないため）
  read -r -a GDAL_RANGE <<< "$DRYRUN_TILES"
fi
mkdir -p "$OUT"

echo "===== [1/3] 標高PNG（地理院形式） $(date) ====="
time python "$SCRIPT_DIR/gsi_dem_tiles.py" "$SRC_DEM" "$OUT/dem_png" \
  --minzoom $MINZ --maxzoom $MAXZ --workers $JOBS ${DEM_RANGE[@]+"${DEM_RANGE[@]}"}

# 注意: --oo OVERVIEW_LEVEL=NONE は必須。外すと z18 でも .ovr（縮小版）から作られ、ぼやけたタイルになる。
#       下位ズームは z18 のタイルを縮小して作られるので、.ovr は不要。
echo "===== [2/3] 陰影図 $(date) ====="
time gdal raster tile \
  --min-zoom=$MINZ --max-zoom=$MAXZ \
  -r bilinear --overview-resampling=average \
  --add-alpha --skip-blank --resume --convention=xyz \
  --webviewer=none -j $JOBS ${GDAL_RANGE[@]+"${GDAL_RANGE[@]}"} \
  --oo OVERVIEW_LEVEL=NONE \
  "$SRC_HS" "$OUT/hillshade"

echo "===== [3/3] CS立体図 $(date) ====="
time gdal raster tile \
  --min-zoom=$MINZ --max-zoom=$MAXZ \
  -r bilinear --overview-resampling=average \
  --skip-blank --resume --convention=xyz \
  --webviewer=none -j $JOBS ${GDAL_RANGE[@]+"${GDAL_RANGE[@]}"} \
  --oo OVERVIEW_LEVEL=NONE \
  "$SRC_CS" "$OUT/cs_topo"

echo "===== 集計 $(date) ====="
for d in dem_png hillshade cs_topo; do
  echo "$d: $(find "$OUT/$d" -name '*.png' | wc -l) files, $(du -sh "$OUT/$d" | cut -f1)"
done
