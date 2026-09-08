# NovaEdit

Python 製の動画編集ソフト。AviUtl の表現力、Premiere の操作性、AI エージェントによる編集自動化を 1 つにまとめることを目指しています。

![編集画面](docs/screenshot.png)

## 現状

**P1（編集コア）まで完成。** 素材の読み込みからカット編集、再生、書き出しまでが一本通る。

- 素材の読み込み（D&D / メニュー）と、映像・音声のリンクした自動配置
- タイムライン: 動画サムネイル・音声波形の表示、拡大縮小、スクラブ
- カット編集: 分割（S）、削除（Del）、詰めて削除（Shift+Del）、ドラッグ移動、端のトリム
- GPU 合成によるプレビュー（リニア空間、再生品質の切り替え）
- 音声再生（オーディオを時計にした A/V 同期）
- NVENC / CPU での書き出し
- プロジェクトの保存・読み込み（`.nvep`）と Undo / Redo

未着手: エフェクトとキーフレーム（P2）、字幕起こし（P3）、AI 連携（P4）、AviUtl / YMM4 互換（P5・P6）。

## 特徴（目標）

- マルチトラックのノンリニア編集と GPU 合成によるリアルタイムプレビュー
- タイムライン上に動画のフィルムストリップと音声波形を表示
- AviUtl / AviUtl2 のスクリプト・エイリアス資産の読み込み（Lua 実行を含む）
- YMM4 のアイテムテンプレートと文字装飾の取り込み
- 素材に紐付く字幕起こし。カット・分割・速度変更に自動追従する
- ソフト内で Claude Code と対話し、AI に実際の編集操作をさせられる

## 開発環境

- Windows 11 / Python 3.14
- ffmpeg 8.x が PATH 上にあること
- OpenGL 4.3 以上に対応した GPU

## セットアップ

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe tools\check_env.py
```

`check_env.py` は OpenGL コンテキスト、PyAV のデコード、NVENC の利用可否、オーディオ出力をまとめて確認します。ここが全部通ってから開発に入ってください。

## 起動

```
.venv\Scripts\python.exe -m novaedit
```

プロジェクトファイルを引数に渡すと、それを開いた状態で起動します。

### 主な操作

| 操作 | キー |
|---|---|
| 再生 / 停止 | Space |
| 再生ヘッドで分割 | S |
| 削除 / 詰めて削除 | Del / Shift+Del |
| コマ送り | ← / → |
| 先頭 / 末尾へ | Home / End |
| 拡大 / 縮小 | Ctrl+ホイール、または Ctrl+= / Ctrl+- |
| 全体を表示 | Shift+Z |
| 素材の読み込み | Ctrl+I |
| 書き出し | Ctrl+E |

## 開発

```
.venv\Scripts\python.exe toolserify.py
```

ruff・mypy・pytest をまとめて走らせます。テストは ffmpeg で実素材をその場に生成するので、
バイナリはリポジトリに含まれていません。

## ライセンス

MIT License。ただし PySide6 (LGPL) と ffmpeg を利用しているため、バイナリを配布する場合は各ライセンスの条件を確認してください。
