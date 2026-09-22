# 脆弱性の報告について

## 報告の仕方

**公開の Issue には書かないでください** 直す前に攻撃の手がかりが公開されます

[Security → Report a vulnerability](https://github.com/kagemorikosame/sashimono-edit/security/advisories/new)
から非公開で報告できます

書いてもらえると助かること:

- どうすれば再現するか
- 何ができてしまうか（ファイルが読める、任意のコードが動く、など）
- Sashimono Edit の版

単独で開発しているため、返事に数日かかることがあります

## このソフトで特に気にしている場所

Sashimono は**他人が作ったファイルを読み込んで実行する**部分を持っています
そこが主な攻撃面です

| 場所 | 何を読むか | どう守っているか |
|---|---|---|
| `src/sashimono/compat/aviutl/runtime.py` | 配布されている Lua スクリプト | `io` `os` `require` `dofile` を外し、モジュールの探索先をスクリプトフォルダ内に限る 実行の命令数にも上限 |
| `src/sashimono/compat/` | `.exo` `.exa` `.object` `.ymmt` | 解釈するだけで、ファイルもプロセスも開かない |
| `src/sashimono/ai/` | AI エージェントの指示 | 組み込みツールを全部止め（`tools=[]`）、自前の MCP ツール以外を `can_use_tool` でも拒否 |
| `src/sashimono/runtime.py` | `pip` で入れる実行環境 | 導入するものと実行するコマンドを、実行前に画面へ出す |

**AviUtl のスクリプトは読み込んだだけで走ります** ここの制限を緩める変更は、
緩める理由と、緩めても安全な根拠を PR に書いてください

## 対象外

- 使う人が自分で入れたスクリプト・プラグインが、その人の環境で行うこと
- 依存ライブラリ自体の脆弱性（見つけた場合は、そのライブラリへ報告してください）
