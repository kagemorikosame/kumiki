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
    #: 設定欄の字間と行間 字の縁や影が隣の字とつながると、字ごとに区切って測れない
    letter_gap: float = 0.0
    line_gap: float = 0.0


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
    # ここから下は #134 の残り
    # ``<@書体>`` の後の ``<s>`` は設定欄の書体へ戻すのか、直前の ``<@書体>`` へ戻すのか
    Probe("tag24_font_then_size_reset", "H<@メイリオ>H<s50>H<s>H"),
    Probe("tag25_font_then_size_font_reset", "H<@メイリオ>H<s50,ＭＳ ゴシック>H<s>H"),
    # 文字装飾の番号 0〜6 縁と影の色は赤にして、どの字に何が付いたかを色で読む
    Probe("tag26_decoration_numbers", "H<@,1>H<@,2>H<@,3>H<@,4>H<@,5>H<@,6>H<@>H", edge="ff0000"),
    # 設定欄が縁取り文字のとき ``<@,0>`` で縁が消え ``<@>`` で戻るか
    Probe("tag27_decoration_off", "H<@,0>H<@>H", decoration="縁取り文字", edge="ff0000"),
    # スタイルの足し引き 太字と斜体
    Probe("tag28_style_plus_minus", "H<@+B>H<@-B>H<@+I>H<@>H"),
    # 文字装飾の番号の後のスタイル
    Probe("tag29_decoration_style", "H<@,3B>H<@>H", edge="ff0000"),
    # ``<s>`` のスタイルと縁取りの太さ
    Probe("tag30_size_style", "H<s,,B>H<s>H<s,,I>H<s>H"),
    Probe("tag31_size_edge", "H<s,,,12>H<s>H", decoration="縁取り文字", edge="ff0000"),
    # 字間と行間
    Probe("tag32_letter_gap", "HH<gw40>HH<gw>HH"),
    Probe("tag33_line_gap", r"H\n<gh40>H\nH<gh>\nH"),
    # 座標 相対と絶対と戻す
    Probe("tag34_position_relative", "H<p+100>H<p>H"),
    Probe("tag35_position_absolute", r"H<p300,100>H\nH"),
    # 文字の変形 横・縦のスケールと角度
    Probe("tag36_transform", "H<tw0.5>H<tw><th2>H<th><tr30>H<tr>H"),
    # コメントと区切り 字は出ないはず
    Probe("tag37_comment", "H<//コメント//>H</>H"),
    # ふりがな
    Probe("tag38_ruby", "H</>漢字<!>かんじ</>H", font="Yu Gothic UI"),
    # 表示速度・待ち・消す 見本の長さ（6 フレーム 60fps で 0.1 秒）の中で何文字出るか
    Probe("tag39_speed", "H<r5>HHH"),
    Probe("tag40_wait", "H<w1>H"),
    Probe("tag41_clear", "HH<c>H"),
    # 書体名を空にした ``<@,番号>`` は何も変えなかった（tag26 tag27 tag29） 書体名を書いて測り直す
    Probe(
        "tag42_decoration_named",
        "H<@Arial,1>H<@Arial,2>H<@Arial,3>H<@Arial,4>H<@Arial,5>H<@Arial,6>H<@>H",
        edge="ff0000",
        letter_gap=40.0,
    ),
    Probe(
        "tag43_decoration_off_named",
        "H<@Arial,0>H<@>H",
        decoration="縁取り文字",
        edge="ff0000",
        letter_gap=40.0,
    ),
    # 縁取りの太さ 0・4・8・20（tag31 の 12 は設定欄の縁より細かった）
    Probe(
        "tag44_edge_sizes",
        "H<s,,,0>H<s,,,4>H<s,,,8>H<s,,,20>H<s>H",
        decoration="縁取り文字",
        edge="ff0000",
        letter_gap=60.0,
    ),
    # ``<@>`` は ``<s>`` で指定した書体も戻すか 大きさは残るか
    Probe("tag45_at_reset_after_size_font", "H<s50,ＭＳ ゴシック>H<@>H"),
    Probe("tag46_at_reset_after_both", "H<@メイリオ>H<s80,ＭＳ ゴシック>H<@>H"),
    # 設定欄の字間・行間があるときの ``<gw>`` ``<gh>`` 足すのか置き換えるのか
    Probe("tag47_letter_gap_with_setting", "HH<gw40>HH<gw>HH", letter_gap=20.0),
    Probe("tag48_line_gap_with_setting", r"H\n<gh40>H\nH<gh>\nH", line_gap=20.0),
    # 縦のスケールと角度の中心
    Probe("tag49_tall_half", "H<th0.5>H<th>H"),
    Probe("tag50_turn", "L<tr45>L<tr>L", letter_gap=60.0),
    # 文字装飾の番号の後のスタイル（書体名つき）
    Probe("tag51_decoration_style_named", "H<@Arial,3B>H<@>H", edge="ff0000", letter_gap=40.0),
    # 相対の座標の縦と、改行をまたいだとき
    Probe("tag52_position_relative_lines", r"H<p+100,+50>H\nH<p>H"),
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
        f"字間={probe.letter_gap:.2f}",
        f"行間={probe.line_gap:.2f}",
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


def chosen(only: str) -> tuple[Probe, ...]:
    """``--only`` で選んだ見本 名前の頭で選ぶ（``tag42,tag5`` 空なら全部）

    足した見本だけを書き出せば、AviUtl2 を動かす長さが短く済む
    """
    heads = [head.strip() for head in only.split(",") if head.strip()]
    if not heads:
        return PROBES
    return tuple(probe for probe in PROBES if probe.name.startswith(tuple(heads)))


def write_probes(work: Path, probes: tuple[Probe, ...] = PROBES) -> list[Path]:
    """見本と青の絵を作業フォルダへ書き、見本の道を並べる順に返す"""
    folder = work / "probes"
    folder.mkdir(parents=True, exist_ok=True)
    image = (folder / "blue.png").resolve()
    image.write_bytes(blue_png())
    written: list[Path] = []
    for probe in probes:
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
    parser.add_argument(
        "--only", default="", help="並べる見本の名前の頭（``tag42,tag5`` のように , で区切る）"
    )
    arguments = parser.parse_args()
    # 丸ごとの道にする 相対のまま渡すと、プロジェクトに書く自分の道（``file=``）が相対になり、
    # AviUtl2 を別の置き場から起こしたときに保存し直す先が食い違う
    work: Path = arguments.work.resolve()
    probes = chosen(arguments.only)
    if not probes:
        print(f"--only {arguments.only} に当たる見本がありません")
        return 1
    files = write_probes(work, probes)
    # 選んだ見本だけを書き出すときは、配布エイリアスも並べない 並べると短く済まない
    alias = None if arguments.only else distributed_alias()
    if alias is not None:
        files.append(alias)
    elif not arguments.only:
        print(f"{DISTRIBUTED_ALIAS} が無いので、見本だけを並べる")
    return aviutl_compare.command_build(
        argparse.Namespace(work=work, files=[str(p) for p in files])
    )


if __name__ == "__main__":
    raise SystemExit(main())
