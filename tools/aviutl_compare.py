"""AviUtl2 本体の絵と Kumiki の絵を並べて比べる

AviUtl の効果は、項目名を実物（AviUtl2 に作らせたエイリアス）から読み取って写している
読めて描けることはテストで確かめられるが、**AviUtl と同じ絵になるか**は本体で
描いてみないと分からない 値の意味（加速の向き・角度の基準・単位）はここで初めて分かる

使い方（3 段）

1. ``build``   エイリアスを時間をずらして並べた AviUtl2 のプロジェクト（.aup2）と、
               どこに何を置いたかの一覧（manifest.json）を作る
2. AviUtl2 でそのプロジェクトを開き、同じフォルダへ ``aviutl.mp4`` として書き出す
3. ``compare`` 書き出した動画と、同じオブジェクトを Kumiki で描いた絵を並べ、
               差の大きい順に一覧（report.html）と並べた絵（PNG）を作る

作業フォルダは既定で ``.work/aviutl-compare`` リポジトリには入れない
（配布物の絵が入るため）

YMM4 側の ``tools/ymm4_compare.py`` と同じ考え方 違うのは、AviUtl の
プロジェクトが INI に似た文字の形なので、**エイリアスの本文をそのまま並べ直す**ところ
値を書き戻さないので、写し間違いが比べる側に混ざらない

差の読み方

* 0 にはならない AviUtl 側は h.264 を通っているので鮮やかな色が痩せる
  （赤 255 が 232、青 255 が 243） 画面いっぱいの原色では 5 前後が残る
* 乱数で絵が決まるもの（集中線・粒子化・ノイズ・砕け散る）は線や粒の向きが
  毎回変わるので、1 枚ずつ引き比べても縮まらない 平均から外して印を付ける
  値の意味は、占有率や明るさのような統計を測って別に確かめる
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kumiki.compat.aviutl.encoding import read_text  # noqa: E402
from kumiki.compat.aviutl.exo import ALIAS_SUFFIXES, ExoParseError, load_exo  # noqa: E402
from kumiki.compat.aviutl.mapping import map_object  # noqa: E402
from kumiki.compat.aviutl.report import CompatibilityReport, global_report  # noqa: E402
from kumiki.compat.catalog import place  # noqa: E402
from kumiki.compat.mapped import MappedObject  # noqa: E402

WIDTH, HEIGHT, FPS = 1920, 1080, 60
#: 比べる絵の大きさ 書き出しは圧縮されるので、縮めてならしてから比べる
COMPARE_WIDTH, COMPARE_HEIGHT = 480, 270
#: エイリアス 1 本あたりの枠（フレーム）
SLOT = 120
#: 枠と枠の間に空ける黒 前のエイリアスの残りが次へ混ざらないように
GAP = 12
DEFAULT_WORK = ROOT / ".work" / "aviutl-compare"

#: プロジェクトの先頭 実物の .aup2（AviUtl2 v2.1.6a の自動控え）から写した
_HEADER = """[project]
version=2010601
file={file}
display.scene=0
preview.scene=0
[scene.0]
scene=0
name=Root
video.width={width}
video.height={height}
video.rate={rate}
video.scale=1
audio.rate=44100
cursor.frame=0
cursor.layer=0
preview.frame=0
display.frame=0
display.layer=0
display.zoom=10000
display.order=0
display.camera=
"""


@dataclass
class Case:
    """比べるエイリアス 1 本"""

    name: str
    file: str
    index: int
    start: int
    length: int
    note: str = ""
    blocks: list[list[str]] = field(default_factory=list)

    def sample_frames(self) -> list[int]:
        """比べるフレーム 入りと真ん中と終わりの手前

        登場と退場の効果は端でしか出ないので、両端を必ず見る
        """
        last = self.length - 1
        picks = {min(3, last), last // 2, max(0, last - 4)}
        return sorted(self.start + offset for offset in picks)


def _sections(text: str) -> list[list[str]]:
    """``[Object]`` ``[Object.0]`` … を節ごとの行の並びへ

    本文をそのまま持ち回る 値を読み書きすると、写し間違いが比べる側にも入る
    """
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("[") and line.endswith("]"):
            current = []
            blocks.append(current)
            continue
        if current is not None and line:
            current.append(line)
    return blocks


def _own_length(head: list[str]) -> int:
    """``[Object]`` の ``frame=始まり,終わり`` から、そのエイリアス自身の長さを読む

    書いていなければ SLOT 区間の長さが分からないときは短く切らない
    """
    for line in head:
        key, _, value = line.partition("=")
        if key.strip() != "frame":
            continue
        first, _, last = value.partition(",")
        try:
            span = int(last) - int(first) + 1
        except ValueError:
            return SLOT
        return span if span > 1 else SLOT
    return SLOT


def build_cases(files: list[Path]) -> tuple[list[Case], list[str]]:
    cases: list[Case] = []
    skipped: list[str] = []
    cursor = 0
    for path in files:
        try:
            document = load_exo(path)
        except (ExoParseError, OSError) as exc:
            skipped.append(f"{path.name}（{type(exc).__name__}）")
            continue
        if document.generation < 2:
            # AviUtl1 のエイリアスは AviUtl2 のプロジェクトへそのまま置けない
            skipped.append(f"{path.name}（AviUtl1 世代）")
            continue
        text, _ = read_text(path)
        blocks = _sections(text)
        if len(blocks) < 2:
            skipped.append(f"{path.name}（中身が無い）")
            continue
        # 先頭は [Object]（区間と重ね順） 残りが中身とフィルタ
        #
        # 長さはエイリアス自身が持つものに合わせる SLOT に伸ばすと、AviUtl 側だけが
        # 長い区間になり、終わり際のフレームでこちらだけ何も無くなる
        # 登場と退場の効き方も区間の長さで決まるので、揃えないと比べられない
        length = min(SLOT, _own_length(blocks[0]))
        cases.append(
            Case(
                name=path.stem,
                file=path.name,
                index=len(cases),
                start=cursor,
                length=length,
                blocks=blocks[1:],
            )
        )
        cursor += length + GAP
    return cases, skipped


def write_project(cases: list[Case], target: Path) -> None:
    """並べたプロジェクトを書く 1 本を 1 レイヤーの 1 区間に置く"""
    out = [_HEADER.format(file=target, width=WIDTH, height=HEIGHT, rate=FPS)]
    for number, case in enumerate(cases):
        end = case.start + case.length - 1
        out.append(f"[{number}]")
        out.append("layer=0")
        out.append(f"frame={case.start},{end}")
        for index, block in enumerate(case.blocks):
            out.append(f"[{number}.{index}]")
            out.extend(block)
    target.write_text("\n".join(out) + "\n", encoding="utf-8")


def default_files() -> list[Path]:
    """比べる相手 手元のエイリアスと、落としてきた配布物"""
    roots: list[Path] = []
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        roots.append(Path(program_data) / "aviutl2" / "Alias")
    roots.append(ROOT / "tests" / "fixtures" / "aviutl")
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for suffix in ALIAS_SUFFIXES:
            found.extend(sorted(root.rglob(f"*{suffix}")))
    return sorted(set(found))


def command_build(arguments: argparse.Namespace) -> int:
    work: Path = arguments.work
    work.mkdir(parents=True, exist_ok=True)
    files = [Path(item) for item in arguments.files] or default_files()
    if not files:
        print("エイリアスが見つかりません")
        return 1

    cases, skipped = build_cases(files)
    if not cases:
        print("並べられるエイリアスがありません")
        return 1

    project = work / "compare.aup2"
    write_project(cases, project)
    (work / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": case.name,
                        "file": case.file,
                        "index": case.index,
                        "start": case.start,
                        "length": case.length,
                        "note": case.note,
                    }
                    for case in cases
                ],
                "skipped": skipped,
                "files": [str(path) for path in files],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    last = cases[-1].start + cases[-1].length
    print(f"{len(cases)} 本を並べた（{last} フレーム ＝ {last / FPS:.1f} 秒）")
    print(f"AviUtl2 で {project} を開き、{work / 'aviutl.mp4'} へ書き出してください")
    for line in skipped:
        print(f"  飛ばした: {line}")
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


def _video_frames(video: Path, wanted: set[int]) -> dict[int, np.ndarray]:
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
    video = work / "aviutl.mp4"
    if not video.exists():
        print(f"{video} がありません AviUtl2 で書き出してから走らせてください")
        return 1

    by_name = {Path(item).stem: Path(item) for item in manifest["files"]}
    cases = manifest["cases"]
    if arguments.only:
        words = [word for word in arguments.only.split(",") if word]
        cases = [case for case in cases if any(word in case["name"] for word in words)]

    wanted: set[int] = set()
    for raw in cases:
        wanted.update(Case(**raw).sample_frames())
    references = _video_frames(video, wanted)

    settings = ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(FPS))
    images = work / "images"
    images.mkdir(exist_ok=True)
    rows: list[tuple[float, str, str, int, str, str]] = []
    report = CompatibilityReport()
    renderer: FrameRenderer | None = None
    try:
        for raw in cases:
            case = Case(**raw)
            source = by_name.get(case.name)
            if source is None:
                continue
            objects = [
                item
                for obj in load_exo(source).objects
                if (item := map_object(obj, settings.frame_rate, report=report)) is not None
            ]
            project = Project.create(settings)
            for command in place(objects, project, at_frame=case.start):
                project = command.apply(project)
            random_based = _is_random(objects)
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
                note = case.note
                if random_based:
                    note = (note + " " if note else "") + RANDOM_NOTE
                rows.append((difference, case.name, case.file, frame, stem, note))
    finally:
        if renderer is not None:
            renderer.close()

    rows.sort(reverse=True)
    steady = [row for row in rows if RANDOM_NOTE not in row[5]]
    # 写すときの記録と、描くときの記録を両方載せる スクリプトが描画の途中で
    # 諦めた（``obj.getpixeldata`` など）のは後者にしか出ない
    _write_report(work / "report.html", rows, manifest.get("skipped", []), report, global_report)
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
    if steady:
        average = sum(row[0] for row in steady) / len(steady)
        print(f"{len(steady)} 枚を比べた 平均の差 {average:.1f}")
    if len(rows) != len(steady):
        print(f"（{len(rows) - len(steady)} 枚は乱数で絵が決まるので平均に入れていない）")
    for difference, name, _, frame, _, _ in steady[: arguments.top]:
        print(f"{difference:6.1f}  {name}  フレーム {frame}")
    return 0


#: 乱数で絵が決まる中身と効果 線や粒の向きが毎回変わるので、
#: 1 枚ずつ引き比べても差は縮まらない 値の意味は別に測って確かめる
RANDOM_SHAPES = frozenset({"concentration"})
RANDOM_EFFECTS = frozenset({"noise", "particles", "crash"})

#: 乱数のものに付ける印 平均から外すかどうかの判定にも使う
RANDOM_NOTE = "乱数（1 枚ずつは比べない）"


def _is_random(objects: list[MappedObject]) -> bool:
    """この見本が乱数で絵を決めるか"""
    for item in objects:
        source = item.clip.source
        if source is not None and str(source.params.get("shape", "")) in RANDOM_SHAPES:
            return True
        if any(effect.kind in RANDOM_EFFECTS for effect in item.clip.effects):
            return True
    return False


def _write_report(
    target: Path,
    rows: list[tuple[float, str, str, int, str, str]],
    skipped: list[str],
    report: CompatibilityReport,
    while_drawing: CompatibilityReport,
) -> None:
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        "<title>AviUtl と Kumiki の比べ</title>",
        "<style>body{font-family:sans-serif;background:#111;color:#eee}",
        "img{image-rendering:pixelated;width:1440px}",
        "td{padding:4px 8px;vertical-align:top}</style>",
        "<h1>AviUtl と Kumiki の比べ</h1>",
        "<p>左が AviUtl 本体、真ん中が Kumiki、右が差（3 倍に強めて表示）</p>",
        "<p>差は 0 にはならない AviUtl 側は h.264 を通っているので、"
        "鮮やかな色が痩せる（赤 255 が 232、青 255 が 243 になる） "
        "画面いっぱいの原色では、これだけで 5 前後の差が残る</p>",
        "<table>",
    ]
    for difference, name, file, frame, stem, note in rows:
        label = html.escape(f"{name}（{file}） フレーム {frame}")
        if note:
            label += f" <small>{html.escape(note)}</small>"
        parts.append(
            f"<tr><td>{difference:6.1f}</td><td>{label}<br><img src='images/{stem}.png'></td></tr>"
        )
    parts.append("</table>")
    if skipped:
        parts.append("<h2>並べなかったもの</h2><ul>")
        parts.extend(f"<li>{html.escape(line)}</li>" for line in skipped)
        parts.append("</ul>")
    if report.lines():
        parts.append("<h2>写せていないもの</h2><ul>")
        parts.extend(f"<li>{html.escape(line)}</li>" for line in report.lines())
        parts.append("</ul>")
    if while_drawing.lines():
        parts.append("<h2>描くときに諦めたもの</h2><ul>")
        parts.extend(f"<li>{html.escape(line)}</li>" for line in while_drawing.lines())
        parts.append("</ul>")
    target.write_text("\n".join(parts), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK, help="作業フォルダ")
    subparsers = parser.add_subparsers(dest="command", required=True)

    builder = subparsers.add_parser("build", help="並べたプロジェクトを作る")
    builder.add_argument("files", nargs="*", help="エイリアス 省略すると手元のものを全部")
    builder.set_defaults(func=command_build)

    comparer = subparsers.add_parser("compare", help="書き出した動画と比べる")
    comparer.add_argument("--only", default="", help="名前にこの語を含むものだけ カンマ区切り")
    comparer.add_argument("--top", type=int, default=20, help="差の大きい順に何件出すか")
    comparer.set_defaults(func=command_compare)

    arguments = parser.parse_args()
    result: int = arguments.func(arguments)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
