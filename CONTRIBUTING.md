# 開発に参加する

Kumiki はまだ **β 版**です。作りが大きく変わることがあります。

- **不具合の報告・要望** → [Issue](../../issues/new/choose)
- **どちらとも言えないこと** → [Discussions](../../discussions)
- **コードを書く** → 以下

**開発ルールの大本は [docs/development.md](docs/development.md)。** ここは手順だけ。

---

## 1. 環境を作る

Windows 専用です。Python 3.12 以上（開発は 3.14）と
[ffmpeg](https://www.gyan.dev/ffmpeg/builds/) が要ります。

```
git clone https://github.com/kagemorikosame/kumiki.git
cd kumiki
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

動くか確かめる:

```
.venv\Scripts\python.exe tools\verify.py
.venv\Scripts\python.exe -m kumiki
```

> **`python` ではなく `.venv\Scripts\python.exe` を使ってください。**
> 環境によっては素の `python` がランチャースタブで、標準入力から読ませると
> 応答が返らなくなります。

字幕起こしと AI 連携は既定では入りません（合計 2 GB を超えるため）。
ソフト内の導入ボタンから、必要になった時点で入れてください。

---

## 2. 変更を書く

```
git switch -c phase/やりたいこと
```

ブランチの名前:

| | |
|---|---|
| `phase/*` | フェーズ単位のまとまった作業 |
| `fix/*` | 不具合の修正 |

`main` へ直接 push しないでください。

書いている間に押さえること（詳しくは [docs/development.md](docs/development.md)）:

- **コメントは日本語で「なぜ」を書く。** 何をしているかはコードを読めば分かります
- **テストは失敗の仕方を書く。** assert が通ることだけを書いたテストは価値が薄い
- **`TODO` を残さない。** やり残しは Issue へ
- **互換層は実物で確かめる。** 形式の推測だけで書くと必ず外れます

---

## 3. 検証する

出す前に必ず:

```
.venv\Scripts\python.exe tools\verify.py
```

ruff（書式・規約）→ mypy（strict）→ pytest。**CI もこれと同じものを走らせます**
ので、手元で通れば CI でも通ります。

GPU が無い環境では OpenGL のテストが自動で飛びます。ffmpeg が無いときは
素材を使うテストが飛びます。どちらも失敗ではありません。

---

## 4. PR を出す

**PR はフェーズ単位**です。細かく切りすぎると全体像が見えず、大きすぎると
レビューが成立しません。

テンプレートに沿って書いてください。とくに:

- **なぜその作りにしたか**（差分を見れば「何をしたか」は分かります）
- `tools/verify.py` の結果（テスト件数）
- 途中で見つけた別の不具合

### CodeRabbit のレビュー

PR を出すと [CodeRabbit](https://coderabbit.ai/) が自動でレビューします。
設定は [`.coderabbit.yaml`](.coderabbit.yaml) で、このプロジェクトの約束
（コメントの書き方、コア層の依存、未対応の記録の仕方など）も見るようにしてあります。

- 指摘は**読んで判断**してください。全部直すのでも、全部無視するのでもありません
- 直さないときは、その理由をコメントに残してください
- 追加で見てほしいときは `@coderabbitai review` と書いてください
- 指摘に納得できないときは、そのコメントに返信すると会話できます

---

## ライセンス

MIT です。PR を送った時点で、その内容が MIT で配布されることに同意したものと
します。
