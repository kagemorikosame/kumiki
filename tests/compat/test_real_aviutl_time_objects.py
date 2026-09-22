"""AviUtl2 本体が書き出した絵と、移動軌跡・星空を突き合わせる

``tools/aviutl_compare.py`` で並べて AviUtl2 v2.1.6a に書き出させた ``.work/aviutl-p5``
（1920x1080 60fps）を読む 配布物の絵が入るのでリポジトリには入れていない 無ければ飛ばす

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

WORK = Path(__file__).resolve().parents[2] / ".work" / "aviutl-p5"
VIDEO = WORK / "aviutl.mp4"
MANIFEST = WORK / "manifest.json"

pytestmark = pytest.mark.skipif(
    not (VIDEO.exists() and MANIFEST.exists()), reason="AviUtl2 の書き出し（.work/aviutl-p5）が無い"
)


def _cases() -> dict[str, dict[str, object]]:
    cases = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    return {str(case["name"]): case for case in cases}


def _reference(start: int, offsets: list[int]) -> dict[int, np.ndarray]:
    av = pytest.importorskip("av")
    wanted = {start + offset for offset in offsets}
    found: dict[int, np.ndarray] = {}
    with av.open(str(VIDEO)) as container:
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
    case = _cases().get(name)
    if case is None:
        pytest.skip(f"{name} が並べた見本に無い")
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


@pytest.mark.parametrize("name", ["kumiki_p5_line_x", "kumiki_p5_line_xy"])
def test_the_trail_matches_aviutl(name: str) -> None:
    # 入り・真ん中・終わり 先端の向きは端で前後の片側しか見られないので、両端を必ず見る
    offsets = [0, 3, 40, 76, 80]
    case = _cases().get(name)
    if case is None:
        pytest.skip(f"{name} が並べた見本に無い")
    reference = _reference(int(str(case["start"])), offsets)
    ours = _ours(name, offsets)
    for offset in offsets:
        difference = np.abs(ours[offset] - reference[offset]).max(axis=2)
        # 実装前は何も描かず、線の画素がまるごと差になっていた（2 万画素が 255）
        # 移した後は縁の半画素だけが残る
        assert float(difference.mean()) < 0.2, offset
        assert int((difference > 64).sum()) < 600, offset


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


@pytest.mark.parametrize("name", ["kumiki_p5_star_n30", "kumiki_p5_star_n30_fast"])
def test_the_stars_flow_like_aviutl(name: str) -> None:
    # 実物は 30 個のうち 19 個前後が画面に入り、すべて外へ流れ、1 フレームで
    # 中心からの距離が 0.72%（速度 6）・1.46%（速度 12）伸びた
    offsets = list(range(0, 81, 2))
    case = _cases().get(name)
    if case is None:
        pytest.skip(f"{name} が並べた見本に無い")
    theirs = _flow(_reference(int(str(case["start"])), offsets))
    ours = _flow(_ours(name, offsets))
    assert ours[0] == pytest.approx(theirs[0], rel=0.3)
    assert ours[1] > 0.9 and theirs[1] > 0.9
    # 2 フレームおきに測っているので伸びは 2 倍前後 速さがずれると真っ先にここへ出る
    assert ours[2] == pytest.approx(theirs[2], rel=0.15)
