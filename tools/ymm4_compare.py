"""YMM4 本体の絵と Kumiki の絵を並べて比べる

YMM4 のテンプレートは、値の意味を配布物の並びから読み取って写している 読めて描ける
ことはテストで確かめられるが、**YMM4 と同じ絵になるか**は本体で描いてみないと分からない

使い方（3 段）

1. ``build``   テンプレートを時間をずらして並べた YMM4 のプロジェクト（.ymmp）と、
               どこに何を置いたかの一覧（manifest.json）を作る
2. YMM4 でそのプロジェクトを開き、同じフォルダへ ``ymm4.mp4`` として書き出す（手作業）
3. ``compare`` 書き出した動画と、同じアイテムを Kumiki で描いた絵を並べ、差の大きい順に
               一覧（report.html）と並べた絵（PNG）を作る

作業フォルダは既定で ``.work/ymm4-compare`` リポジトリには入れない
（配布物の絵が入るため）
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kumiki.compat.aviutl.report import CompatibilityReport  # noqa: E402
from kumiki.compat.catalog import place  # noqa: E402
from kumiki.compat.ymm4.template import load_template, map_template  # noqa: E402
from kumiki.compat.ymm4.values import number, type_name  # noqa: E402

WIDTH, HEIGHT, FPS = 1920, 1080, 30
#: 比べる絵の大きさ YMM4 の書き出しは圧縮されるので、縮めてならしてから比べる
COMPARE_WIDTH, COMPARE_HEIGHT = 480, 270
#: テンプレート 1 本あたりの最短の枠（フレーム）
MIN_SLOT = 60
#: 枠と枠の間に空ける黒 前のテンプレートの残りが次へ混ざらないように
GAP = 6
DEFAULT_WORK = ROOT / ".work" / "ymm4-compare"
#: 置いても比べられないアイテム 場面切り替えは前後の絵が要る
SKIPPED_ITEMS = frozenset({"AudioItem"})

_BRUSH_PARAMETER = "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushParameter, YukkuriMovieMaker"


def _still(value: float) -> dict[str, Any]:
    return {"Values": [{"Value": value}], "Span": 0.0, "AnimationType": "なし"}


def base_shape(frame: int, layer: int, length: int) -> dict[str, Any]:
    """エフェクトだけのテンプレートを着せる下地 角のある図形の方が縁や影の違いが見える"""
    return {
        "$type": "YukkuriMovieMaker.Project.Items.ShapeItem, YukkuriMovieMaker",
        "ShapeType2": "YukkuriMovieMaker.Shape.QuadrilateralShapePlugin, YukkuriMovieMaker",
        "ShapeParameter": {
            "$type": "YukkuriMovieMaker.Project.Items.RectangleShapeParameter, YukkuriMovieMaker",
            "Round": _still(0.0),
            "SizeMode": "WidthHeight",
            "Size": _still(300.0),
            "AspectRate": _still(0.0),
            "Width": _still(640.0),
            "Height": _still(360.0),
            "StrokeThickness": _still(10000.0),
            "Brush": {
                "Type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushPlugin, YukkuriMovieMaker",
                "Parameter": {
                    "$type": _BRUSH_PARAMETER,
                    "Color": "#FFE08A2C",
                },
            },
        },
        "X": _still(0.0),
        "Y": _still(0.0),
        "Z": _still(0.0),
        "Opacity": _still(100.0),
        "Zoom": _still(100.0),
        "Rotation": _still(0.0),
        "FadeIn": 0.0,
        "FadeOut": 0.0,
        "Blend": "Normal",
        "IsInverted": False,
        "IsClippingWithObjectAbove": False,
        "IsAlwaysOnTop": False,
        "IsZOrderEnabled": False,
        "VideoEffects": [],
        "Group": 0,
        "Frame": frame,
        "Layer": layer,
        "KeyFrames": {"Frames": [], "Count": 0},
        "Length": length,
        "PlaybackRate": 100.0,
        "ContentOffset": "00:00:00",
        "Remark": "比較の下地",
        "IsLocked": False,
        "IsHidden": False,
    }


@dataclass
class Case:
    """比べるテンプレート 1 本"""

    name: str
    file: str
    index: int
    start: int
    length: int
    items: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def sample_frames(self) -> list[int]:
        """比べるフレーム 入りと真ん中と終わりの手前"""
        last = self.length - 1
        picks = {min(2, last), last // 2, max(0, last - 3)}
        return sorted(self.start + offset for offset in picks)


def _span(items: list[dict[str, Any]]) -> tuple[int, int]:
    starts = [int(number(item.get("Frame"), 0.0)) for item in items]
    ends = [
        int(number(item.get("Frame"), 0.0)) + max(1, int(number(item.get("Length"), 1.0)))
        for item in items
    ]
    return min(starts), max(ends)


def build_cases(files: list[Path]) -> tuple[list[Case], list[str]]:
    cases: list[Case] = []
    skipped: list[str] = []
    cursor = 0
    for path in files:
        for index, template in enumerate(load_template(path)):
            items = [copy.deepcopy(item) for item in template.items]
            kinds = {type_name(item) for item in items}
            if kinds & SKIPPED_ITEMS:
                skipped.append(f"{template.name}（{', '.join(sorted(kinds & SKIPPED_ITEMS))}）")
                continue
            first, end = _span(items)
            length = max(MIN_SLOT, min(end - first, 300))
            top = min(int(number(item.get("Layer"), 0.0)) for item in items)
            for item in items:
                offset = int(number(item.get("Frame"), 0.0)) - first
                item["Frame"] = offset + cursor
                item["Layer"] = int(number(item.get("Layer"), 0.0)) - top
                # 枠より長いアイテムは枠の終わりで切る 切らないと次のテンプレートの枠へ
                # はみ出し、YMM4 の絵にだけ前のテンプレートが映り込む
                item_length = max(1, int(number(item.get("Length"), 1.0)))
                item["Length"] = max(1, min(item_length, length - offset))
            note = ""
            if all(type_name(item) == "TransitionItem" for item in items):
                # 場面切り替えだけのテンプレート 下に前の場面と後の場面を敷き、切れ目を真ん中に置く
                for item in items:
                    item["Layer"] = int(item["Layer"]) + 1
                half = length // 2
                before = base_shape(cursor, 0, half)
                after = base_shape(cursor + half, 0, length - half)
                after["ShapeParameter"]["Brush"]["Parameter"]["Color"] = "#FF2C7AE0"
                after["X"] = _still(200.0)
                before["X"] = _still(-200.0)
                items[:0] = [before, after]
                note = "前後の場面の図形を敷いた"
            has_content = any(type_name(item) not in ("GroupItem",) for item in items)
            if not has_content:
                # エフェクトだけのテンプレート グループの範囲の中（1 つ下）に下地を置く
                # 別のグループがいる段は避ける 同じ段に重ねると YMM4 は下地を空いた段へ
                # ずらして描き、こちらは重ねたまま描くので、掛かるグループが食い違う
                # （オーラはグループが 0・1・3 段にあり、1 段目に置いた下地へ YMM4 は
                # 1 段目のグループのノイズを掛けていた）
                group = items[0]
                taken = {int(item.get("Layer", 0)) for item in items}
                below = int(group.get("Layer", 0)) + 1
                while below in taken:
                    below += 1
                items.append(base_shape(cursor, below, length))
                for item in items:
                    if type_name(item) == "GroupItem":
                        item["Length"] = length
                note = "下地の図形に着せた"
            cases.append(
                Case(
                    name=template.name,
                    file=path.name,
                    index=index,
                    start=cursor,
                    length=length,
                    items=items,
                    note=note,
                )
            )
            cursor += length + GAP
    return cases, skipped


def write_project(cases: list[Case], target: Path) -> None:
    items = [item for case in cases for item in case.items]
    length = max((case.start + case.length for case in cases), default=1) + GAP
    max_layer = max((int(item.get("Layer", 0)) for item in items), default=0)
    document = {
        "FilePath": str(target),
        "SelectedTimelineIndex": 0,
        "Timelines": [
            {
                "ID": str(uuid.uuid4()),
                "Name": "メイン",
                "VideoInfo": {"FPS": FPS, "Hz": 48000, "Width": WIDTH, "Height": HEIGHT},
                "Items": items,
                "LayerSettings": {"Items": []},
                "CurrentFrame": 0,
                "Length": length,
                "MaxLayer": max_layer + 1,
            }
        ],
        "Characters": [],
        "CollapsedGroups": [],
    }
    target.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8-sig")


def command_build(arguments: argparse.Namespace) -> int:
    work: Path = arguments.work
    work.mkdir(parents=True, exist_ok=True)
    files = sorted(
        path
        for source in arguments.sources
        for path in (source.rglob("*.ymmt") if source.is_dir() else [source])
    )
    if arguments.exclude:
        # YMM4 自身が書き出しに失敗するテンプレートがある 外して並べ直す
        files = [path for path in files if not any(word in path.name for word in arguments.exclude)]
    cases, skipped = build_cases(files)
    write_project(cases, work / "compare.ymmp")
    manifest = {
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "skipped": skipped,
        "cases": [
            {
                "name": case.name,
                "file": case.file,
                "index": case.index,
                "start": case.start,
                "length": case.length,
                "note": case.note,
                "items": case.items,
            }
            for case in cases
        ],
    }
    (work / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    total = max((case.start + case.length for case in cases), default=0)
    print(
        f"{len(cases)} 本を並べた（{total} フレーム、{total / FPS:.0f} 秒）"
        f" 飛ばした {len(skipped)} 本"
    )
    print(f"YMM4 で {work / 'compare.ymmp'} を開き、{work / 'ymm4.mp4'} へ書き出してください")
    return 0


def _shrink(image: np.ndarray) -> np.ndarray:
    """面積の平均で縮める 1920x1080 から 4 分の 1 ならちょうど割り切れる"""
    height, width = image.shape[:2]
    fy, fx = height // COMPARE_HEIGHT, width // COMPARE_WIDTH
    cropped = image[: COMPARE_HEIGHT * fy, : COMPARE_WIDTH * fx, :3].astype(np.float32)
    return cropped.reshape(COMPARE_HEIGHT, fy, COMPARE_WIDTH, fx, 3).mean(axis=(1, 3))


def _save_png(image: np.ndarray, target: Path) -> None:
    """RGB の配列を PNG へ 画像のためだけに Pillow を足さず、入っている Qt で書く"""
    from PySide6.QtGui import QImage

    height, width = image.shape[:2]
    data = np.ascontiguousarray(image)
    QImage(data.data, width, height, width * 3, QImage.Format.Format_RGB888).save(str(target))


def _ymm4_frames(video: Path, wanted: set[int]) -> dict[int, np.ndarray]:
    import av

    found: dict[int, np.ndarray] = {}
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        rate = float(stream.average_rate or FPS)
        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                continue
            index = round(float(frame.pts * frame.time_base) * rate)
            if index in wanted:
                found[index] = frame.to_ndarray(format="rgb24")
            if len(found) == len(wanted):
                break
    return found


def command_compare(arguments: argparse.Namespace) -> int:
    from kumiki.core.model import Project, ProjectSettings
    from kumiki.core.timebase import FrameRate
    from kumiki.engine.render import FrameRenderer

    work: Path = arguments.work
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    video = work / "ymm4.mp4"
    if not video.exists():
        print(f"{video} がありません YMM4 で書き出してから走らせてください")
        return 1

    cases = manifest["cases"]
    if arguments.only:
        # カンマで区切って何語でも 名前にどれかを含むものを比べる
        words = [word for word in arguments.only.split(",") if word]
        cases = [case for case in cases if any(word in case["name"] for word in words)]
    wanted: dict[int, dict[str, Any]] = {}
    for raw in cases:
        case = Case(**{**raw, "items": raw["items"]})
        for frame in case.sample_frames():
            wanted[frame] = raw
    references = _ymm4_frames(video, set(wanted))

    settings = ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(FPS))
    images = work / "images"
    images.mkdir(exist_ok=True)
    rows: list[tuple[float, str, str, int, str, str]] = []
    report = CompatibilityReport()
    renderer: FrameRenderer | None = None
    try:
        for raw in cases:
            case = Case(**raw)
            objects = map_template(case.items, report=report)
            project = Project.create(settings)
            for command in place(objects, project, at_frame=case.start):
                project = command.apply(project)
            if renderer is None:
                renderer = FrameRenderer(project)
            else:
                renderer.set_project(project)
            for frame in case.sample_frames():
                reference = references.get(frame)
                if reference is None:
                    continue
                ours = renderer.render(frame)
                a, b = _shrink(reference), _shrink(ours)
                difference = float(np.abs(a - b).mean())
                stem = f"{case.start:06d}_{frame:06d}"
                side = np.concatenate([a, b, np.abs(a - b) * 3.0], axis=1)
                _save_png(np.clip(side, 0, 255).astype(np.uint8), images / f"{stem}.png")
                rows.append((difference, case.name, case.file, frame, stem, case.note))
    finally:
        if renderer is not None:
            renderer.close()

    rows.sort(reverse=True)
    _write_report(work / "report.html", rows, manifest.get("skipped", []), report)
    (work / "report.json").write_text(
        json.dumps(
            [
                {"difference": d, "name": n, "file": f, "frame": fr, "image": s, "note": note}
                for d, n, f, fr, s, note in rows
            ],
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    for difference, name, _, frame, _, _ in rows[: arguments.top]:
        print(f"{difference:6.1f}  {name}  フレーム {frame}")
    return 0


def _write_report(
    target: Path,
    rows: list[tuple[float, str, str, int, str, str]],
    skipped: list[str],
    report: CompatibilityReport,
) -> None:
    body = [
        "<!doctype html><meta charset='utf-8'><title>YMM4 と Kumiki の比較</title>",
        "<style>body{font-family:sans-serif;background:#111;color:#ddd}"
        "img{max-width:100%}td{vertical-align:top;padding:4px}</style>",
        "<h1>YMM4（左）と Kumiki（中）と差（右、3 倍）</h1>",
        f"<p>比べた絵 {len(rows)} 枚 差は 0〜255 の平均</p>",
        "<table>",
    ]
    for difference, name, file, frame, stem, note in rows:
        body.append(
            f"<tr><td>{difference:.1f}<br>{html.escape(name)}<br>{html.escape(file)}"
            f"<br>フレーム {frame}<br>{html.escape(note)}</td>"
            f"<td><img src='images/{stem}.png'></td></tr>"
        )
    body.append("</table><h2>飛ばしたテンプレート</h2><ul>")
    body.extend(f"<li>{html.escape(line)}</li>" for line in skipped)
    body.append("</ul><h2>写すときの記録</h2><ul>")
    body.extend(f"<li>{html.escape(line)}</li>" for line in report.lines())
    body.append("</ul>")
    target.write_text("\n".join(body), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("sources", type=Path, nargs="+")
    build.add_argument("--exclude", action="append", default=[], help="ファイル名に含む語で外す")
    compare = commands.add_parser("compare")
    compare.add_argument("--only", default="")
    compare.add_argument("--top", type=int, default=30)
    arguments = parser.parse_args()
    if arguments.command == "build":
        return command_build(arguments)
    return command_compare(arguments)


if __name__ == "__main__":
    sys.exit(main())
