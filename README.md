# NovaEdit

Python 製の動画編集ソフト。AviUtl の表現力、Premiere の操作性、AI エージェントによる編集自動化を 1 つにまとめることを目指しています。

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

## ライセンス

MIT License。ただし PySide6 (LGPL) と ffmpeg を利用しているため、バイナリを配布する場合は各ライセンスの条件を確認してください。
