# 03 DEM からの公開用データ作成

工程 [02](../02_dem_generation/) で作った DEM・陰影図・CS立体図を、XYZ タイル（標高は国土地理院形式の標高PNG）と PMTiles に変換し、Source Cooperative で公開する。

- 公開先: https://source.coop/wata909/tottori-dem05-csmap
- ビューア: https://data.source.coop/wata909/tottori-dem05-csmap/index.html

## 成果物（鳥取県全域）

| レイヤ | XYZ | PMTiles | 枚数 | 容量 |
|---|---|---|---|---|
| 標高PNG（地理院形式、0.01 m） | `dem_png/{z}/{x}/{y}.png` | `pmtiles/tottori_dem05_gsi.pmtiles` | 289,641 | 17 GB / 16.6 GiB |
| 陰影図 | `hillshade/{z}/{x}/{y}.png` | `pmtiles/tottori_hillshade05.pmtiles` | 289,579 | 13 GB / 12.8 GiB |
| CS立体図 | `cs_topo/{z}/{x}/{y}.png` | `pmtiles/tottori_csmap05.pmtiles` | 289,641 | 23 GB / 22.3 GiB |

- タイル方式: WebMercatorQuad（EPSG:3857）、XYZ（y は北から）、256 px、z8–18
- z18 は北緯 35.4° で約 0.49 m/px となり、元の 0.5 m 格子とほぼ一致する
- データのないタイルは作らない（HTTP 404 になる）

## 必要なもの

- ルートの `environment.yml` の環境（GDAL ≥ 3.13 の新 CLI `gdal raster tile`、numpy、pmtiles）
- アップロードする場合: AWS CLI 2.x と Source Cooperative CLI（`source-coop`）
- bash（Windows では WSL か Git Bash）

## 設定

設定はすべて `scripts/config.sh` にまとめてあり、環境変数で上書きできる。

| 変数 | 既定値 | 内容 |
|---|---|---|
| `DATA_DIR` | （必須） | 入力ラスタと出力タイルを置くディレクトリ |
| `SRC_DEM` / `SRC_HS` / `SRC_CS` | `DEM_05m/...` | DEM・陰影図・CS立体図。`DATA_DIR` からの相対パスか絶対パス |
| `TILE_DIR` / `PMTILES_DIR` | `tiles` / `pmtiles` | 出力先 |
| `MINZ` / `MAXZ` | 8 / 18 | ズーム範囲 |
| `JOBS` | CPU数 − 2 | 並列数 |
| `DRYRUN_BBOX` / `DRYRUN_TILES` | 鳥取市付近 | ドライランの範囲 |
| `SC_PROFILE` / `SC_DEST` | `source-coop` / `s3://wata909/tottori-dem05-csmap` | アップロード先。自分のプロダクトに変更すること |

工程02の出力を直接使う例:

```bash
export DATA_DIR=/path/to/work
export SRC_DEM=dem/ALL_laz/DEM/ALL_laz_TIN_0p5m.tif
export SRC_HS=dem/ALL_laz/DEM_hillshade/ALL_laz_TIN_hillshade.tif
export SRC_CS=csmap/ALL_laz_CSMap_0p5m.tif
```

## 手順

リポジトリのルートで `conda activate tottori-dem` してから、`03_publishing/` で実行する。

### 1. ドライラン（小範囲で全工程を確認する）

```bash
DRYRUN=1 bash scripts/make_all_tiles.sh
```

`tiles/_dryrun/` に z16–18 のタイルが出力される。`viewer/index.html?root=<タイルのURL>&minz=16` で表示を確認できる（ローカルなら `python -m http.server` で配信する）。

### 2. タイルの作成

```bash
bash scripts/make_all_tiles.sh 2>&1 | tee make_all_tiles.log
```

中断しても、同じコマンドで既存のタイルをスキップして再開できる。macOS でスリープを防ぐには先頭に `caffeinate -i` を付ける。

| 工程 | 方法 | 所要時間（Apple Silicon 12コア） |
|---|---|---|
| 標高PNG | `gsi_dem_tiles.py`（z18 は元 DEM から作成、z17 以下は子タイル4枚の平均） | 15 分 |
| 陰影図 | `gdal raster tile`（PNG 灰色＋アルファ） | 7 分 |
| CS立体図 | `gdal raster tile`（PNG RGB/RGBA） | 10 分 |

標高PNGの作り方（`gsi_dem_tiles.py`）:

1. z18 の各タイルについて、元 DEM を `gdal.Warp` で EPSG:3857 の 256 × 256 に bilinear で再投影する（float のまま）。
2. 地理院形式にエンコードする。`x = round(h / 0.01)`、`x < 0` なら `x += 2^24`、R,G,B = x の上位・中位・下位バイト、NoData は `(128,0,0)`。
3. z17 から z8 は、子タイル4枚をデコードして 2 × 2 で平均（NoData は除外）し、再エンコードする。元 DEM のオーバービューは不要。
4. 全面が NoData のタイルは出力しない。

### 3. 検証

```bash
python scripts/verify_dem_tiles.py "$DATA_DIR/$SRC_DEM" "$DATA_DIR/tiles/dem_png"
```

鳥取県全域での結果（150 タイル・17,260 点）: 元 DEM を独立に bilinear 補間した値との差は最大 0.0052 m（丸めによる差のみ）。

### 4. PMTiles の作成と検証

```bash
bash scripts/make_all_pmtiles.sh 2>&1 | tee make_all_pmtiles.log
python scripts/verify_pmtiles.py "$DATA_DIR/tiles/dem_png" "$DATA_DIR/pmtiles/tottori_dem05_gsi.pmtiles"
```

XYZ のタイルを変換せずにそのまま格納する（1レイヤ 2〜3 分）。

### 5. Source Cooperative への公開

1. Web でプロダクトを作成する。鳥取県のデータでは Data Connection に AWS Open Data（AP-Northeast-1、東京）を選んだ。
2. 認証:

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/source-cooperative/source-coop-cli/releases/latest/download/source-coop-cli-installer.sh | sh
source-coop login
```

`~/.aws/config` に以下を追加する:

```ini
[profile source-coop]
credential_process = source-coop creds
endpoint_url = https://data.source.coop
s3 =
  max_concurrent_requests = 16
  multipart_chunksize = 64MB
```

3. アップロード:

```bash
bash scripts/upload_sourcecoop.sh 2>&1 | tee upload.log
bash scripts/upload_sourcecoop.sh retry upload.log      # 失敗したファイルだけ再送
```

- 合計約 105 GB（XYZ 約 87 万ファイル＋PMTiles 3 ファイル）で、約 4 時間かかった。
- Cloudflare の `520` エラーが散発する（87 万件中約 57 件）。AWS CLI の自動再試行の対象外なので、`retry` で再送する。
- 並列数 64 では PMTiles の分割アップロードが失敗したため、16 に下げた。

## ビューア（`viewer/index.html`）

Leaflet 1.9.4。既定では Source Cooperative 上のタイルを読み込む（`?root=` で切り替えられる）。段彩（標高PNGをブラウザでデコード）、陰影図、CS立体図、乗算表示、鳥取県公開版との比較、カーソル位置の標高差を表示できる。

## 注意点

| 事象 | 原因・対処 |
|---|---|
| 陰影図タイルがぼやける | `--oo OVERVIEW_LEVEL=NONE` を付けないと、z18 でも `.ovr`（縮小版）から作られる |
| CS立体図で `ZIPDecode` エラー | `.ovr` の破損。`OVERVIEW_LEVEL=NONE` で回避できる |
| EPSG:6673 の定義差異の警告 | 投影パラメータは同一。`GTIFF_SRS_SOURCE=EPSG` で抑制する |
| `gdal raster tile --help` が1行しか出ない | `--long-usage` を使う |
| アップロードで 520 | 一時的なエラー。失敗分だけ再送する |

## 出典表記

> 鳥取県 航空レーザ測量点群データ（PDL1.0）を加工して作成: 鳥取大学 地理情報科学研究室 (2026)
>
> この測量成果は、国土地理院長の承認を得て同院所管の測量成果を使用して得たものである。（承認番号）令6中公第450号（一部区域）

詳細は [docs/sourcecoop_README.md](docs/sourcecoop_README.md) を参照のこと。

## 参考

- 鳥取県 森林情報等の公開: https://www.pref.tottori.lg.jp/322140.htm
- G空間情報センター 鳥取県 DEM 0.5m: https://www.geospatial.jp/ckan/dataset/dem05_tottori
- 国土地理院 標高タイル: https://maps.gsi.go.jp/development/demtile.html
- GDAL `gdal raster tile`: https://gdal.org/en/stable/programs/gdal_raster_tile.html
- Source Cooperative CLI: https://github.com/source-cooperative/source-coop-cli
- PMTiles: https://github.com/protomaps/PMTiles
