r"""テキストの制御文字（``<@書体>`` ``<#色>`` ``<s大きさ>``）を AviUtl2 に描かせる見本を作る

    .venv\Scripts\python.exe tools\aviutl_tag_probes.py --work .work\composite-font
    .venv\Scripts\python.exe tools\aviutl2_export.py --project .work\composite-font\compare.aup2 ^
        --output .work\composite-font\aviutl
    .venv\Scripts\python.exe tools\aviutl_compare.py --work .work\composite-font compare

合成フォント（``comfont.aux2``）は本文を書体の切り替えの制御文字 ``<@書体名>…<@>`` へ
書き換える ``aviutl2.txt`` には書き方しか無く、**書体や大きさが混ざった行の高さと
並びの基準線**は書かれていない 推測で組まずに AviUtl2 に描かせて測るための見本

見本は作業フォルダの ``probes`` へ書き、``tools/aviutl_compare.py`` の ``build`` と同じ
並べ方でプロジェクトを作る 配布エイリアスの ``合成フォントテキスト`` が手元にあれば
一緒に並べる（プロファイルを置いて書き出せば、組み替えた本文まで同じ絵で比べられる）

どの見本も、文字の枠を青で塗る（画像合成の後方から合成） 字の形だけでは、行の高さと
枠の上下の余白を分けて読めない 青の絵は作業フォルダに作るので、リポジトリには
利用者の置き場の道が入らない
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import aviutl_compare  # noqa: E402

#: 見本の長さ（フレーム） 動かない文字なので短くてよい 書き出す絵の枚数を抑える
LENGTH = 6

#: 配布エイリアスの名前 手元に無ければ並べない
DISTRIBUTED_ALIAS = "合成フォントテキスト.object"


@dataclass(frozen=True)
class Probe:
    """見本 1 本 ``text`` の改行は AviUtl2 の書き方（``\\n`` の 2 文字）で持つ"""

    name: str
    text: str
    font: str = "Arial"
    size: float = 100.0
    align: str = "中央揃え[中]"
    decoration: str = "標準文字"
    edge: str = "000000"


#: 何を測る見本か 名前の頭の数字は並べる順
PROBES: tuple[Probe, ...] = (
    # 書体を変えない 3 行 ほかの見本の行の高さの基準（Arial 100 で 115 前後のはず）
    Probe("tag01_base_lines", r"HH\nHH\nHH"),
    # 1 行の中で書体が混ざったときの基準線と枠の高さ H の下端は基準線に乗る
    Probe("tag02_font_mix", "H<@Meiryo>H<@><@MS Gothic>H<@><@Yu Mincho>H<@>H"),
    # 行ごとに書体が違うときの行の高さ メイリオは行送りが大きい
    Probe("tag03_font_lines", r"<@Meiryo>HH<@>\nHH\n<@MS Gothic>HH<@>"),
    # 書体が 1 文字だけ混ざった行と混ざらない行 混ざった行だけ高くなるか
    Probe("tag04_font_one_char", r"H<@Meiryo>H<@>\nHH\nHH"),
    # 入れ子 ``<@>`` は 1 つ前の書体へ戻るのか、既定の書体へ戻るのか
    Probe("tag05_font_nest", "H<@Meiryo>H<@MS Gothic>H<@>H<@>H"),
    # 改行をまたぐ書体 行が変わっても書体が続くか
    Probe("tag06_font_across_lines", r"H<@Meiryo>H\nHH<@>H"),
    # 日本語の書体どうし 基準線で揃うのか、字の枠の中心で揃うのか
    Probe(
        "tag07_font_kanji",
        "田<@Meiryo>田<@><@MS Gothic>田<@><@Yu Mincho>田<@>",
        font="MS UI Gothic",
    ),
    # 色 文字色だけ・文字色と縁の色・元へ戻す
    Probe(
        "tag08_color",
        "H<#ff0000>H<#00ff00,0000ff>H<#>H",
        decoration="縁取り文字",
        edge="808080",
    ),
    # 大きさ 絶対・元へ戻す・倍・足す 基準線と枠の高さ
    Probe("tag09_size_mix", "H<s50>H<s>H<s*2>H<s+20>H"),
    # 大きい文字の行と普通の行 行の高さはその行の大きさで決まるか 改行で戻るか
    Probe("tag10_size_lines", r"H<s200>H\nHH\n<s>HH"),
    # ``<s>`` の書体の指定
    Probe("tag11_size_font", "H<s80,Meiryo>H<s>H"),
    # 左寄せ[上] で行の幅が違うとき 枠と行の揃え方
    Probe("tag12_left_top", r"<@Meiryo>HH<@>\nH<s50>H", align="左寄せ[上]"),
    # 合成フォントが返す形そのもの（既定の書体 大きさ 64） 配布エイリアスが無い機械でも比べられる
    Probe(
        "tag13_comfont_literal",
        "<@Yu Mincho>ここに<@><@Meiryo>テキスト<@><@Yu Mincho>を<@><@MS Gothic>入力<@>",
        font="",
        size=64.0,
    ),
    # ここから下は日本語の書体名 上の見本の英語の名前（Meiryo など）は AviUtl2 v2.1.6a が
    # 見つけられず、どれも既定の Yu Gothic UI で描いた AviUtl2 の書体の一覧の名前で測り直す
    Probe("tag14_jp_font_mix", "H<@メイリオ>H<@><@ＭＳ ゴシック>H<@><@游明朝>H<@>H"),
    Probe("tag15_jp_font_lines", r"<@メイリオ>HH<@>\nHH\n<@ＭＳ ゴシック>HH<@>"),
    Probe(
        "tag16_jp_font_kanji",
        "田<@メイリオ>田<@><@ＭＳ ゴシック>田<@><@游明朝>田<@>",
        font="MS UI Gothic",
    ),
    # 上の高さが大きい書体（Yu Gothic UI 108）と下の深さが大きい書体（メイリオ 90 で 39.6）
    # 行の高さが「上の高さの最大 + 下の深さの最大」か「行送りの最大」かで 12 画素ほど違う
    Probe("tag17_ascent_descent", r"H<s90,メイリオ>H<s>H\nHH", font="Yu Gothic UI"),
    Probe(
        "tag18_jp_comfont_literal",
        "<@游明朝>ここに<@><@メイリオ>テキスト<@><@游明朝>を<@><@ＭＳ ゴシック>入力<@>",
        font="",
        size=64.0,
    ),
    # 設定欄の書体を英語の名前で書いたとき 制御文字と同じく見つけられないか
    Probe("tag19_object_font_english", r"HH\nHH", font="Meiryo"),
    Probe("tag20_object_font_japanese", r"HH\nHH", font="メイリオ"),
    # 文字装飾の切り替え（記録のため 合成フォントは使わない）
    Probe("tag21_font_decoration", "H<@メイリオ,3>H<@>H", edge="ff0000"),
    # 大きさだけを変えた空の行の高さ
    Probe("tag22_empty_line", r"H\n<s200>\nH"),
    # 色の名前（記録のため）
    Probe("tag23_color_name", "H<#red>H<#>H"),
)


def probe_text(probe: Probe, frame_image: Path) -> str:
    """見本 1 本の ``.object`` の中身

    項目は AviUtl2 v2.1.6a に作らせた見本（``kumiki_p8_t_*``）の形
    """
    lines = [
        "[Object]",
        f"frame=0,{LENGTH - 1}",
        "[Object.0]",
        "effect.name=テキスト",
        f"サイズ={probe.size:.2f}",
        "字間=0.00",
        "行間=0.00",
        "表示速度=0.00",
        f"フォント={probe.font}",
        "文字色=ffffff",
        f"影・縁色={probe.edge}",
        f"文字装飾={probe.decoration}",
        f"文字揃え={probe.align}",
        "B=0",
        "I=0",
        f"テキスト={probe.text}",
        "文字毎に個別オブジェクト=0",
        "自動スクロール=0",
        "移動座標上に表示=0",
        "オブジェクトの長さを自動調節=0",
        # 文字の枠を青で塗る 後方から合成なので文字の後ろに入り、枠の外へは出ない
        "[Object.1]",
        "effect.name=画像合成",
        "X=0.0",
        "Y=0.0",
        "Group=1",
        "拡大率=100.00",
        "合成モード=後方から合成",
        f"画像={frame_image}",
        "ループ画像=1",
        "ループ再生=1",
    ]
    # AviUtl2 が書いたエイリアスと同じ CRLF にする 読み手の違いを混ぜない
    return "\r\n".join(lines) + "\r\n"


def blue_png() -> bytes:
    """1 画素の青の PNG 枠を塗るだけなので大きさは要らない（ループ画像で敷き詰める）"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x00\x00\xff")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def write_probes(work: Path) -> list[Path]:
    """見本と青の絵を作業フォルダへ書き、見本の道を並べる順に返す"""
    folder = work / "probes"
    folder.mkdir(parents=True, exist_ok=True)
    image = (folder / "blue.png").resolve()
    image.write_bytes(blue_png())
    written: list[Path] = []
    for probe in PROBES:
        path = folder / f"{probe.name}.object"
        path.write_bytes(probe_text(probe, image).encode("utf-8"))
        written.append(path)
    return written


def distributed_alias() -> Path | None:
    """配布エイリアスの ``合成フォントテキスト`` 手元の AviUtl2 に無ければ ``None``"""
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        return None
    path = Path(program_data) / "aviutl2" / "Alias" / DISTRIBUTED_ALIAS
    return path if path.is_file() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, required=True, help="作業フォルダ")
    arguments = parser.parse_args()
    # 丸ごとの道にする 相対のまま渡すと、プロジェクトに書く自分の道（``file=``）が相対になり、
    # AviUtl2 を別の置き場から起こしたときに保存し直す先が食い違う
    work: Path = arguments.work.resolve()
    files = write_probes(work)
    alias = distributed_alias()
    if alias is not None:
        files.append(alias)
    else:
        print(f"{DISTRIBUTED_ALIAS} が無いので、見本だけを並べる")
    return aviutl_compare.command_build(
        argparse.Namespace(work=work, files=[str(p) for p in files])
    )


if __name__ == "__main__":
    raise SystemExit(main())
