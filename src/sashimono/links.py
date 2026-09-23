"""ソフトの外へ案内する先（使い方・不具合の受け口）

案内する URL はここ 1 か所に集める メニュー・文言・配る zip の説明書きが
それぞれ URL を書くと、置き場を移したときに 1 つだけ古い先へ飛ばし続ける
（Wiki を公開したら「使い方」の先を差し替える 差し替える所が 1 つなら漏れない）

Qt を読まない 自己診断や配る zip を組み立てる道具からも使えるようにする
"""

from __future__ import annotations

__all__ = ["MANUAL_URL", "REPORT_URL", "REPOSITORY_URL"]

#: 公開リポジトリ
REPOSITORY_URL = "https://github.com/kagemorikosame/sashimono-edit"

#: 使い方 Wiki が公開されるまでは README の操作の節へ飛ばす
#: 公開前の Wiki へ飛ばすと、GitHub は「Wiki を作る」画面か空のページを出し、
#: 使う人には壊れたリンクにしか見えない 公開したら ``f"{REPOSITORY_URL}/wiki"`` へ替える
MANUAL_URL = f"{REPOSITORY_URL}#主な操作"

#: 不具合・要望の受け口 雛形を選ぶ画面へ直に飛ばす
#: 白紙の Issue へ飛ばすと、版や再現手順の欄の無い報告になり、聞き返しから始まる
REPORT_URL = f"{REPOSITORY_URL}/issues/new/choose"
