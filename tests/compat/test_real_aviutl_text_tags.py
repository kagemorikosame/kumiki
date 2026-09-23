"""AviUtl2 本体が書き出した制御文字入りのテキストと、Sashimono の絵を突き合わせる（Issue #108）

``tools/aviutl_tag_probes.py`` で並べ、``tools/aviutl2_export.py`` で AviUtl2 v2.1.6a に
連番の PNG で書き出させた ``.work/composite-font`` を読む 配布エイリアス
（``合成フォントテキスト``）が入るのでリポジトリには入れていない 無ければ飛ばす

見本は文字の枠を青で塗っている 字は白（赤の成分が 255）で青は赤の成分が 0 なので、
赤の成分だけを見れば字の形を、不透明な所を見れば枠を読める
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sashimono.compat.aviutl.exo import load_exo
from sashimono.compat.aviutl.mapping import map_object
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.core.timebase import FrameRate
from sashimono.engine import sources
from sashimono.engine.sources import render_source_framed

WORK = Path(__file__).resolve().parents[2] / ".work" / "composite-font"

#: 字の外形の食い違いの許し（画素） 描き方（Qt と DirectWrite）の縁のにじみで 1 画素は動く
INK_TOLERANCE = 1
#: 縁取りの付いた見本は縁の太らせ方（制御文字とは別の、前からの差）で 2 画素まで動く
WIDER = {"tag08_color": 2}
#: 枠を比べない見本 縁取り文字の枠は AviUtl2 では上へ 7 画素ほど高く、Sashimono は
#: 縁を枠に入れていない 制御文字とは別の差なので、ここでは字の外形だけを見る
FRAME_UNCHECKED = {"tag08_color"}
#: 測ったが描いていない見本 文字装飾の切り替え（``<@メイリオ,3>``）は書体だけを読む
NOT_DRAWN = {"tag21_font_decoration"}
DISTRIBUTED = "合成フォントテキスト"


def _manifest() -> list[dict[str, Any]]:
    manifest = WORK / "manifest.json"
    if not manifest.exists() or not any((WORK / "aviutl").glob("frame*.png")):
        pytest.skip("AviUtl2 に書き出させた .work/composite-font が無い")
    cases: list[dict[str, Any]] = json.loads(manifest.read_text(encoding="utf-8"))["cases"]
    return cases


def _case(name: str) -> dict[str, Any]:
    for case in _manifest():
        if case["name"] == name:
            return case
    pytest.skip(f"{name} を書き出した AviUtl2 の絵が無い")


def _reference(case: dict[str, Any]) -> np.ndarray:
    from PySide6.QtGui import QImage

    frame = int(case["start"]) + 2
    matches = [
        path
        for path in (WORK / "aviutl").glob("frame*.png")
        if int(path.stem.removeprefix("frame")) == frame
    ]
    assert matches, f"フレーム {frame} の PNG が無い"
    image = QImage(str(matches[0])).convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4)
    # 写してから返す QImage が消えると、見ていた画素の置き場も消えて読むと落ちる
    return pixels.copy()


def _box(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    assert len(xs), "字が無い"
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _ink(image: np.ndarray) -> np.ndarray:
    """字の所 赤の成分を不透明度で掛けて見る（黒の背景に置いたときの赤）"""
    return image[..., 0].astype(np.int32) * image[..., 3].astype(np.int32) // 255 > 128


def _probe_names() -> list[str]:
    try:
        cases = _manifest()
    except pytest.skip.Exception:
        return ["（書き出しが無い）"]
    return [case["name"] for case in cases if case["name"].startswith("tag")]


@pytest.fixture(autouse=True)
def japanese_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """書き出した機械（日本語の Windows）と同じく書体を日本語の名前で引く"""
    monkeypatch.setattr(sources, "_system_is_japanese", lambda: True)


@pytest.mark.parametrize("name", _probe_names())
def test_the_text_lands_where_aviutl2_drew_it(name: str) -> None:
    """字の外形と文字の枠が AviUtl2 と 1 画素以内で重なる

    制御文字を読まないと、タグが字として並んで外形が数百画素広がる 行の高さや
    ベースラインの決め方を違えると、上下が 7〜100 画素ずれる
    """
    if name in NOT_DRAWN:
        pytest.skip(f"{name} は測っただけで描いていない（文字装飾の切り替え）")
    case = _case(name)
    item = map_object(
        load_exo(Path(case["source"])).objects[0], FrameRate(60), report=CompatibilityReport()
    )
    assert item is not None and item.clip.source is not None
    ours, framed = render_source_framed(item.clip.source, 1920, 1080, frame=2, fps=60.0)
    assert ours is not None and framed is not None
    reference = _reference(case)

    tolerance = WIDER.get(name, INK_TOLERANCE)
    theirs_ink, ours_ink = _box(_ink(reference)), _box(_ink(ours))
    assert max(abs(a - b) for a, b in zip(theirs_ink, ours_ink, strict=True)) <= tolerance, (
        theirs_ink,
        ours_ink,
    )
    if name in FRAME_UNCHECKED:
        return
    # 枠は青く塗った不透明な所 縁の画素は半分だけ塗られるので、1 画素内側から数える
    theirs_frame = _box(reference[..., 3] == 255)
    ours_frame = tuple(float(value) for value in framed)
    assert max(abs(a - b) for a, b in zip(theirs_frame, ours_frame, strict=True)) <= 1.5, (
        theirs_frame,
        ours_frame,
    )


def test_the_distributed_alias_draws_like_the_literal_tags() -> None:
    """配布エイリアスが合成フォントで組み替えた絵は、同じタグを直に書いた見本と同じ

    AviUtl2 の側で確かめる 違えば、合成フォントのプラグインが見本と違うタグを返しており、
    見本の突き合わせでは配布エイリアスを確かめたことにならない
    """
    alias, literal = _case(DISTRIBUTED), _case("tag18_jp_comfont_literal")
    alias_ink = _ink(_reference(alias))
    literal_ink = _ink(_reference(literal))
    assert int((alias_ink != literal_ink).sum()) == 0
