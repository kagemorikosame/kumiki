# 開発ルール

Sashimono の開発で守ることをここにまとめる **このファイルが大本**で、
`CLAUDE.md` / `AGENTS.md` / `.cursor/rules/` はここを指しているだけ
ルールを変えるときはここを直す

---

## 1. 環境

### Python は必ず仮想環境のものを使う

```
.venv\Scripts\python.exe
```

**素の `python` を使わない** 開発機の `python` は PEP 514 のランチャースタブで、
標準入力からスクリプトを読ませると**応答が返らなくなる** ヒアドキュメントと
組み合わせると対話モードで起動し、出力ファイルが数 GB まで膨らんだことがある

### 導入

```
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

字幕起こし（`asr`）と AI 連携（`ai`）は**既定では入れない** 合計 2 GB を超えるので、
ソフト内の導入ボタンから必要になった時点で入れる

---

## 2. 実装したら必ず検証する

```
.venv\Scripts\python.exe tools\verify.py
```

ruff（書式・規約）→ mypy（strict）→ pytest をまとめて走らせる **1 つでも落ちたら
終了コードが非 0** CI もこれと同じものを走らせるので、手元で通れば CI でも通る

個別に走らせたいとき:

```
.venv\Scripts\python.exe -m ruff format src tests tools
.venv\Scripts\python.exe -m ruff check --fix src tests tools
.venv\Scripts\python.exe -m mypy src tests
.venv\Scripts\python.exe -m pytest -q
```

**AI（Claude Code / Codex / Cursor）に実装させた場合も同じ** 実装 → リファクタ →
`tools/verify.py` まで含めて 1 つの作業とする 「動くコードを書いた」で止めない

---

## 3. 書き方

### コメントは「なぜ」を書く

日本語で書く **何をしているかはコードを読めば分かる** 書くのは、そう書いた理由と、
そう書かないとどうなるか

```python
# 秒は必ず分数で持つ 浮動小数にすると保存と読み込みを繰り返すだけで値が動く
```

将来の自分と他人が「これは消してよいのか」を判断できるようにするのが目的

### 文章に句点（まる）を使わない

コメント・docstring・画面や例外に出す文言・Markdown・設定ファイルのコメント・
コミットメッセージ・PR の本文、どれも句点で文を終えない

- 行の終わりや閉じ括弧の前では、何も付けずに終える
- 1 行に 2 つの文を続けるときは、半角空白で区切る
- 読点（、）はこれまでどおり使う

```python
# 秒は必ず分数で持つ 浮動小数にすると保存と読み込みを繰り返すだけで値が動く
```

**データとしての句点は例外** 字幕整形が句点として扱う文字の一覧や正規表現、
テストの入力データ（起こし結果の字幕文など）は、中身そのものなので残す
ここを消すと字幕の改行位置や句読点の処理が壊れる

リポジトリの中のファイルは `tools\punctuation.py` が見分けて検査する
（`tools\verify.py` と CI に入っているので、落ちたら気付ける）
コミットメッセージと PR の本文は道具では見ないので、書くときに気を付ける
うっかり書いてしまったら次で直せる

```
.venv\Scripts\python.exe tools\punctuation.py --fix
```

### テストは失敗の仕方を書く

`assert` が通ることではなく、**壊れたときに何が起きるか**をテスト名とコメントに残す

```python
def test_the_animation_uses_the_whole_length(self) -> None:
    # 並び順をフレーム番号だと思うと、300 フレームの動きが 2 フレームで終わる
```

### TODO を残さない

`TODO` / `FIXME` をコードに書かない やり残しは
[[実装ロードマップ]]（Obsidian）か GitHub の Issue に出す コード内の TODO は
誰も見ないまま残る

### 型は strict で通す

`mypy --strict` が通らないものは入れない `type: ignore` を書くときは、理由を
同じ行か直前のコメントに残す

---

## 4. 設計の約束

### コア層は GUI に依存しない

`src/sashimono/core/` は PySide6 を import しない CLI からもテストからも AI からも
同じ API で動くことが、AI 連携とテスト容易性の前提になっている

### 変更は必ずコマンド経由

UI の操作も AI の操作も同じ `Command` を発行し、同じ Undo スタックに載る
モデルを直接書き換えない

### パラメータ定義は 1 形式

自前のエフェクトも AviUtl の配布スクリプトも、同じ `ParameterSpec` に載せる
設定 UI の自動生成・プリセット・キーフレームがこれ 1 つで済む **新しい種類の
パラメータを足すときは、AviUtl の制御文字と対応が付くかを先に考える**

### 好みが分かれる所は、設定画面から変えられるようにする

**動きを勝手に決めない** 人によって答えが変わる項目は、決め打ちにせず
設定（`表示 → 設定…`）に出す 実装する前に「これは全員にとって同じ答えか」を
1 度考える

答えが分かれるのは、たとえばこういうもの

- 自動で何かが変わる（大きい素材でプレビューの画質を落とす、など）
  速さを取る人と画質を取る人がいる
- 速さと品質の釣り合い（控えの大きさ、先読みの量）
  機械の速さで答えが変わる
- 見た目の好み（表示する単位、色、並び順）
- 操作の癖（確認を出すか、どこまで自動で進むか）

決め方

1. **既定は、知らない人が困らない側にする** 何も設定しない人が一番多い
   自動で軽くする側を既定にして、嫌な人が切れるようにする
2. 置き場は `ui/workspace.py` の `Preferences`
   プロジェクトではなく**本人と機械**に付く（同じプロジェクトを速い機械で
   開いたら等倍で見たいことがある） プロジェクト設定と混ぜない
3. 壊れた値で起動を止めない 項目ごとに既定へ戻す
4. **測った値があるなら、設定画面にも書く** 「なんとなく軽くなる」ではなく、
   どれを選ぶと何 ms になるのかを見て選べるようにする
5. 切ったら本当に止まることを試験で押さえる 切っても効いたままなら、
   設定がある方が質が悪い

### 版の出どころは 1 か所

`src/sashimono/__init__.py` の `__version__` だけ `pyproject.toml` はここから読む
2 か所に書くと、配った版と中身の版が必ずずれる

### 本人の置き場の名前は 1 か所、旧名は印で囲む

設定・退避・キャッシュ・導入した実行環境の場所は `src/sashimono/core/userdirs.py` から取る
`%APPDATA%` などを各所で直に組み立てない 改名で置き場の名前を変えたときに、
引き継ぎの側と食い違う

改名前の名前（古い拡張子・形式名・置き場）は、読む側と引き継ぐ側にだけ残す
その範囲は「旧名を残す」の印の行で囲む（印の後ろに `: ここから` と `: ここまで`、
ファイルごと残すときは `: このファイル全体` を付ける 書き方は `core/io/serialize.py` を見る
ここに印そのものを書くと、この文書まで一括置換から外れる） 名前を一括で
置き換える道具はこの範囲を飛ばす 囲まずに書くと、一括置換で新しい名前に書き換わり、
改名前に保存した作品が開けなくなる（`tests/test_legacy_names.py` が落ちる）

### Y 軸は上が正

テキストの位置・影のずれ・変形・マスク、すべて**正の値で上へ動く**
過去に変形とマスクだけ逆になっていて、配布エイリアスを描いて初めて気付いた
新しく座標を扱うものを足すときは、向きをテストで固定する

例外は次の 2 つだけで、どちらも**表示名に「下が正」と書く**

| エフェクト | 設定 | なぜ |
| --- | --- | --- |
| `brush_fill` | 模様の中心・ずらし・ノイズの位置と速さ | YMM4 のブラシの値をそのまま持つ |
| `particles` | 放つ位置 Y | YMM4 のパーティクルの値をそのまま持つ |

どちらも「YMM4 の設定をそのまま渡して同じ絵になる」ことを優先している
シェーダの中で画面の向きへ直すので、写す側で反転してはいけない

---

## 5. 互換層は実物で確かめる

AviUtl / YMM4 の読み込みは、**実際に配布されているファイルを通すまで完成としない**

形式の推測だけで書いた YMM4 の読み手は、実物では 1 本も読めなかった
（`.ymmt` が ZIP だと知らず、素の JSON として開いて落ちた） AviUtl でも同じことが
起きている 資料と現物は食い違う

手順:

1. 実際に配布されているものを手に入れる
2. 片端から通し、記録された未対応を**数える**
3. **回数の多い順に**埋める 全部は埋めない

未対応は握り潰さず `CompatibilityReport` に残す ソフト内の
〔互換〕→〔互換性レポート…〕で一覧できる

**配布物そのものはリポジトリに入れない** 作者ごとに再配布の条件が違う
（例外は `licenses/` の使用許諾の全文と写し 配る zip に一緒に入れなければならない
物で、どれも原文のまま写してよいと書かれている 取得元と版は `THIRD_PARTY_NOTICES.md`、
GNU の全文は FSF の配る物と sha256 が同じことを試験で確かめている）
`tests/fixtures/ymm4` に置けばテストが拾い、無ければ飛ばす

### 値の意味は本体に描かせて読む

JSON のキーの名前から意味を推し量ると必ず外れる（`InOutZoom` の `Value` は
「縮める量」ではなく「隠れたときの大きさ」だった） 2 つの道具でそれを潰す

- `tools/ymm4_probes.py` … 値を 1 つずつ変えたテンプレートを作る
  （`第 1〜5 弾` を引数で選ぶ: `first` `second` `third` `fourth` `fifth`）
- `tools/ymm4_compare.py` … テンプレートを時間差で並べた `.ymmp` を作り（`build`）、
  YMM4 が書き出した動画と Sashimono の絵をフレームごとに比べる（`compare`）

```
.venv\Scripts\python.exe tools\ymm4_probes.py .work\probe5\probes.ymmt fifth
.venv\Scripts\python.exe tools\ymm4_compare.py --work .work\probe5 build .work\probe5\probes.ymmt
:: YMM4 で .work\probe5\compare.ymmp を開き、.work\probe5\ymm4.mp4 へ書き出す
.venv\Scripts\python.exe tools\ymm4_compare.py --work .work\probe5 compare
```

`compare` は `report.html` と、`ymm4 | Sashimono | 差` を並べた画像を `images/` に書く
差は 480x270 に縮めた平均なので、細い縁の違いは数に出にくい 数字だけでなく絵も見る

既定ではテンプレート 1 本につき 3 枚だけを比べる `--every` を付けると枠のフレームを
すべて比べ、1 本ごとに一番大きい差の 1 枚を出す 場面の切れ目のように一瞬だけずれる所は
3 枚では見落とす

YMM4 の書き出しは、最初の 1 枚の時刻が 0 ではなく 1 フレーム後ろから始まる 道具は
動画の頭の時刻を引いてそろえている 自前で書き出しを読むときも同じようにしないと、
YMM4 の絵が 1 枚遅れて並び、動きのある所で差が 8〜24 跳ねる

YMM4 が読み込みで断った設定（列挙型の名前の間違いなど）は、ダイアログが別の窓に
隠れて見えないことがある そのときは YMM4 を前に出して Ctrl+C を押すと、
エラーの文面がクリップボードに入る

### AviUtl 側も同じやり方で確かめる

- `tools/aviutl_count.py` … 手元のエイリアスを全部通して、写せない所を回数つきで数える
  見に行くのは `%PROGRAMDATA%\aviutl2\Alias` と `tests/fixtures/aviutl`

```
.venv\Scripts\python.exe tools\aviutl_count.py
```

トラックバーの「移動方法」の書き方は、AviUtl2 に実際に作らせたエイリアスから読んだ
作り方は `tests/fixtures/aviutl/README.md` に書いてある 要点は
**オブジェクト設定でトラックバーの名前をクリックすると移動方法の一覧が出る**こと
（値の欄ではなく名前 値の左右の `-` と `◆` は増減のボタン）

### DLL の中身は書き直さず、実物を呼ぶ

配布スクリプトの中には、処理を DLL（中身が DLL の ``.mod2``）へ切り出している物がある
（テレビ字幕の ``TVSubtitle.scan`` は、文字の絵から行ごとの四角を探す） DLL の中身は
公開されておらず、説明書きから書き直すと必ず外れる 実際に呼んで測ったら、1 文字ずつ
ではなく 1 行ずつの四角で、「接着間隔」は上下の行の隙間に効いていた

- **利用者の AviUtl2 に入っている DLL をそのまま呼ぶ**（``compat/aviutl/native.py``）
  呼び方は AviUtl ExEdit2 Plugin SDK の ``module2.h`` に従う（MIT ライセンス）
- DLL は Sashimono と同じ権限で動く（Lua の閉じ込めの外） 読むのはスクリプトフォルダに
  本人が置いた物だけにし、設定で切れるようにしてある
- 配布物の DLL はリポジトリに入れない 試験は、Python で作った関数を C の関数として
  渡し、DLL と同じ作法で引数を読み結果を積ませて確かめる 実物での確認は、DLL が
  入っている機械でだけ走る試験（無ければ飛ぶ）に分ける

---

## 6. Git の使い方

### ブランチ

| | |
|---|---|
| `main` | 常に `tools/verify.py` が通る状態を保つ |
| `phase/*` | フェーズ単位の作業 例 `phase/p7-proxy` |
| `fix/*` | 不具合の修正 |

`main` へ直接 push しない

### PR はフェーズ単位

1 フェーズ = 1 PR 細かく切りすぎると全体像が見えず、大きすぎるとレビューが
成立しない **フェーズの完了条件を満たしたら PR を出す**

PR には必ず含める:

- 何をしたか、**なぜその作りにしたか**
- `tools/verify.py` の結果（テスト件数）
- 途中で見つけた不具合と、その直し方
- 実物で確かめた場合はその結果（「配布物 36 本すべて通した」など）

### AI のレビューを受ける

レビュー役は 4 つ どれも無料枠（Copilot は Pro の月の回数）で動かしているので、
回数が切れる役が必ず出る **PR を出したら全員に頼み、指摘を突き合わせる** 枠が
切れた役は飛ばしてよい

| レビュー役 | 頼み方 | 設定 |
|---|---|---|
| [CodeRabbit](https://coderabbit.ai/) | PR で `@coderabbitai review`（公開リポジトリでは自動で走らない） | `.coderabbit.yaml` |
| GitHub Copilot | `gh pr edit <番号> --add-reviewer @copilot`（自動にはしない 月の回数を守るため） | なし |
| Sourcery | `@sourcery-ai review` | Web の画面（Review Settings） 言語は日本語、`tests/fixtures/**` を外す |
| Qodo | `/agentic_review` | `.pr_agent.toml` |

#### 承認（Approved）の出し方

CodeRabbit と Sourcery は承認を出す 指摘が残っている間は「変更を求める」状態で、
全部片付くと承認に変わるので、**PR の一覧を見るだけで手が要るかどうかが分かる**

- CodeRabbit … `.coderabbit.yaml` の `reviews.request_changes_workflow: true`
  承認へ変わるのは「指摘が全部解決」「最新のコミットまで見た」とき
  **こちらの CI の結果は見ていない** CI が赤でも承認は出るので、承認を CI の代わりにしない
- Sourcery … Web の画面（Review Settings）の `Let Sourcery approve pull requests`
- Copilot の承認は**有効にしない** 既定のまま（指摘だけ） 承認 1 つで必須承認を
  満たせてしまい、門として弱くなるため
- Qodo の自動承認は指摘が 0 件のときだけ出る作りなので、今の進め方では出ない 使っていない
  （`Qodo review` の status はこれとは別物 Qodo が最新のコミットを見たかだけを見ており、
  指摘の数や承認は見ていない）

**承認はマージの門ではない**（`main` の必須承認は 0 のまま）
門は今までどおり CI 2 本・`Qodo review`・会話の解決

ただし **CodeRabbit の「変更を求める」は、必須承認が 0 でもマージを止める**
（PR #18 で確かめた `reviewDecision` が `CHANGES_REQUESTED` になり `BLOCKED` に変わる）
止まったときの外し方は 2 つ

1. 指摘を直すか返事をしてスレッドを解決し、`@coderabbitai review` で見直してもらう
   （これが普通の道 承認へ変わる 見るのは前回のレビューからの差分なので、
   PR 全体を見直してほしいときは `@coderabbitai full review`）
2. CodeRabbit の枠が切れて見直しが返ってこないときは、**管理者が GitHub の画面で
   その レビューを Dismiss する**（`main` の保護は管理者に強制していないので外せる）

`@coderabbitai approve` と `@coderabbitai resolve` は**使わない** 指摘を見ないまま
承認や解決ができてしまい、承認の表示と実際に見た範囲が食い違う

約束（コメントの書き方・コア層の依存・テストの書き方）は、どの役にも同じものを渡す
`.coderabbit.yaml` を直したら、`.pr_agent.toml` もそろえる

#### Qodo のレビューはマージの必須条件

Qodo はコメントを書くだけで GitHub のチェックを出さないので、
`.github/workflows/qodo-gate.yml` が Qodo のコメントを読み、代わりに `Qodo review` という
status を出す（判定は `tools/qodo_gate.py`） main の保護でこれを必須にしてある
**PR の先頭のコミットまで Qodo が見るまで、マージできない** 修正を push したら
`/agentic_review` で頼み直す 見たかどうかは、Qodo のコメントにそのコミットの SHA が
書かれているかだけで決める（時刻では決めない 手元で作れるうえ、別のブランチの push で
ずれるので、見ていないコミットを通す穴になる） 指摘が 1 件も無いと SHA が書かれない
ことがあるので、そのときも `/agentic_review` で頼み直す
PR の向き先（base）を変えたときは、変えたあとに書かれた Qodo のコメントだけを数える
（SHA は同じまま、比べる相手が変わって別の差分になるため） 変えたら頼み直す

気を付けること

- **Qodo は push のたびに自動で見直す** そのとき、指摘より先にまとめのコメントを書き換える
  ので、`Qodo review` が通った数十秒後に新しい指摘が届くことがある マージの前に、
  最後の push より後に届いた指摘が無いかを見る
- 同じ PR の判定が重なると、新しい実行が古い実行を取り消す（古い判定で新しい結果を
  上書きしないため） 取り消された実行はチェックの一覧に赤く残るが、判定の status は
  最後の実行が書くので気にしなくてよい
- **Git Bash から `/agentic_review` を書くときは `MSYS_NO_PATHCONV=1` を付ける** 付けないと
  パスの変換で `C:/Program Files/Git/agentic_review` と書き込まれ、Qodo に届かない

```bash
MSYS_NO_PATHCONV=1 gh pr comment <番号> --body "/agentic_review"
```

Qodo が止まった、無料枠が切れたなどで返事が来ないときは、マージが止まったままになる
その場合だけ、理由を書いて手で通す（管理者の操作 何を確かめたかを PR に残す）

```
gh api repos/kagemorikosame/sashimono-edit/statuses/<先頭のコミットの SHA> -f state=success -f context="Qodo review" -f description="手で通した: <理由>"
```

Gemini Code Assist（GitHub の PR レビュー）は使わない GitHub 向けの無料 consumer version は
2026-07-17 に提供を終え、GitHub のレビュー機能で残っているのは Google Cloud の有料契約が要る
enterprise 版だけのため

- **指摘は読んで判断する** 機械的に全部直すのでも、全部無視するのでもない
- 直さないときは、その理由を PR のコメントに残す
- **同じ指摘が何役からも来る** 直すのは 1 回で、どのスレッドにも同じコミットを示して返す
- 役どうしで言うことが食い違ったら、どちらを採ったかと理由を PR に残す

### コミットメッセージ

1 行目に何をしたか、空行を挟んで**なぜそうしたか** 日本語で書く

```
YMM4 互換を、実配布の .ymmt に合わせて書き直す

推測で書いていた読み手では 1 本も読めなかった ZIP を素の JSON として開いて
1 バイト目で落ちていた

実物と違っていたのは 4 点
- .ymmt は ZIP 中の catalog.json が本体
...
```

---

## 7. リリース

正式リリースの条件と残っている課題は
[Issues](https://github.com/kagemorikosame/sashimono-edit/issues) と
[マイルストーン](https://github.com/kagemorikosame/sashimono-edit/milestones)で追う

いまは **β 版** 作りが大きく変わることがある 互換の穴（AviUtl / YMM4 で
まだ再現できていないもの）は、実際に呼ばれた回数つきで Issue に出す
数の多い順に埋める方針なので、回数が書かれていないと優先順位が付けられない

### 配る zip を作る

```
.venv\Scripts\python.exe tools\build_package.py
```

`dist\SashimonoEdit-<版>-windows-x64.zip` ができる 最後に**その zip を別の場所へ展開し、
中の exe で `--self-check` を走らせる**ところまでが 1 回の作業

- 組み立てた直後のフォルダで確かめない 開発機の Python や DLL を拾って通ってしまう
  配るのは zip なので、zip から確かめる 確かめるときの `PATH` も Windows の分だけにする
- `--self-check` は画面を出さずに、GL で描く・書き出す・Lua を走らせる・pip が
  あるか、を 1 行ずつ出す 起動しただけでは積み忘れは分からない
  （読み込みは遅延で、呼んだ時点で初めて DLL を探しに行く）
- 使う人の機械で動かないと言われたら、zip の中の `README.txt` にあるとおり
  `Sashimono.exe --self-check | more` の結果を貼ってもらう
- 字幕起こしと AI 連携は積まない（合わせて 2 GB を超える） 開発機に入っていても
  拾わないよう、`tools/build_package.py` の `EXCLUDED_MODULES` で外している
- **名前で読み込む部品**（lupa の Lua の実体、pip）は PyInstaller が辿れない
  組み立ては通ってしまうので、`COLLECTED_PACKAGES` でまとめて積む
  足すときは `--self-check` にその部品を実際に動かす項目も足す

依存が何も入っていない機械（VC++ ランタイムなど Windows 側の部品も無い）での
確認は、開発機ではできない 配る前に 1 度、別の機械かクリーンな環境で確かめる

### 使用許諾（配る zip は GPL の条件で配る）

Sashimono 本体のソースは MIT PyAV の wheel に入っている FFmpeg は組み込みの表記が
「LGPL version 3 or later」だが、同じ wheel に GPL の libx264 と libx265 が入っていて、
CPU での書き出しは libx264 を使う **zip は全体として GPL の条件で配る** と決めた
（Issue #32） 本体の MIT は GPL と両立するので、ソースは MIT のまま

- 部品ごとの使用許諾とソースの入手先は `THIRD_PARTY_NOTICES.md` に手で書く
  zip には `THIRD_PARTY_NOTICES.txt` として入る
- GNU の使用許諾の全文は `licenses/` に置く（どの包みも dist-info に持っていないため）
- PyAV の wheel の DLL（FFmpeg・x264・x265 ほか）と LuaJIT も写しを持っていないので、
  上流から積んだのと同じ版の写しを取ってきて `licenses/<部品>-<版>/` に置いてある
  版と取得元は `THIRD_PARTY_NOTICES.md` の表に書く PyAV を上げたら、pyav-ffmpeg の
  組み立て設定（`scripts/pkg.py`）で版を確かめて取り直す
- Python の包みの使用許諾は、組み立てのたびに dist-info から `licenses\<名前>-<版>\` へ写す
  **包みの名前は決め打ちにしない** 組み立ての記録（PyInstaller の TOC）から、積んだ
  ファイル 1 つずつの出どころを辿って数える PyInstaller は入っていれば拾うので、組み立てる
  機械が変わると積む包みも変わる
- 次のどれかがあると、zip を作る前に止まる
  - 出どころの分からないファイル（どの包みでも、Sashimono のソースでも、Python 本体でもない）
  - 使用許諾の写しが見つからない包み（写しを持たない包みは `WITHOUT_LICENSE_FILES` で名指しする
    その前に `THIRD_PARTY_NOTICES.md` へ書く）
  - `THIRD_PARTY_NOTICES.md` の一覧に無い包み
- 使わない Qt の部品（PDF を絵として読むプラグインと Qt6Pdf、仮想キーボードと
  それが読む Qt6Quick・Qt6Qml）は組み立てたあとに外す（`UNUSED_QT_PARTS`） 外した物を
  残った DLL が読んでいないかは、組み立てのたびに DLL の import 表で確かめる
  外すと zip が 8 MB 小さくなり、qtwebengine（580 MB）などのソースを添付しなくてよくなる
- wheel の中の DLL（`av.libs`）と LuaJIT は、`NATIVE_LICENSES` でリポジトリの写しと
  結び付ける 表に無い DLL が増えたら止まる
- 包みの版は一覧の表の版と突き合わせる 依存は下限だけで指定しているので、新しい版が
  入ったら一覧を直すまで zip を作らない
- `--skip-build` でも、組み立ての記録に無いファイル（手で足した DLL など）がフォルダに
  あれば止まる
- zip から確かめる段では、使用許諾の一覧と写しが組み立てたときと同じ中身か（sha256）を見る
- 組み立てる間は `PATH` を Windows の分にする 開発機の `PATH` にある
  Git for Windows の OpenSSL が、Qt の通信部品のためとして積まれていた
- zip から確かめる段でも、一覧と全文が入っているかを見る
- Qt は LGPL-3.0 で使う onedir で組み立てるので Qt の DLL は別のファイルのまま残り、
  使う人が差し替えられる 1 つの exe へまとめる形（onefile）に変えるときは、これを考え直す

### リリースにソースを添付する

GPL と LGPL の部品（FFmpeg・x264・x265・LAME・libiconv・Qt・PySide6）は、配る側が
対応するソースを渡せる状態を保たなければならない 上流の置き場は消えたり移ったりするので、
**zip と同じ GitHub Release に、積んだのと同じ版のソースを添付する**

```
.venv\Scripts\python.exe tools\collect_sources.py --check
.venv\Scripts\python.exe tools\collect_sources.py
gh release upload <タグ> dist\SashimonoEdit-<版>-windows-x64.zip dist\sources\*
```

- `--check` は入手先に届くかだけを見る（中身は落とさない） 落とすと 100 MB ほど
- Qt のソースは、組み立てたフォルダ（`dist\Sashimono`）に積んだ Qt のファイルが属する
  モジュールの分だけ落とす（`QT_FILE_MODULES`） 先に `tools/build_package.py` を走らせる
  どのモジュールの物か分からない Qt のファイルがあれば止まる
- 落とすと `dist\sources` にアーカイブと `sources-SHA256SUMS.txt` `sources-manifest.json`
  （取得元・版・sha256）ができる 上流が sha256 を公開している物は照合し、合わなければ止まる
- 版は `tools/collect_sources.py` に手で書いてある 入っている PyAV（の FFmpeg）と PySide6 の
  版と食い違うと落とす前に止まる 上げたら一覧と `THIRD_PARTY_NOTICES.md` を一緒に直す
- x264 の本家（code.videolan.org）は道具からの取得をボット避けの画面で断るので、同じ
  コミットを持つ GitHub の写しから落とす

### 色の範囲

扱うのは **SDR の sRGB / Rec.709 だけ** HDR と広色域は今後の課題（Issue #32 で決めた）
新しく色を扱う処理を足すときも、この範囲を前提にしてよい 範囲の外の素材を正しく
扱うふりはしない

### 自動更新の署名鍵

自動更新（Issue #36）は β の間は保留 署名鍵も β の間は作らない
作るときは **GitHub Actions の Secrets にだけ置き、組み立てる手元の機械には置かない**
（Issue #32 で決めた） 手元に置くと、その機械が乗っ取られたときに偽の更新へ署名できる
署名は Actions の中で行い、鍵をファイルとして書き出さない
