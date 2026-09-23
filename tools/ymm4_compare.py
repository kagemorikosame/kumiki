"""YMM4 本体の絵と Sashimono の絵を並べて比べる

YMM4 のテンプレートは、値の意味を配布物の並びから読み取って写している 読めて描ける
ことはテストで確かめられるが、**YMM4 と同じ絵になるか**は本体で描いてみないと分からない

使い方（3 段）

1. ``build``   テンプレートを時間をずらして並べた YMM4 のプロジェクト（.ymmp）と、
               どこに何を置いたかの一覧（manifest.json）を作る
2. YMM4 でそのプロジェクトを開き、同じフォルダへ ``ymm4.mp4`` として書き出す（手作業）
3. ``compare`` 書き出した動画と、同じアイテムを Sashimono で描いた絵を並べ、差の大きい順に
               一覧（report.html）と並べた絵（PNG）を作る

音は絵と別の 2 段（Issue #89 の残り）

1. ``audio-build``   正弦波を並べた探り用のプロジェクト（audio-probe.ymmp）を作る
2. YMM4 でそれを開き、同じフォルダへ ``audio-probe.mp4`` として書き出す（手作業）
3. ``audio-measure`` 書き出した音を枠ごとに測り、音量の曲線・定位の向き・
                     再生速度 0 の意味を表にする

作業フォルダは既定で ``.work/ymm4-compare`` リポジトリには入れない
（配布物の絵が入るため）
"""

from __future__ import annotations

import argparse
import copy
import html
import itertools
import json
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sashimono.compat.aviutl.report import CompatibilityReport  # noqa: E402
from sashimono.compat.catalog import place  # noqa: E402
from sashimono.compat.ymm4.template import load_template, map_template  # noqa: E402
from sashimono.compat.ymm4.values import number, type_name  # noqa: E402

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

    def sample_frames(self, *, every: bool = False) -> list[int]:
        """比べるフレーム 既定は入りと真ん中と終わりの手前 ``every`` なら枠のすべて

        3 枚だけだと、場面切り替えの切れ目のように一瞬だけずれる所を見落とす
        """
        if every:
            return list(range(self.start, self.start + self.length))
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
    write_document(items, length, target)


def write_document(items: list[dict[str, Any]], length: int, target: Path) -> None:
    """アイテムの並びを YMM4 のプロジェクト（.ymmp）として書く

    絵の比較と音の探りで同じ書き方を使う YMM4 は BOM 付きの UTF-8 でないと
    プロジェクトを開けない（``utf-8-sig``）
    """
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


def frame_index(pts: int, start: int | None, time_base: Fraction, rate: float) -> int:
    """書き出した動画の 1 枚が、プロジェクトの何フレーム目かを返す

    YMM4 の書き出しは、最初の 1 枚の時刻が 0 ではなく 1 フレームぶん後ろ
    （``start_time`` が 1 フレーム）から始まる 時刻をそのままフレーム番号にすると、
    YMM4 の絵が 1 枚ずつ遅れて並び、動きのある所（場面の切れ目や動き出し）で
    差が 8〜24 跳ねる ストリームの頭の時刻を引いて、最初の 1 枚を 0 にそろえる
    """
    return round(float((pts - (start or 0)) * time_base) * rate)


#: 動画の 1 枚 フレーム番号と、絵を取り出す関数の組
#: 取り出す（RGB の配列へ変換する）のは比べる 1 枚だけ 3 枚だけ比べるときに、
#: そこまで読み進めた全部の絵を変換すると、それだけで数分かかる
Picture = tuple[int, "Callable[[], np.ndarray]"]


def _ymm4_frames(video: Path) -> Iterator[Picture]:
    """書き出した動画を頭から 1 枚ずつ返す"""
    import av

    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        rate = float(stream.average_rate or FPS)
        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                continue
            index = frame_index(frame.pts, stream.start_time, frame.time_base, rate)
            yield index, partial(frame.to_ndarray, format="rgb24")


class References:
    """YMM4 の絵を、若い番号から順に 1 枚ずつ引く

    先に全部を読んで持っておくと、全フレームを比べたときに 1920x1080 の絵が
    1 万枚を超え、メモリに載らない 比べる順（番号の若い順）に動画を進めて読む
    """

    def __init__(self, frames: Iterator[Picture]) -> None:
        self._frames = frames
        self._current: Picture | None = None
        self._asked = -1

    def get(self, wanted: int) -> np.ndarray | None:
        """``wanted`` 番の絵 動画に無ければ ``None``

        前に引いた番号より若い番号を引くと例外にする 読み進めた動画は戻せないので、
        黙って ``None`` を返すと、呼ぶ側の並べ間違いが「比べる絵が無い」に化けて気付けない
        """
        if wanted < self._asked:
            raise ValueError(f"{self._asked} 番の後に {wanted} 番は引けない 若い順に引くこと")
        self._asked = wanted
        while self._current is None or self._current[0] < wanted:
            following = next(self._frames, None)
            if following is None:
                return None
            self._current = following
        return self._current[1]() if self._current[0] == wanted else None


def command_compare(arguments: argparse.Namespace) -> int:
    from sashimono.core.model import Project, ProjectSettings
    from sashimono.core.timebase import FrameRate
    from sashimono.engine.render import FrameRenderer

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
    # 動画を頭から順に読むので、枠も頭から順に比べる
    cases = sorted(cases, key=lambda raw: int(raw["start"]))
    every: bool = arguments.every
    references = References(_ymm4_frames(video))

    settings = ProjectSettings(
        width=WIDTH, height=HEIGHT, frame_rate=FrameRate(FPS), blending=arguments.blending
    )
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
            # 全フレームを比べるときは、枠ごとに差の一番大きい 1 枚だけを残す
            # 1 枚ずつ絵を書き出すと、77 本で 1 万枚を超える
            worst: tuple[float, int, np.ndarray, np.ndarray] | None = None
            for frame in case.sample_frames(every=every):
                reference = references.get(frame)
                if reference is None:
                    continue
                ours = renderer.render(frame)
                a, b = _shrink(reference), _shrink(ours)
                difference = float(np.abs(a - b).mean())
                if every:
                    if worst is None or difference > worst[0]:
                        worst = (difference, frame, a, b)
                    continue
                rows.append(_saved_row(images, case, frame, difference, a, b))
            if worst is not None:
                rows.append(_saved_row(images, case, worst[1], worst[0], worst[2], worst[3]))
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


def _saved_row(
    images: Path, case: Case, frame: int, difference: float, a: np.ndarray, b: np.ndarray
) -> tuple[float, str, str, int, str, str]:
    """並べた絵を書き出し、一覧の 1 行を返す"""
    stem = f"{case.start:06d}_{frame:06d}"
    side = np.concatenate([a, b, np.abs(a - b) * 3.0], axis=1)
    _save_png(np.clip(side, 0, 255).astype(np.uint8), images / f"{stem}.png")
    return (difference, case.name, case.file, frame, stem, case.note)


def _write_report(
    target: Path,
    rows: list[tuple[float, str, str, int, str, str]],
    skipped: list[str],
    report: CompatibilityReport,
) -> None:
    body = [
        "<!doctype html><meta charset='utf-8'><title>YMM4 と Sashimono の比較</title>",
        "<style>body{font-family:sans-serif;background:#111;color:#ddd}"
        "img{max-width:100%}td{vertical-align:top;padding:4px}</style>",
        "<h1>YMM4（左）と Sashimono（中）と差（右、3 倍）</h1>",
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


#: 探りに使う正弦波 440Hz なら書き出しの圧縮（AAC）でもほとんど痩せない
TONE_HZ = 440.0
#: 正弦波の長さ（秒）
TONE_SECONDS = 2.0
#: 書き出しの標本化周波数 YMM4 のプロジェクトの ``Hz`` と合わせる
AUDIO_RATE = 48000
#: 枠 1 つの長さ（フレーム） 素材（2 秒）より長くして、``PlaybackRate`` を
#: 半分にしたときの 4 秒が枠の中へ収まるようにする 収まらないと、長さで
#: 速度を見分けられない
AUDIO_SLOT = 120
#: 枠と枠の間の無音（フレーム） 音は尾を引く（圧縮の先読みと後引き）ので、
#: 絵の ``GAP`` より広く取る 狭いと隣の枠の残りを測る
AUDIO_GAP = 30
#: 無音とみなす振幅 書き出しの圧縮が無音の所に乗せる雑音より上に置く
SILENCE = 2.0e-3
#: 鳴っている長さを数える窓（秒） 1 サンプルずつ見ると、波が 0 を横切る
#: 瞬間まで無音に数えてしまう
ENVELOPE_SECONDS = 0.005
#: 音量つまみを dB 目盛りと見たときの、0 のところの減衰（dB）
#: 予想値の列を出すためだけの仮定 実測がどちらに近いかは人が読んで決める
DB_SPAN = 60.0


@dataclass(frozen=True)
class AudioSlot:
    """探りの枠 1 つ 条件 1 つぶんの音声アイテムに対応する"""

    kind: str
    name: str
    start: int
    volume: float
    pan: float
    playback_rate: float


def build_audio_slots() -> list[AudioSlot]:
    """測る条件を、時間軸に重ならないように並べる

    先頭を基準（``Volume`` 100・``Pan`` 0・``PlaybackRate`` 100）にする
    ほかの枠は、この枠との比で読む

    ``Pan`` の値域は -100〜100 とした 配布物と手元のプロジェクトの音声アイテム
    125 個はすべて 0 で、実物からは決められない YMM4 本体（4.48.0.3）の IL を
    読むと、``Animation(0, -100, 100)`` を作る並び（``ldc.r8`` 3 つ）は
    ``YukkuriMovieMaker.dll`` に 18 か所あり、``Animation(0, -1, 1)`` は
    本体・Plugin・Community のどれにも 1 か所も無かった ``Volume`` が
    ``Animation(100, 0, 100)`` の百分率であることとも揃う
    もし値域が -1〜1 だったなら YMM4 が読み込みで丸めるので、-100 の枠と
    -50 の枠が同じ測り値になる 表がそうなっていたら、この判断が外れている
    """
    conditions: list[tuple[str, str, float, float, float]] = []
    # 基準を最初に置く 途中に置くと、読む人が表の 1 行目を基準と取り違える
    conditions.append(("基準", "Volume=100 Pan=0 Rate=100", 100.0, 0.0, 100.0))
    # 0 と 100 の間を細かく取る 振幅比なら 50 で 0.5、dB 目盛りなら 0.03 前後になり、
    # 2 点だけでは曲線の形（真ん中がどちらへ曲がるか）が読めない
    for volume in (0.0, 10.0, 25.0, 50.0, 75.0, 90.0):
        conditions.append(("volume", f"Volume={volume:g}", volume, 0.0, 100.0))
    for pan in (-100.0, -50.0, 50.0, 100.0):
        conditions.append(("pan", f"Pan={pan:g}", 100.0, pan, 100.0))
    for rate in (0.0, 50.0, 200.0):
        conditions.append(("rate", f"PlaybackRate={rate:g}", 100.0, 0.0, rate))
    slots: list[AudioSlot] = []
    cursor = 0
    for kind, name, volume, pan, rate in conditions:
        slots.append(
            AudioSlot(
                kind=kind, name=name, start=cursor, volume=volume, pan=pan, playback_rate=rate
            )
        )
        cursor += AUDIO_SLOT + AUDIO_GAP
    return slots


def audio_item(slot: AudioSlot, media: Path) -> dict[str, Any]:
    """探りの枠 1 つを YMM4 の音声アイテムにする

    項目の並びと既定の値は、手元の YMM4 プロジェクト 16 本に入っていた
    ``AudioItem`` 125 個から写した（推測で書くと YMM4 が開けない）
    ``Volume`` と ``Pan`` は動く値、``PlaybackRate`` はただの数という食い違いも実物どおり
    """
    return {
        "$type": "YukkuriMovieMaker.Project.Items.AudioItem, YukkuriMovieMaker",
        "IsWaveformEnabled": False,
        "FilePath": str(media),
        "AudioTrackIndex": 0,
        "Volume": _still(slot.volume),
        "Pan": _still(slot.pan),
        "PlaybackRate": slot.playback_rate,
        "ContentOffset": "00:00:00",
        "FadeIn": 0.0,
        "FadeOut": 0.0,
        "IsLooped": False,
        "EchoIsEnabled": False,
        "EchoInterval": 0.1,
        "EchoAttenuation": 40.0,
        "AudioEffects": [],
        "Group": 0,
        "Frame": slot.start,
        "Layer": 0,
        "KeyFrames": {"Frames": [], "Count": 0},
        "Length": AUDIO_SLOT,
        "Remark": slot.name,
        "IsLocked": False,
        "IsHidden": False,
    }


def audio_manifest(slots: list[AudioSlot], media: Path) -> dict[str, Any]:
    """枠の一覧 ``audio-measure`` はこれだけを見て切り出す"""
    return {
        "fps": FPS,
        "rate": AUDIO_RATE,
        "slot": AUDIO_SLOT,
        "gap": AUDIO_GAP,
        "tone_hz": TONE_HZ,
        "tone_seconds": TONE_SECONDS,
        "media": str(media),
        "baseline": 0,
        "slots": [
            {
                "index": index,
                "kind": slot.kind,
                "name": slot.name,
                "start": slot.start,
                "length": AUDIO_SLOT,
                "volume": slot.volume,
                "pan": slot.pan,
                "playback_rate": slot.playback_rate,
            }
            for index, slot in enumerate(slots)
        ],
    }


def make_tone(target: Path) -> bool:
    """ffmpeg で正弦波を作る 作れなければ ``False``

    ``.wav``（PCM）にする 外の ffmpeg にどのエンコーダが入っているかに左右されない
    """
    if shutil.which("ffmpeg") is None:
        return False
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={TONE_HZ:g}:sample_rate={AUDIO_RATE}:duration={TONE_SECONDS:g}",
        # 左右で同じ波にする 定位の向きは、鳴らし分けた左右の比でしか読めない
        "-ac",
        "2",
        str(target),
    ]
    completed = subprocess.run(command, capture_output=True, check=False)
    return completed.returncode == 0 and target.exists()


def command_audio_build(arguments: argparse.Namespace) -> int:
    # 絶対パスにしてから書く YMM4 はこの道具の作業フォルダを知らないので、
    # 相対のまま `.ymmp` へ書くと正弦波を見つけられず、全部の枠が無音になる
    # 無音の書き出しを測ると「再生速度 0 で止まる」「音量の比が 0」と読めてしまう
    work: Path = arguments.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    media = work / "audio-probe-tone.wav"
    if not make_tone(media):
        # 測れない環境で「壊れた」と読まれないように、落とさずに終える
        # （tools/bench_export.py と同じ作法）
        print("ffmpeg が無いか正弦波を作れないので、探りのプロジェクトは作れない")
        return 0
    slots = build_audio_slots()
    items = [audio_item(slot, media) for slot in slots]
    length = max(slot.start for slot in slots) + AUDIO_SLOT + AUDIO_GAP
    project = work / "audio-probe.ymmp"
    write_document(items, length, project)
    (work / "audio-probe.json").write_text(
        json.dumps(audio_manifest(slots, media), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    # 前の測り結果は捨てる 残すと、新しい枠の一覧に対応しない表を読んでしまう
    (work / "audio-report.json").unlink(missing_ok=True)
    print(f"{len(slots)} 枠を並べた（{length} フレーム、{length / FPS:.0f} 秒）")
    video = work / "audio-probe.mp4"
    if video.exists():
        # 前の書き出しが残っていると、新しい枠の一覧で古い音を切り出してしまう
        # `audio-measure` は書き出しが一覧より古ければ止めるが、ここでも言っておく
        print(f"{video} は前の探りの書き出しです 作り直した方で書き出し直してください")
    print(f"YMM4 で {project} を開き、{video} として書き出してください")
    return 0


def sample_index(
    pts: int,
    start: int | None,
    time_base: Fraction,
    rate: int,
    start_base: Fraction | None = None,
) -> int:
    """書き出した音の一切れが、頭から何サンプル目かを返す :func:`frame_index` の音版

    YMM4 の書き出しは最初の一切れの時刻が 0 から始まらない 時刻をそのまま位置に
    すると、枠がまるごとずれて隣の枠の音を測る

    ``start`` はストリームの刻み（``start_base``）、``pts`` は並べ直したあとの
    刻み（``time_base``）で数える 刻みが違うまま引き算すると、頭の位置が
    まるごとずれて、枠に別の条件の音や無音が入る

    分数のまま掛ける 先に ``float`` へ落とすと、長い書き出しの終わりの方で
    丸めが 1 サンプルずれ、そこだけ隣の枠の音が混じる
    """
    seconds = pts * time_base - (start or 0) * (start_base if start_base is not None else time_base)
    return round(seconds * rate)


def decode_audio(video: Path) -> tuple[np.ndarray, int] | None:
    """書き出した動画の音を ``(2, サンプル数)`` の配列と標本化周波数で返す

    頭の時刻のずれを詰めた位置へ置く 欠けた所は 0（無音）のまま残す

    音の道が無い書き出し（映像だけの形式で出した物）は ``None``
    ここで添字を取ると `IndexError` で終わり、測り方の案内を出せない
    """
    import av
    from av.audio.resampler import AudioResampler

    with av.open(str(video)) as container:
        if not container.streams.audio:
            return None
        stream = container.streams.audio[0]
        rate = int(stream.rate or AUDIO_RATE)
        # 左右 2 本の浮動小数へそろえる 書き出しの形式（s16 か fltp か、
        # 何本の音か）で測り方が変わらないようにする
        resampler = AudioResampler(format="fltp", layout="stereo", rate=rate)
        pieces: list[tuple[int, np.ndarray]] = []
        # 1 枚ずつ流す 全部を先に list へ溜めると、長い書き出しで
        # 復号した音を 2 重に抱えることになる 末尾の None は残りを吐かせる合図
        for frame in itertools.chain(container.decode(stream), [None]):
            for converted in resampler.resample(frame):
                if converted.pts is None or converted.time_base is None:
                    continue
                at = sample_index(
                    converted.pts,
                    stream.start_time,
                    converted.time_base,
                    rate,
                    stream.time_base,
                )
                pieces.append((at, np.asarray(converted.to_ndarray(), dtype=np.float32)))
    return assemble_audio(pieces), rate


def assemble_audio(pieces: list[tuple[int, np.ndarray]]) -> np.ndarray:
    """``(置く位置, (2, n) の一切れ)`` を 1 本の ``(2, サンプル数)`` へ並べる

    頭より前に来る一切れ（圧縮の先読み分や編集リストの扱い）は、頭より前の分を
    捨ててから置く 負の位置のまま添字にすると末尾から数えられ、先頭の音が
    終わりへ書かれるか、長さが合わずに例外で落ちる 頭より前は測る対象ではない
    """
    total = max((at + block.shape[1] for at, block in pieces), default=0)
    if total <= 0:
        return np.zeros((2, 0), dtype=np.float32)
    buffer = np.zeros((2, total), dtype=np.float32)
    for at, block in pieces:
        if at < 0:
            block = block[:, -at:]
            at = 0
        if block.shape[1] == 0:
            continue
        buffer[:, at : at + block.shape[1]] = block[:, : total - at]
    return buffer


def slot_bounds(start: int, length: int, fps: int, rate: int) -> tuple[int, int]:
    """枠の始まりと終わりを、音のサンプルの番号で返す

    フレームからサンプルへ直すときに切り捨てると、枠の頭が 1 サンプル手前へ寄り、
    前の枠の尾を測る 端は必ず「フレーム番号 × レート ÷ FPS」の丸めでそろえる
    """
    return round(start * rate / fps), round((start + length) * rate / fps)


@dataclass(frozen=True)
class AudioMeasure:
    """枠 1 つの測り値 左右は ``(左, 右)`` の順"""

    rms: tuple[float, float]
    peak: tuple[float, float]
    seconds: float
    hz: float


def measure_block(block: np.ndarray, rate: int) -> AudioMeasure:
    """枠 1 つぶんの波形から、RMS・最大振幅・鳴っている長さ・中心の周波数を測る"""
    if block.shape[1] == 0:
        return AudioMeasure(rms=(0.0, 0.0), peak=(0.0, 0.0), seconds=0.0, hz=0.0)
    rms = tuple(float(np.sqrt(np.mean(np.square(row)))) for row in block[:2])
    peak = tuple(float(np.max(np.abs(row))) for row in block[:2])
    mono = np.mean(block[:2], axis=0)
    window = max(1, int(rate * ENVELOPE_SECONDS))
    usable = (mono.shape[0] // window) * window
    if usable:
        envelope = np.max(np.abs(mono[:usable]).reshape(-1, window), axis=1)
        seconds = float(np.count_nonzero(envelope > SILENCE) * window / rate)
    else:
        seconds = 0.0
    hz = 0.0
    if seconds > 0.0:
        spectrum = np.abs(np.fft.rfft(mono))
        hz = float(np.fft.rfftfreq(mono.shape[0], 1.0 / rate)[int(np.argmax(spectrum))])
    return AudioMeasure(rms=(rms[0], rms[1]), peak=(peak[0], peak[1]), seconds=seconds, hz=hz)


def ratio(value: float, base: float) -> float:
    """基準に対する比 基準が無音なら 0 を返す（0 で割らない）"""
    return value / base if base > 0.0 else 0.0


def volume_guesses(volume: float) -> dict[str, float]:
    """音量つまみの読み方ごとの、基準に対する振幅の予想値"""
    share = volume / 100.0
    return {
        "振幅比": share,
        "二乗": share * share,
        "dB目盛り": 10.0 ** ((volume - 100.0) * DB_SPAN / 100.0 / 20.0),
    }


def command_audio_measure(arguments: argparse.Namespace) -> int:
    work: Path = arguments.work
    manifest_path = work / "audio-probe.json"
    if not manifest_path.exists():
        print(f"{manifest_path} がありません 先に audio-build を走らせてください")
        return 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # 測れなかった道で前の表が残ると、新しい結果として開けてしまう
    # 測れたときは最後に書き直すので、先に消しておけばどの道でも残らない
    (work / "audio-report.json").unlink(missing_ok=True)
    video = work / "audio-probe.mp4"
    if not video.exists():
        print(f"{video} がまだ書き出されていません")
        print(f"YMM4 で {work / 'audio-probe.ymmp'} を開き、そこへ書き出してから走らせてください")
        return 0

    if video.stat().st_mtime <= manifest_path.stat().st_mtime:
        # 探りを作り直したのに書き出しが前のままだと、新しい枠の一覧で
        # 古い音を切り出して、まるで別の条件を測ったような表が出る
        # 同じ時刻も断る 置き場によっては時刻が 2 秒刻みでしか残らず、
        # 同じ時刻は「あとで書き出した」証しにならない
        print(f"{video} は探りを作り直す前の書き出しです")
        print(f"YMM4 で {work / 'audio-probe.ymmp'} を開き直し、書き出してから走らせてください")
        return 0

    decoded = decode_audio(video)
    if decoded is None:
        print(f"{video} に音の道がありません 音が入る形式で書き出してください")
        return 0
    samples, rate = decoded
    if samples.shape[1] == 0:
        # 音の道はあるのに中身が無い書き出し 測ると全部の枠が 0 秒・比 0 になり、
        # 「再生速度 0 で止まる」と読み違える
        print(f"{video} の音が空です 音が入る形式で書き出してください")
        return 0
    fps = int(manifest.get("fps", FPS))
    measured: list[tuple[dict[str, Any], AudioMeasure]] = []
    for entry in manifest["slots"]:
        begin, end = slot_bounds(int(entry["start"]), int(entry["length"]), fps, rate)
        measured.append((entry, measure_block(samples[:, begin:end], rate)))
    base = measured[int(manifest.get("baseline", 0))][1]
    rows = [_audio_row(entry, value, base) for entry, value in measured]
    _print_audio_rows(rows)
    (work / "audio-report.json").write_text(
        json.dumps(
            {"rate": rate, "samples": int(samples.shape[1]), "rows": rows},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"{work / 'audio-report.json'} へ書いた")
    return 0


def _audio_row(entry: dict[str, Any], value: AudioMeasure, base: AudioMeasure) -> dict[str, Any]:
    both = (value.rms[0] + value.rms[1]) / 2.0
    base_both = (base.rms[0] + base.rms[1]) / 2.0
    row = {
        "index": entry["index"],
        "kind": entry["kind"],
        "name": entry["name"],
        "volume": entry["volume"],
        "pan": entry["pan"],
        "playback_rate": entry["playback_rate"],
        "rms_left": value.rms[0],
        "rms_right": value.rms[1],
        "peak_left": value.peak[0],
        "peak_right": value.peak[1],
        "seconds": value.seconds,
        "hz": value.hz,
        "left_ratio": ratio(value.rms[0], base.rms[0]),
        "right_ratio": ratio(value.rms[1], base.rms[1]),
        "both_ratio": ratio(both, base_both),
        "seconds_ratio": ratio(value.seconds, base.seconds),
    }
    if entry["kind"] in ("volume", "基準"):
        row["guesses"] = volume_guesses(float(entry["volume"]))
    return row


def _print_audio_rows(rows: list[dict[str, Any]]) -> None:
    # RMS は枠まるごとの平均 鳴っている長さが違う枠どうしを比べるときは、
    # 長さの列と合わせて読む（速度を半分にすると、枠を鳴り通して比が上がる）
    print(f"{'枠':<26}{'左比':>8}{'右比':>8}{'平均比':>8}{'長さ秒':>8}{'中心Hz':>8}")
    for row in rows:
        print(
            f"{row['name']:<26}{row['left_ratio']:>8.3f}{row['right_ratio']:>8.3f}"
            f"{row['both_ratio']:>8.3f}{row['seconds']:>8.2f}{row['hz']:>8.0f}"
        )
    volumes = [row for row in rows if "guesses" in row]
    if volumes:
        print()
        print("音量つまみの読み方くらべ（平均比が、どの予想値に近いか）")
        print(f"{'枠':<26}{'実測':>8}{'振幅比':>8}{'二乗':>8}{'dB目盛り':>10}")
        for row in volumes:
            guesses = row["guesses"]
            print(
                f"{row['name']:<26}{row['both_ratio']:>8.3f}{guesses['振幅比']:>8.3f}"
                f"{guesses['二乗']:>8.3f}{guesses['dB目盛り']:>10.4f}"
            )
    pans = [row for row in rows if row["kind"] == "pan"]
    if pans:
        print()
        print("定位の向き 左比 > 右比 なら、その値は左へ寄せる向き")
    rates = [row for row in rows if row["kind"] == "rate"]
    if rates:
        print()
        print("再生速度 長さが 0 秒なら止まる、基準と同じ長さなら等倍として扱われている")
        print("         中心Hz が基準の半分・倍なら、速度は音の高さごと変えている")


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
    compare.add_argument(
        "--every",
        action="store_true",
        help="枠のフレームをすべて比べ、枠ごとに一番大きい差を出す（既定は 3 枚だけ）",
    )
    # 既定は新しく作るプロジェクトと同じ sRGB（YMM4 の混ぜ方） リニアを選べば、
    # 設定ができる前に保存したプロジェクトの見え方で比べられる
    compare.add_argument("--blending", choices=("srgb", "linear"), default="srgb")
    commands.add_parser("audio-build")
    commands.add_parser("audio-measure")
    arguments = parser.parse_args()
    runners: dict[str, Callable[[argparse.Namespace], int]] = {
        "build": command_build,
        "compare": command_compare,
        "audio-build": command_audio_build,
        "audio-measure": command_audio_measure,
    }
    return runners[arguments.command](arguments)


if __name__ == "__main__":
    sys.exit(main())
