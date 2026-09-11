# Kumiki — コーディングエージェント向けの指示

Codex など、`AGENTS.md` を読む道具向け。**内容は [CLAUDE.md](CLAUDE.md) と同じ。**
開発ルールの大本は [docs/development.md](docs/development.md)。

## 必ず守ること

1. **Python は `.venv\Scripts\python.exe` を使う。**
   素の `python` はランチャースタブで、標準入力から読ませると応答が返らない。

2. **実装したら `tools\verify.py` を走らせる。**
   ```
   .venv\Scripts\python.exe tools\verify.py
   ```
   ruff → mypy(strict) → pytest。1 つでも落ちたら終了コードが非 0。

3. **コメントは日本語で「なぜ」を書く。** 何をしているかはコードを読めば分かる。

4. **TODO を残さない。** やり残しは Issue かロードマップへ。

5. **互換層（AviUtl / YMM4）は実物を通すまで完成としない。**

## 作業の単位

PR はフェーズ単位。`phase/*` ブランチを切って `main` へ。`main` へ直接 push しない。

## 覚えておくと早いこと

- 版の出どころは `src/kumiki/__init__.py` の `__version__` 1 か所だけ
- Y 軸は上が正（テキスト・影・変形・マスクすべて）
- コア層（`src/kumiki/core/`）は PySide6 を import しない
- 変更は必ず `Command` 経由
