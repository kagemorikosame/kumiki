"""AviUtl2 本体が書き出した絵と、移動軌跡・星空を突き合わせる

``tools/aviutl_compare.py`` で並べて AviUtl2 v2.1.6a に書き出させた ``.work/aviutl-p5`` と
``.work/aviutl-p6-time`` ``.work/aviutl-p6-wave``（1920x1080 60fps）を読む
配布物の絵が入るのでリポジトリには入れていない 無ければ飛ばす

移動軌跡は 1 枚ずつ比べる 星空は置き場所が乱数なので、粒の数・流れる向き・速さを比べる
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import numpy as np
import pytest

from kumiki.compat.aviutl.exo import load_exo
from kumiki.compat.aviutl.mapping import map_object
from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.core.timebase import FrameRate
from kumiki.engine.sources import render_source

WORKS = Path(__file__).resolve().parents[2] / ".work"


def _case(name: str) -> tuple[Path, dict[str, object]]:
    """見本の名前から、書き出した動画のある作業フォルダと並べた位置を引く 無ければ飛ばす"""
    for work in (WORKS / "aviutl-p5", WORKS / "aviutl-p6-time", WORKS / "aviutl-p6-wave"):
        manifest = work / "manifest.json"
        if not (manifest.exists() and (work / "aviutl.mp4").exists()):
            continue
        for case in json.loads(manifest.read_text(encoding="utf-8"))["cases"]:
            if case["name"] == name:
                return work, case
    pytest.skip(f"{name} を書き出した AviUtl2 の動画が無い")


def _reference(name: str, offsets: list[int]) -> dict[int, np.ndarray]:
    av = pytest.importorskip("av")
    work, case = _case(name)
    start = int(str(case["start"]))
    wanted = {start + offset for offset in offsets}
    found: dict[int, np.ndarray] = {}
    with av.open(str(work / "aviutl.mp4")) as container:
        stream = container.streams.video[0]
        rate = float(stream.average_rate or 60)
        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                continue
            index = round(float(frame.pts * frame.time_base) * rate)
            if index in wanted:
                found[index - start] = frame.to_ndarray(format="rgb24").astype(np.float32)
            if index > max(wanted):
                break
    return found


def _ours(name: str, offsets: list[int]) -> dict[int, np.ndarray]:
    _, case = _case(name)
    source_path = Path(str(case["source"]))
    if not source_path.exists():
        pytest.skip(f"{source_path} が無い")
    item = map_object(load_exo(source_path).objects[0], FrameRate(60), report=CompatibilityReport())
    assert item is not None and item.clip.source is not None
    drawn: dict[int, np.ndarray] = {}
    for offset in offsets:
        image = render_source(
            item.clip.source, 1920, 1080, frame=offset, fps=60.0, duration=item.clip.duration
        )
        assert image is not None
        alpha = image[:, :, 3:4].astype(np.float32) / 255.0
        drawn[offset] = image[:, :, :3].astype(np.float32) * alpha
    return drawn


@pytest.mark.parametrize(
    "name",
    [
        "kumiki_p5_line_x",
        "kumiki_p5_line_xy",
        # 点線と帯（描画間隔 300・補助描画 50） 固定速度 10 と先端の角度 45 度の星型
        "kumiki_p6_line_dotted",
        "kumiki_p6_line_fixed",
    ],
)
def test_the_trail_matches_aviutl(name: str) -> None:
    # 入り・真ん中・終わり 先端の向きは端で前後の片側しか見られないので、両端を必ず見る
    offsets = [0, 3, 40, 76, 80]
    reference = _reference(name, offsets)
    ours = _ours(name, offsets)
    for offset in offsets:
        difference = np.abs(ours[offset] - reference[offset]).max(axis=2)
        # 実装前は何も描かず、線の画素がまるごと差になっていた（2 万画素が 255）
        # 移した後は縁の半画素だけが残る
        assert float(difference.mean()) < 0.2, offset
        assert int((difference > 64).sum()) < 2000, offset


def _blobs(lit: np.ndarray) -> list[tuple[float, float]]:
    """明るい粒の中心（画面の中心から） 小さい絵なので素直に塗りつぶしで数える"""
    mask = lit > 20
    seen = np.zeros_like(mask)
    centres: list[tuple[float, float]] = []
    for y, x in zip(*np.nonzero(mask), strict=True):
        if seen[y, x]:
            continue
        queue = deque([(int(y), int(x))])
        seen[y, x] = True
        points: list[tuple[int, int]] = []
        while queue:
            cy, cx = queue.popleft()
            points.append((cy, cx))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = cy + dy, cx + dx
                if (
                    0 <= ny < mask.shape[0]
                    and 0 <= nx < mask.shape[1]
                    and mask[ny, nx]
                    and not seen[ny, nx]
                ):
                    seen[ny, nx] = True
                    queue.append((ny, nx))
        array = np.array(points, dtype=np.float64)
        centres.append((array[:, 1].mean() - 960.0, array[:, 0].mean() - 540.0))
    return centres


def _flow(images: dict[int, np.ndarray]) -> tuple[float, float, float]:
    """粒の数の平均・外へ向かう割合・1 フレームで中心から離れる割合の中央値"""
    counts: list[int] = []
    outward: list[bool] = []
    growth: list[float] = []
    previous: list[tuple[float, float]] = []
    for offset in sorted(images):
        found = _blobs(images[offset].max(axis=2))
        counts.append(len(found))
        if previous and found:
            now = np.array(found)
            for x, y in previous:
                distance = np.hypot(now[:, 0] - x, now[:, 1] - y)
                nearest = int(distance.argmin())
                radius = float(np.hypot(x, y))
                if distance[nearest] < 40 and radius > 50:
                    moved = now[nearest] - (x, y)
                    growth.append(float(np.hypot(*now[nearest])) / radius - 1.0)
                    if np.hypot(*moved) > 0.5:
                        outward.append(float(np.dot(moved, (x, y))) > 0)
        previous = found
    return float(np.mean(counts)), float(np.mean(outward)), float(np.median(growth))


@pytest.mark.parametrize(
    "name", ["kumiki_p5_star_n30", "kumiki_p5_star_n30_fast", "kumiki_p6_star_n1500"]
)
def test_the_stars_flow_like_aviutl(name: str) -> None:
    # 実物は 30 個のうち 19 個前後が画面に入り、すべて外へ流れ、1 フレームで
    # 中心からの距離が 0.72%（速度 6）・1.46%（速度 12）伸びた 1500 個では 922 個が見えた
    offsets = list(range(0, 81, 2))
    theirs = _flow(_reference(name, offsets))
    ours = _flow(_ours(name, offsets))
    assert ours[0] == pytest.approx(theirs[0], rel=0.3)
    assert ours[1] > 0.9 and theirs[1] > 0.9
    # 2 フレームおきに測っているので伸びは 2 倍前後 速さがずれると真っ先にここへ出る
    assert ours[2] == pytest.approx(theirs[2], rel=0.15)


#: 音声波形表示の見本が描く音 手元に無ければ飛ばす（配布物なのでリポジトリには入れない）
BGM = Path(r"D:\V用のBGM\138_BPM150.mp3")


def test_the_waveform_follows_the_sound() -> None:
    # 1 画素 1 サンプル（44.1kHz）・窓の頭がフレームの時刻・正が下 この 3 つのどれかが
    # 違うと、線の縦の位置がフレームごとに実物とずれて相関が落ちる（逆さまなら負になる）
    if not BGM.exists():
        pytest.skip("音声波形表示の見本が描く音が無い")
    from kumiki.core.commands import AddClip, AddTrack
    from kumiki.core.model import Project, ProjectSettings, Track, TrackKind
    from kumiki.engine.gpu import GLContextError, OffscreenGLContext
    from kumiki.engine.render import FrameRenderer

    name = "kumiki_p6_wave_range"
    offsets = [30, 40, 50, 60, 70]
    reference = _reference(name, offsets)
    _, case = _case(name)
    item = map_object(
        load_exo(Path(str(case["source"]))).objects[0], FrameRate(60), report=CompatibilityReport()
    )
    assert item is not None
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    settings = ProjectSettings(width=1920, height=1080, frame_rate=FrameRate(60), sample_rate=44100)
    project = Project.create(settings)
    track = Track(kind=TrackKind.VIDEO, name="V1")
    project = AddTrack(track).apply(project)
    project = AddClip(track.id, item.clip).apply(project)
    renderer = FrameRenderer(project, context=context)
    try:
        for offset in offsets:
            ours = renderer.render(offset)[:, :, :3].max(axis=2).astype(np.float64)
            theirs = reference[offset].max(axis=2)
            rows = np.arange(340, 740, dtype=np.float64)
            columns = slice(560, 1290)
            ours_y = (ours[340:740, columns] * rows[:, None]).sum(0) / ours[340:740, columns].sum(0)
            theirs_y = (theirs[340:740, columns] * rows[:, None]).sum(0) / theirs[
                340:740, columns
            ].sum(0)
            correlation = float(np.corrcoef(ours_y, theirs_y)[0, 1])
            assert correlation > 0.95, offset
            assert float(np.abs(ours_y - theirs_y).mean()) < 4.0, offset
    finally:
        renderer.close()
        context.release()


def _shrunk(image: np.ndarray) -> np.ndarray:
    """比べる道具と同じく 480x270 へ面積の平均で縮める 圧縮の揺れをならすため"""
    rgb = image[:, :, :3].astype(np.float64)
    return rgb.reshape(270, 4, 480, 4, 3).mean(axis=(1, 3))


@pytest.mark.parametrize(
    ("name", "limit"),
    [
        # 升目（縦 16・横 16・両方とスペース 4）とスペクトラム（細かいものと 16 本の棒）
        # 升目を読まずに細い線で描いていた頃は、それぞれ 5.6・2.8・6.2・4.2・6.6 まで開いた
        ("kumiki_p6_wave_v16", 2.5),
        ("kumiki_p6_wave_h16", 1.5),
        ("kumiki_p6_wave_h16_v16_space4", 2.5),
        ("kumiki_p6_wave_spec_plain", 2.5),
        ("kumiki_p6_wave_spec_h16", 3.5),
    ],
)
def test_the_waveform_modes_match_aviutl(name: str, limit: float) -> None:
    if not BGM.exists():
        pytest.skip("音声波形表示の見本が描く音が無い")
    from kumiki.core.commands import AddClip, AddTrack
    from kumiki.core.model import Project, ProjectSettings, Track, TrackKind
    from kumiki.engine.gpu import GLContextError, OffscreenGLContext
    from kumiki.engine.render import FrameRenderer

    offsets = [3, 40, 76]
    reference = _reference(name, offsets)
    _, case = _case(name)
    item = map_object(
        load_exo(Path(str(case["source"]))).objects[0], FrameRate(60), report=CompatibilityReport()
    )
    assert item is not None
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    settings = ProjectSettings(width=1920, height=1080, frame_rate=FrameRate(60), sample_rate=44100)
    project = Project.create(settings)
    track = Track(kind=TrackKind.VIDEO, name="V1")
    project = AddTrack(track).apply(project)
    project = AddClip(track.id, item.clip).apply(project)
    renderer = FrameRenderer(project, context=context)
    try:
        for offset in offsets:
            ours = _shrunk(renderer.render(offset))
            theirs = _shrunk(reference[offset])
            assert float(np.abs(ours - theirs).mean()) < limit, offset
    finally:
        renderer.close()
        context.release()
