#!/bin/bash
# Source Cooperative へのアップロード
#   前提: source-coop login 済み、~/.aws/config に [profile source-coop] を設定済み（README 参照）
#         SC_DEST を自分のプロダクトに設定（config.sh または環境変数）
#   使い方: DATA_DIR=/path/to/data bash scripts/upload_sourcecoop.sh 2>&1 | tee upload.log
#   失敗分のみ再送: DATA_DIR=/path/to/data bash scripts/upload_sourcecoop.sh retry upload.log
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/config.sh"
cd "$DATA_DIR"
P=(--profile "$SC_PROFILE")

if [ "${1:-}" = "retry" ]; then
  # ログの "upload failed: <local> to <s3>" から失敗ファイルだけを個別に再送
  LOG="${2:?usage: upload_sourcecoop.sh retry <logfile>}"
  grep "upload failed" "$LOG" | awk '{print $3}' > failed.txt
  echo "retry $(wc -l < failed.txt) files"
  : > failed_again.txt
  while read -r f; do
    rel="${f#$TILE_DIR/}"; rel="${rel#./}"
    aws s3 cp "$f" "$SC_DEST/$rel" "${P[@]}" --only-show-errors || echo "$f" >> failed_again.txt
  done < failed.txt
  [ -s failed_again.txt ] && { echo "still failed:"; cat failed_again.txt; exit 1; } || echo "all uploaded"
  exit 0
fi

echo "== README / viewer $(date)"
aws s3 cp "$REPO_DIR/docs/sourcecoop_README.md" "$SC_DEST/README.md" "${P[@]}"
aws s3 cp "$REPO_DIR/viewer/index.html" "$SC_DEST/index.html" --content-type "text/html; charset=utf-8" "${P[@]}"

echo "== PMTiles $(date)"
aws s3 sync "$PMTILES_DIR" "$SC_DEST/pmtiles" --exclude "*.log" --exclude "*.part" "${P[@]}"

for d in dem_png hillshade cs_topo; do
  echo "== $d $(date)"
  aws s3 sync "$TILE_DIR/$d" "$SC_DEST/$d" "${P[@]}" --only-show-errors
done
echo "== done $(date)"
