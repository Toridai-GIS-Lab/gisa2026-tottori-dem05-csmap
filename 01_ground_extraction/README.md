# 01 地表面点群の抽出

航空レーザ測量の点群（LAS/LAZ）から、PDAL の地表面フィルタ（SMRF / PMF / CSF）で地表面点を抽出する。UAV 等の基準点群がある場合は、抽出結果の精度を比較できる。

出力は COPC LAZ。そのまま工程 [02](../02_dem_generation/) の入力になる。

## 必要なもの

- ルートの `environment.yml` の環境（PDAL ≥ 2.10、Python、NumPy、SciPy）
- 点群データ（各自で用意する。リポジトリには含まない）

## スクリプト

| スクリプト | 内容 |
|---|---|
| `scripts/extract_ground.py` | 1ファイル〜フォルダ単位の地表面点抽出。並列実行・既存出力のスキップ・処理記録（manifest CSV）に対応 |
| `scripts/convert_text.py` | XYZ テキスト（UAV 地上点など）を COPC LAZ に変換 |
| `scripts/evaluate_ground.py` | 抽出点群と基準点群の標高差（dz）を集計 |
| `scripts/run_samples.py` | `samples.json` に書いた複数地域 × 複数手法の抽出と評価を一括で実行 |
| `config/params.json` | 各手法のパラメータ |
| `config/samples.example.json` | 手法比較用の地域定義の例 |

## 使い方

### A. 広域の点群から地表面点を抽出する

```bash
conda activate tottori-dem
python scripts/extract_ground.py /path/to/laz_folder -o /path/to/ground --method smrf --workers 4
```

- 入力フォルダの直下にある `*.las` / `*.laz` を処理する（サブフォルダは見ない）。
- 出力名は `<入力名>_<手法>.copc.laz`。入力名末尾の `_org` と `.copc` は取り除く。
- 出力が既にあれば点数を確認したうえでスキップする。作り直す場合は `--overwrite` を付ける。
- `<出力>/_logs/` に、ファイルごとの PDAL パイプラインとログを保存する。
- Windows で日本語を含むパスの処理に失敗する場合は `--ascii-temp` を付ける。入力を英数字名の一時ファイルにコピーしてから処理する。

### B. 基準点群（UAV）と比較して手法を選ぶ

1. `config/samples.example.json` を `config/samples.json` にコピーし、点群のパスを書き換える。パスは `samples.json` の場所からの相対パスでも書ける。
2. 実行する。

```bash
python scripts/run_samples.py config/samples.json -o results/
```

`results/summary.csv` と `results/summary.md` に、地域 × 手法ごとの抽出点数・評価点数・dz の RMSE などが出力される。

### 処理内容

```
readers.las
→ filters.assign  Classification[:]=0
→ filters.assign  ReturnNumber[0:0]=1, NumberOfReturns[0:0]=1
→ filters.smrf | filters.pmf | filters.csf
→ filters.range   Classification[2:2]
→ writers.copc
```

| 手法 | パラメータ（`config/params.json`） |
|---|---|
| SMRF | slope 0.15, window 18.0, threshold 0.5, scalar 1.25 |
| PMF | max_window_size 33, slope 1.0, max_distance 2.5, initial_distance 0.15, cell_size 1.0 |
| CSF | resolution 1.0, threshold 0.5, rigidness 3, iterations 500 |

これらは保存されていた処理条件であり、あらゆる地形での最適値を検証したものではない。なお SMRF の `slope=0.15` は「傾斜15度」という意味ではない。

### 評価の定義（`evaluate_ground.py`）

抽出点ごとに、XY 平面で最も近い基準点を探す。その距離が `--radius`（既定 0.5 m）以内なら、`dz = z(抽出点) − z(基準点)` を集計する。半径内に基準点がない抽出点（UAV 範囲の外や欠測部）は評価しない。`reference_covered_ratio` は、半径内に抽出点がある基準点の割合で、抽出の網羅性の目安になる。

## 注意点

| 事象 | 内容 |
|---|---|
| 元の分類7（ノイズ）が除外されない | Classification を最初に 0 に初期化するので、SMRF の `ignore=Classification[7:7]` は効かない。元の処理を再現するために残している |
| ReturnNumber 等が 0 | 0 を 1 に補正してからフィルタにかける |
| 旧評価の値 | 旧版の評価パイプライン（`03*_evaluate_*.json`）は、UAV 点群と抽出点群を結合してから `filters.nndistance` を計算していた。この値は「抽出点から基準点までの誤差」ではない。本リポジトリの `evaluate_ground.py` で再評価すること |
| COPC と LAZ | 工程02は COPC を前提にしている。`--format laz` で出力した場合は `pdal translate in.laz out.copc.laz -w writers.copc` で COPC に変換する |
