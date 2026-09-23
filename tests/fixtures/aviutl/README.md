# AviUtl のエイリアス置き場

ここに `.object`（AviUtl2）を置くと `tests/compat/test_real_aliases.py` が、
`aviutl1/` に AviUtl1 世代の配布物を置くと `tests/compat/test_real_aviutl1_exa.py` が
拾って読み込みを検査する 置かなければ、その検査は飛ばされる

**配布物そのものはリポジトリに入れていない** 作者ごとに再配布の条件が違うため
手元で試すときは、配布元からダウンロードしてここへ置く

`tools/aviutl_count.py` は、ここと `%PROGRAMDATA%\aviutl2\Alias` の両方を読んで
写せない所を回数つきで数える

## AviUtl1 の配布物（`aviutl1/`）

`.exa` の読み方は、次の 4 つの配布物から読み取った どれも GitHub で公開されていて、
使用許諾は MIT か Unlicense 取ってきたものを、リポジトリごとのフォルダのまま
`aviutl1/` へ置く（`.exa` と、それが呼ぶ `.anm` `.obj` `.scn` `.lua`）

| 配布元 | 使用許諾 | `.exa` |
|---|---|---|
| https://github.com/sigma-axis/sigma_aviutl_scripts | MIT | 38 本（効果 30・図形 5・シーンチェンジ 4） |
| https://github.com/sigma-axis/AviUtl-Alias-FPS-Counter | Unlicense | 2 本 |
| https://github.com/sigma-axis/aviutl_localfont2 | MIT | 2 本 |
| https://github.com/oov/aviutl_psdtoolkit | MIT | 25 本（日本語版と英語版の組） |

`aviutl1/` はフォルダごと `.gitignore` に入れてある スクリプトの拡張子（`.anm` など）は
上の一覧に無いので、フォルダで外さないと中身が紛れ込む

分かったこと

- 節の名前は `[vo]` `[vo.0]`（映像）、`[ao]` `[ao.0]`（音声）、`[v.0]`（シーンチェンジだけ）
  区間は `[vo]` の `length=` だけで、効果だけのエイリアスは `[vo]` 自体を持たない
- 文字コードは cp932 英語版は要素と項目の名前が英語（`Standard drawing` `Zoom%` など）
- トラックバーの動きは `始点,終点,番号` 番号の意味を確かめられたのは 3（瞬間移動）だけ
- アニメーション効果は `name=表示名@ファイル名` と `track0..3` `check0`、`--dialog` の値は
  `param=_1=…;_0=nil;` と Lua の書き方のまま入る
- 67 本に中間点を持つものは無い

## 移動方法の試験ファイルの作り方

`probes/` に置いてあるのは、AviUtl2（v2.1.6a）に作らせた「移動方法」の見本
トラックバーの書き方はここから読み取った 作り直すときの手順

1. AviUtl2 でレイヤーを右クリック → メディアオブジェクトの追加 → 適当なエイリアス
2. オブジェクト設定でトラックバーの**名前をクリック**すると移動方法の一覧が出る
   （値の欄ではなく名前 値の左右の `-` と `◆` は増減のボタン）
3. 直線移動・補間移動・瞬間移動・ランダム移動・反復移動・回転・移動量指定・
   参照式などを別々の項目に割り当て、終わりの値も変えておく
4. タイムラインでオブジェクトを右クリック → 中間点を追加（値が 3 つ並ぶ形になる）
5. もう一度右クリック → エイリアスを作成 名前を付けると
   `%PROGRAMDATA%\aviutl2\Alias` に `.object` として出る
6. そのファイルをここへ移す（AviUtl2 側からは消しておく）

読み取った文法は `src/sashimono/compat/aviutl/motion.py` の説明に書いてある
