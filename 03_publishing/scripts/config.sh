# 共通設定（各 make_*.sh / upload_*.sh から source される）
# すべて環境変数で上書きできる。最低限 DATA_DIR を指定すること。
#
#   DATA_DIR=/path/to/data bash scripts/make_all_tiles.sh
#
# DATA_DIR: 入力ラスタと出力タイルを置くディレクトリ
if [ -z "${DATA_DIR:-}" ]; then
  echo "ERROR: DATA_DIR を指定してください（例: DATA_DIR=/path/to/data bash $0）" >&2
  exit 1
fi

# 入力（DATA_DIR からの相対パス、または絶対パス）
#   工程02の出力をそのまま使う場合の例:
#   SRC_DEM=dem/ALL_laz/DEM/ALL_laz_TIN_0p5m.tif
SRC_DEM="${SRC_DEM:-DEM_05m/DEM/ALL_laz_TIN_0p5m.tif}"                                  # float32, NoData -9999
SRC_HS="${SRC_HS:-DEM_05m/DEM_hillshade/ALL_laz_TIN_hillshade.tif}"                     # Byte, NoData 0
SRC_CS="${SRC_CS:-DEM_05m/CS_Topo/ALL_laz_edge50m_CSMap_s003_c006_0p5m.tif}"           # RGBA Byte

# 出力
TILE_DIR="${TILE_DIR:-tiles}"          # tiles/{dem_png,hillshade,cs_topo}/{z}/{x}/{y}.png
PMTILES_DIR="${PMTILES_DIR:-pmtiles}"

# タイル設定
MINZ="${MINZ:-8}"
MAXZ="${MAXZ:-18}"
JOBS="${JOBS:-$( (nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4) | awk '{print ($1>2)?$1-2:1}')}"

# ドライラン範囲（経緯度 / z18 タイル番号）。既定は鳥取市付近
DRYRUN_BBOX="${DRYRUN_BBOX:-134.20,35.50,134.26,35.55}"
DRYRUN_TILES="${DRYRUN_TILES:---min-x=228793 --max-x=228837 --min-y=103344 --max-y=103388}"

# Source Cooperative（自分のアカウント・プロダクトに変更する）
SC_PROFILE="${SC_PROFILE:-source-coop}"
SC_DEST="${SC_DEST:-s3://wata909/tottori-dem05-csmap}"

export GTIFF_SRS_SOURCE=EPSG   # GeoTIFFキーとEPSG定義の差異警告を抑制（投影パラメータは同一）
