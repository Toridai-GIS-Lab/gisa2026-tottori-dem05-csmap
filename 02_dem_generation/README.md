# 02 地表面点群からの DEM・陰影図・CS立体図の作成

図郭単位の地表面点群（COPC LAZ）から、TIN 補間で 0.5 m DEM を作る。その DEM から陰影図と CS立体図を作成する。

## 必要なもの

- ルートの `environment.yml` の環境（PDAL ≥ 2.10、GDAL、NumPy、SciPy）
- 地表面点群（COPC LAZ）。1ファイル = 1図郭で、ファイル名が図郭番号（例 `05NB331...`）で始まること。そうでない場合は図郭範囲を CSV で与える
- XY が 1 cm 単位で記録されていること（`--xy-scale` で変更できる）
- ディスク容量の目安（鳥取県全域・1,296図郭の実績）: DEM 約 25 GiB、陰影図 約 10 GiB、CS立体図 約 18 GiB。4並列の作業領域として、さらに 20〜30 GiB 程度

## 手順

```bash
conda activate tottori-dem
S=02_dem_generation/scripts

# 0) PDAL の辺長制限の動作確認（任意）
python $S/test_edge_limit.py --workdir work/edge_test

# 1) 入力一覧を作る（図郭範囲・点数・座標系・COPCかどうかを検査）
python $S/make_inventory.py /path/to/ground_copc -o work/inventory.json --epsg 6673

# 2) まず1図郭で試す（モザイクは作らない）
python $S/tin_dem.py --inventory work/inventory.json --output work/dem_test --only 05NB331_grd

# 3) 全図郭の DEM・陰影図を作成し、全域にモザイクする
python $S/tin_dem.py --inventory work/inventory.json --output work/dem --workers 4 --memory-limit-gib 64

# 4) 検査（境界の段差と最終検査。陰影図の外部オーバービューも作る）
python $S/check_seams.py work/dem/<group>
python $S/final_qa.py work/dem/<group> --overviews

# 5) CS立体図
python $S/make_csmap.py work/dem/<group>/DEM/<group>_TIN_0p5m.tif work/csmap/<group>_CSMap_0p5m.tif --workers 4
```

`<group>` は入力フォルダ名である（`make_inventory.py --group` で変更できる）。

### make_inventory.py

- 図郭範囲の決め方:
  - `--tile-mode gsi2500`（既定）: ファイル名の図郭番号（地図情報レベル 2500: 2,000 m × 1,500 m、レベル 5000: 4,000 m × 3,000 m）から範囲を復元する。系番号と `--epsg` の一致も検査する。
  - `--tile-mode csv --tiles-csv tiles.csv`: 任意の図郭。CSV の列は `file,xmin,ymin,xmax,ymax`（X = 東西、Y = 南北）。
- 点群の範囲が図郭からはみ出す場合、COPC でない場合、座標系が違う場合は `inventory_errors.json` に記録する。このファイルが空でないと、`tin_dem.py` は実行されない。
- 出力範囲は点群の bbox ではなく図郭の範囲になる。点が端まで届いていない場合も、図郭範囲まで格子を作る。

### tin_dem.py

1. 対象図郭＋外側 `--buffer`（5 m）と交差する図郭（最大 9 図郭）を `readers.copc` で範囲指定して読み込み、`Withheld == 0` の点だけを使う。
2. 作業用コピーの XY を図郭原点からの整数 cm に変換して `filters.delaunay` で三角形分割し、メートル座標に戻す。PDAL 2.10.2 で一部の点群に不正な三角形網ができ、筋状の異常面が出た問題への対策である。
3. `filters.faceraster`（0.5 m、`max_triangle_edge_length=50`）でラスタ化する。辺が 50 m を超える三角形はラスタ化せず NoData にする。
4. バッファ付きの DEM から陰影図（方位 315°、高度 45°）を作り、整数セル位置で図郭に切り戻す。再補間はしない。
5. 全図郭について、三角形の向き・面積ゼロの三角形・面積総和と凸包面積の一致を検査する。
6. 全域にモザイクし、図郭 DEM との一致を確認する。陰影図は全域 DEM から作り直す。

同じ入力・同じ条件・同じスクリプトの場合だけ既存の結果を再利用する（SHA-256 で確認）。条件を変えるときは別の `--output` を指定すること。

| 主なオプション | 既定値 | 内容 |
|---|---|---|
| `--cell` | 0.5 | 出力セルサイズ [m] |
| `--buffer` | 5 | 周辺図郭から読み込む幅 [m] |
| `--max-edge` | 50 | TIN の最大辺長 [m]。0 で制限なし |
| `--xy-scale` | 100 | XY の記録単位の逆数（100 = 1 cm） |
| `--workers` | 4 | 同時に処理する図郭数 |
| `--memory-limit-gib` | 16 | 同時処理するジョブの推定メモリの上限 |
| `--keep-tiles` | — | モザイク後も図郭ごとのラスタを残す |

### make_csmap.py

| 項目 | 値 |
|---|---|
| 傾斜用 Gaussian | σ = 1.5 px（0.75 m）、radius 5 px |
| 曲率用 Gaussian | σ = 4.5 px（2.25 m）、radius 14 px |
| 曲率 | 平滑化 DEM の負の Laplacian、係数 1.1 |
| 合成 | `RGB = (0.125H + 0.125C1 + 0.25S1 + 0.5C2) × S2/255` |
| 出力 | RGBA GeoTIFF（BigTIFF、DEFLATE）。NoData は透明 |

NoData を標高 0 として混ぜないように、値と有効マスクを別々に Gaussian 処理してから正規化する。ブロックごとに周囲 16 px を余分に読み、継ぎ目が出ないようにしている。σ は画素単位で指定するので、セルサイズが 0.5 m 以外の場合は地上距離が変わる点に注意すること。

## 出力の構成

```
work/dem/
  inventory.json, summary.json
  <group>/
    configuration.json, tile_inventory.json, summary.json
    checks/<tile>.json          図郭ごとの検査結果
    jobs/<tile>/                実行した PDAL パイプライン、ログ、mesh_check.json
    DEM/<group>_TIN_0p5m.tif    Float32, NoData -9999
    DEM_hillshade/<group>_TIN_hillshade.tif   Byte, NoData 0
    boundary_*.csv / boundary_summary.json   （check_seams.py）
    final_validation.json, products.csv, preview_hillshade.png （final_qa.py）
```

## 鳥取県全域での実績（2026年10月）

| 指標 | 辺長上限なし | 最大辺長 50 m |
|---|---:|---:|
| DEM 有効セル | 13,541,212,653 | 12,944,408,215 |
| 境界の隣接セル差が 5 m を超える境界数 | 39 | 29 |
| 同・セル対数 | 853 | 138 |

- 有効セル面積は約 149 km² 減った。点のない場所をまたぐ巨大な三角形を除いたためである。
- 境界差は両側とも有効なセル対だけを集計している。差の減少をそのまま精度向上とは扱わない。最大約 57.9 m の境界差が残っている。
- 50 m は小範囲で 0.5〜100 m を比較して選んだ値であり、普遍的な最適値ではない。

## 補間手法の検討経緯

- **PDAL / GDAL の比較**（野呂山 UAV 点群）: IDW、mean、linear、average、invdist、nearest を比較した。手法によって補間される範囲（有効面積率）が大きく異なった。
- **日南町（05NB331）での5手法比較**（取り置いた 15,000 点に対する RMSE）: TIN 0.172 m、IDW 0.217 m、Natural Neighbor 0.171 m、ロバスト MLS 0.151 m、B-spline 0.470 m。MLS も候補だったが、全域処理は TIN で行った。この表から TIN が最も高精度だと結論したわけではない。
- **IDW**: power を変えても点状のアーティファクトは解消しなかった。
- **TIN の異常**: 05NB152 で最長辺約 587 m の不正な三角形が見つかった。整数 XY 化で回避した。点不足による正常な巨大三角形とは別の問題なので、最大辺長制限で別に対処した。

## 注意点

| 事象 | 原因・対処 |
|---|---|
| 図郭境界に欠損が出る | 周辺図郭の点を読んで補間し、図郭に切り戻す |
| 点があるのに筋状の異常面が出る | TIN の数値異常。整数 XY 化で回避する |
| 無点領域に大きな平面ができる | 最大辺長制限を使う。ただし内部の空白（池・水田など）も NoData になる |
| 5 m と 50 m を取り違える | 周辺の読み込み幅 5 m、TIN の最大辺長 50 m、境界差の検査しきい値 5 m は別の設定 |
| 縮小プレビューで条件を決めてしまう | 元の GeoTIFF の同じ場所を等倍で比較する |

元点群の地表面分類の精度、現地での標高精度、無点領域での地形の復元精度は検証していない。
