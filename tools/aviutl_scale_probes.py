r"""拡大率とクリッピングを AviUtl2 に描かせて、置かれた大きさを画素で測る（Issue #167）

    .venv\Scripts\python.exe tools\aviutl_scale_probes.py --work .work\aviutl-scale build
    .venv\Scripts\python.exe tools\aviutl2_export.py --project .work\aviutl-scale\compare.aup2 ^
        --output .work\aviutl-scale\aviutl --aviutl2 J:\aviutl2_v2.1.6a\aviutl2.exe
    .venv\Scripts\python.exe tools\aviutl_scale_probes.py --work .work\aviutl-scale measure

YMM4 の読み込みは拡大率を ``scale`` と ``scale_y`` の両方へ入れ、縦にだけ 2 回掛けていた
（#166） AviUtl の読み込みも同じ入れ方をしていたので、拡大率 100 以外の白い四角を
AviUtl2 に描かせ、白の外形の幅と高さを Sashimono の絵と並べて測る

クリッピングは #174 で「絵の置かれた範囲の端から数える」に直した 画面より小さい図形を
切って、切れた後の外形の上下左右の端がどこへ来るかを測る 上下左右に違う量を入れるのは、
向きを取り違えたときに見分けるため

見本は合成の物だけ（白の図形と、その場で作る単色の画像） 配布物は使わないので、
測った数は試験の定数へそのまま写せる 見本の置き方は ``tools/aviutl_compare.py`` の
``build`` と同じ
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import aviutl_compare  # noqa: E402

#: 見本の長さ（フレーム） 動かないので短くてよい
LENGTH = 6
#: 外形に数える明るさ 白の図形の縁のにじみの半分で切る
LIT = 128
#: 画像の見本の大きさ 縦横で違う数にして、縦と横の取り違えを見分ける
IMAGE_SIZE = (160, 90)


#: フィルタ 1 つ ``(効果の名前, (項目, 値) の並び)``
Filter = tuple[str, tuple[tuple[str, str], ...]]


@dataclass(frozen=True)
class Probe:
    """見本 1 本 ``filters`` はフィルタを掛ける順に並べた物"""

    name: str
    filters: tuple[Filter, ...] = ()
    zoom: str = "100.000"
    aspect: str = "0.000"
    image: bool = False
    size: int = 200


def _zoom(rate: str, x: str = "100.000", y: str = "100.000") -> Filter:
    return ("拡大率", (("拡大率", rate), ("X", x), ("Y", y)))


def _resize(rate: str, x: str = "100.000", y: str = "100.000") -> Filter:
    return (
        "リサイズ",
        (("拡大率", rate), ("X", x), ("Y", y), ("補間なし", "0"), ("ピクセル数でサイズ指定", "0")),
    )


def _clip(top: int, bottom: int, left: int, right: int, centre: int = 0) -> Filter:
    return (
        "クリッピング",
        (
            ("上", str(top)),
            ("下", str(bottom)),
            ("左", str(left)),
            ("右", str(right)),
            ("中心の位置を変更", str(centre)),
        ),
    )


#: 何を測る見本か 名前の頭は並べる順
PROBES: tuple[Probe, ...] = (
    # 拡大率 100 の白い四角 ほかの見本の大きさの基準（サイズ 200 なら 200x200 のはず）
    Probe("sc01_base"),
    # 描画設定の拡大率 縦に 2 回掛かっていれば 400x800 になる
    Probe("sc02_draw_200", zoom="200.000"),
    Probe("sc03_draw_50", zoom="50.000"),
    # 描画設定の縦横比 正で横が縮むのか縦が縮むのか
    Probe("sc04_aspect_50", aspect="50.000"),
    Probe("sc05_aspect_minus_50", aspect="-50.000"),
    Probe("sc06_draw_200_aspect_50", zoom="200.000", aspect="50.000"),
    # 拡大率のフィルタ 拡大率と、縦横別の X Y
    Probe("sc07_zoom_filter_200", filters=(_zoom("200.000"),)),
    Probe("sc08_zoom_filter_xy", filters=(_zoom("100.000", "200.000", "50.000"),)),
    Probe("sc09_zoom_filter_150_xy", filters=(_zoom("150.000", "200.000", "50.000"),)),
    # リサイズ 拡大率と縦横別
    Probe("sc10_resize_200", filters=(_resize("200.000"),)),
    Probe("sc11_resize_xy", filters=(_resize("100.000", "50.000", "150.000"),)),
    # 画像ファイル（縦横の違う単色の絵）の拡大率 図形と同じ掛かり方か
    Probe("sc12_image_draw_200", zoom="200.000", image=True),
    # クリッピング 画面より小さい図形（300x300）の上下左右を違う量で切る
    Probe("cr01_clip", filters=(_clip(10, 40, 20, 80),), size=300),
    # 中心の位置を変更 切った後に残りが動くか
    Probe("cr02_clip_centre", filters=(_clip(10, 40, 20, 80, 1),), size=300),
    # 切ってから拡大率 200 切る量は拡大前の画素で数えるか
    Probe("cr03_clip_draw_200", filters=(_clip(10, 40, 20, 80),), size=300, zoom="200.000"),
    # 画像ファイルを切る
    Probe("cr04_clip_image", filters=(_clip(10, 20, 30, 0),), image=True),
)


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """単色の PNG Pillow を足さずに書く（``tools/aviutl_tag_probes.py`` の青の絵と同じ作り）"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    pixels = zlib.compress(row * height)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def probe_text(probe: Probe, image: Path) -> str:
    """見本 1 本の ``.object`` の中身

    描画設定（標準描画）は最後に置く AviUtl2 が書いたエイリアス（``ProgramData\\aviutl2\\Alias``
    の字幕テンプレート）と同じ並び
    """
    lines = ["[Object]", f"frame=0,{LENGTH - 1}", "[Object.0]"]
    if probe.image:
        lines += ["effect.name=画像ファイル", f"ファイル={image}"]
    else:
        lines += [
            "effect.name=図形",
            "図形の種類=四角形",
            f"サイズ={probe.size}",
            "縦横比=0.00",
            "ライン幅=4000",
            "色=ffffff",
            "角を丸くする=0",
        ]
    number = 1
    for name, items in probe.filters:
        lines += [f"[Object.{number}]", f"effect.name={name}"]
        lines += [f"{key}={value}" for key, value in items]
        number += 1
    lines += [
        f"[Object.{number}]",
        "effect.name=標準描画",
        "X=0.00",
        "Y=0.00",
        "Z=0.00",
        "Group=1",
        "中心X=0.00",
        "中心Y=0.00",
        "中心Z=0.00",
        "Group3=1",
        "X軸回転=0.00",
        "Y軸回転=0.00",
        "Z軸回転=0.00",
        "Group2=1",
        f"拡大率={probe.zoom}",
        f"縦横比={probe.aspect}",
        "透明度=0.00",
        "合成モード=通常",
    ]
    # AviUtl2 が書いたエイリアスと同じ CRLF にする 読み手の違いを混ぜない
    return "\r\n".join(lines) + "\r\n"


def write_probes(work: Path) -> list[Path]:
    """見本と画像を作業フォルダへ書き、見本の道を並べる順に返す"""
    folder = work / "probes"
    folder.mkdir(parents=True, exist_ok=True)
    image = (folder / "white.png").resolve()
    image.write_bytes(solid_png(*IMAGE_SIZE, (255, 255, 255)))
    written: list[Path] = []
    for probe in PROBES:
        path = folder / f"{probe.name}.object"
        path.write_bytes(probe_text(probe, image).encode("utf-8"))
        written.append(path)
    return written


Box = tuple[int, int, int, int]


def lit_box(image: np.ndarray) -> Box | None:
    """明るい画素の外形 ``(左, 上, 右, 下)`` 右と下は外形の 1 つ外 何も無ければ ``None``"""
    lit = image[..., :3].max(axis=2) >= LIT
    rows = np.nonzero(lit.any(axis=1))[0]
    columns = np.nonzero(lit.any(axis=0))[0]
    if rows.size == 0 or columns.size == 0:
        return None
    return int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1


def describe(box: Box | None) -> str:
    if box is None:
        return "（何も無い）"
    left, top, right, bottom = box
    return f"{right - left}x{bottom - top} 左 {left} 上 {top} 右 {right} 下 {bottom}"


def command_build(work: Path) -> int:
    files = write_probes(work)
    return aviutl_compare.command_build(
        argparse.Namespace(work=work, files=[str(p) for p in files])
    )


def command_measure(work: Path) -> int:
    """AviUtl2 の書き出しと Sashimono の絵で、見本ごとの白の外形を並べる"""
    from sashimono.compat.aviutl.exo import load_exo
    from sashimono.compat.aviutl.mapping import map_object
    from sashimono.compat.aviutl.report import CompatibilityReport
    from sashimono.core.model import ProjectSettings
    from sashimono.core.timebase import FrameRate
    from sashimono.engine.render import FrameRenderer

    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    cases = [aviutl_compare.Case(**raw) for raw in manifest["cases"]]
    # 真ん中のフレームだけを見る 動かない見本なので、どのフレームでも同じはず
    frames = {case.name: case.sample_frames()[1] for case in cases}
    references = aviutl_compare.reference_frames(work, set(frames.values()))
    if references is None:
        print(f"{work / aviutl_compare.PNG_FOLDER} に書き出しがありません 先に書き出してください")
        return 1
    settings = ProjectSettings(
        width=aviutl_compare.WIDTH,
        height=aviutl_compare.HEIGHT,
        frame_rate=FrameRate(aviutl_compare.FPS),
        sample_rate=aviutl_compare.AUDIO_RATE,
    )
    report = CompatibilityReport()
    rows: list[dict[str, object]] = []
    renderer: FrameRenderer | None = None
    try:
        for case in cases:
            frame = frames[case.name]
            reference = references.get(frame)
            if reference is None:
                print(f"{case.name}: 書き出しにフレーム {frame} がありません")
                return 1
            objects = [
                item
                for obj in load_exo(Path(case.source)).objects
                if (item := map_object(obj, settings.frame_rate, report=report)) is not None
            ]
            project, missing = aviutl_compare.placed_project(objects, settings, case.start)
            if missing:
                # 見本の画像はその場で作る 無ければ作業フォルダが壊れているので測らない
                print(aviutl_compare.lacking_note(case.name, missing))
                return 1
            if renderer is None:
                renderer = FrameRenderer(project)
            else:
                renderer.set_project(project)
            ours = np.asarray(renderer.render(frame))
            theirs, mine = lit_box(reference), lit_box(ours)
            print(f"{case.name}")
            print(f"  AviUtl2   {describe(theirs)}")
            print(f"  Sashimono {describe(mine)}")
            rows.append({"name": case.name, "aviutl": theirs, "sashimono": mine})
    finally:
        if renderer is not None:
            renderer.close()
    (work / "measure.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
    for line in report.lines():
        print(f"  記録: {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, required=True, help="作業フォルダ")
    parser.add_argument("command", choices=("build", "measure"))
    arguments = parser.parse_args(argv)
    # 丸ごとの道にする 相対のまま渡すと、プロジェクトに書く自分の道（``file=``）が相対になる
    work: Path = arguments.work.resolve()
    if arguments.command == "build":
        return command_build(work)
    return command_measure(work)


if __name__ == "__main__":
    raise SystemExit(main())
