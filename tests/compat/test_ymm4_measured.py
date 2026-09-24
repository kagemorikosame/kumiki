"""YMM4 本体の書き出しで測った値に、読み込んで描いた絵が合うか

値はどれも 2026-09-24 に YMM4 4.56.1.1 で ``tools/ymm4_compare.py`` の探りを書き出して
測った物（``zoom-build`` と ``effectitem-build`` 1920x1080・30fps） 探りと同じ形の
アイテムをここで組み、同じ読み込み（``map_template``）と描画を通す

* 拡大率 100% の画像と動画は素材の画素の大きさで置かれる（Issue #159）
* エフェクトアイテムは下の絵の透明な所も黒として掛かる 拡大と位置のエフェクトは
  絵を変えない（Issue #143）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import gather_media, place
from sashimono.compat.ymm4.template import map_template
from sashimono.core.model import MediaItem, Project, ProjectSettings
from sashimono.core.timebase import FrameRate
from sashimono.engine.decode import ProbeError, probe_media
from sashimono.engine.render import FrameRenderer

pytestmark = pytest.mark.usefixtures("gpu")

WIDTH, HEIGHT = 1920, 1080

#: 素材の真ん中に置いた印（素材の縦横の半分）が、YMM4 の書き出しに出た大きさ
#: ``(素材の幅, 素材の高さ, 拡大率) -> (印の幅, 印の高さ)`` 画面からはみ出す分は切れた後
#: 画面に収める読みなら 640x360 は 960x540、3840x2160 は 960x540、縦長は 304x540 になる
MEASURED_MARKS: dict[tuple[int, int, float], tuple[int, int]] = {
    (640, 360, 100.0): (320, 180),
    (3840, 2160, 100.0): (1920, 1080),
    (360, 640, 100.0): (180, 320),
    (640, 360, 200.0): (640, 360),
}
GROUND = (40, 90, 230)
MARK = (235, 40, 40)

#: エフェクトアイテムの探りで、YMM4 の書き出しの左上の角（周りが透明な所）の色
#: 下地は画面の真ん中の 640x360 の図形 周りには何も無い
MEASURED_CORNERS: dict[str, tuple[int, int, int]] = {
    # 黒を反転して白 フィルタの読み（透明は透明のまま）なら黒のまま
    "invert": (255, 255, 255),
    # 反転を不透明度 50 で重ねた 黒と白の半々
    "invert_half": (129, 126, 130),
    # 前景の塗りつぶし（#2C7AE0・不透明度 50・通常） 黒い所まで塗られた
    "fill": (22, 61, 114),
}
#: 色の差の許し 書き出しの圧縮と色の変換の揺れ
COLOUR_TOLERANCE = 8


def _still(value: float) -> dict[str, Any]:
    return {"Values": [{"Value": value}], "Span": 0.0, "AnimationType": "なし"}


def _write_png(path: Path, image: np.ndarray) -> Path:
    from PySide6.QtGui import QImage

    height, width = image.shape[:2]
    data = np.ascontiguousarray(image, dtype=np.uint8)
    qimage = QImage(data.tobytes(), width, height, width * 3, QImage.Format.Format_RGB888)
    assert qimage.save(str(path))
    return path


def _marked_picture(directory: Path, width: int, height: int) -> Path:
    """探りと同じ素材 地の色の真ん中に縦横半分の印"""
    image = np.empty((height, width, 3), dtype=np.uint8)
    image[...] = GROUND
    top, left = height // 4, width // 4
    image[top : top + height // 2, left : left + width // 2] = MARK
    return _write_png(directory / f"印 {width}x{height}.png", image)


def _image_item(path: Path, *, zoom: float = 100.0, layer: int = 0) -> dict[str, Any]:
    """探り（``tools/ymm4_compare.py`` の ``image_item``）と同じ形の画像アイテム"""
    return {
        "$type": "YukkuriMovieMaker.Project.Items.ImageItem, YukkuriMovieMaker",
        "FilePath": str(path),
        "X": _still(0.0),
        "Y": _still(0.0),
        "Z": _still(0.0),
        "Opacity": _still(100.0),
        "Zoom": _still(zoom),
        "Rotation": _still(0.0),
        "Blend": "Normal",
        "IsInverted": False,
        "VideoEffects": [],
        "Frame": 0,
        "Layer": layer,
        "Length": 30,
        "PlaybackRate": 100.0,
    }


def _shape_item() -> dict[str, Any]:
    """探りの下地（``base_shape``）と同じ 真ん中の 640x360 の橙の四角 周りは透明"""
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
                    "$type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushParameter, "
                    "YukkuriMovieMaker",
                    "Color": "#FFE08A2C",
                },
            },
        },
        "X": _still(0.0),
        "Y": _still(0.0),
        "Opacity": _still(100.0),
        "Zoom": _still(100.0),
        "Rotation": _still(0.0),
        "Blend": "Normal",
        "Frame": 0,
        "Layer": 0,
        "Length": 30,
    }


def _effect(name: str, **values: Any) -> dict[str, Any]:
    return {
        "$type": f"YukkuriMovieMaker.Project.Effects.{name}, YukkuriMovieMaker",
        **values,
        "IsEnabled": True,
    }


def _effect_item(*effects: dict[str, Any], opacity: float = 100.0) -> dict[str, Any]:
    """配布物（トーン調整Te）の実物と同じ形のエフェクトアイテム 範囲は画面全体"""
    return {
        "$type": "YukkuriMovieMaker.Project.Items.EffectItem, YukkuriMovieMaker",
        "ShapeType2": "YukkuriMovieMaker.Shape.BackgroundShapePlugin, YukkuriMovieMaker",
        "ShapeParameter": {
            "$type": "YukkuriMovieMaker.Project.Items.BackgroundShapeParameter, YukkuriMovieMaker",
            "StrokeThickness": _still(4000.0),
        },
        "Blur": _still(0.0),
        "InvertMask": False,
        "VideoEffects": list(effects),
        "X": _still(0.0),
        "Y": _still(0.0),
        "Opacity": _still(opacity),
        "Rotation": _still(0.0),
        "Blend": "Normal",
        "Frame": 0,
        "Layer": 1,
        "Length": 30,
    }


def _probe(path: Path) -> MediaItem | None:
    try:
        return probe_media(path)
    except ProbeError:
        return None


def _render(items: list[dict[str, Any]]) -> np.ndarray:
    """探りの measure と同じ道（写す・素材を登録する・置く・描く）で真ん中の 1 枚を描く"""
    objects = map_template(items, report=CompatibilityReport())
    project = Project.create(ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(30)))
    plan = gather_media(objects, project, _probe)
    assert not plan.missing
    for command in [*plan.commands, *place(objects, project, at_frame=0, media=plan.media)]:
        project = command.apply(project)
    renderer = FrameRenderer(project)
    try:
        return renderer.render(15)[..., :3]
    finally:
        renderer.close()


def _mark_size(picture: np.ndarray) -> tuple[int, int]:
    """印の色が占める矩形の幅と高さ 印・地・黒のうち近い色で読む（探りの ``mark_box``）"""
    colours = np.asarray([MARK, GROUND, (0, 0, 0)], dtype=np.float32)
    distance = np.abs(picture.astype(np.float32)[..., None, :] - colours).sum(axis=-1)
    ys, xs = np.nonzero(distance.argmin(axis=-1) == 0)
    return int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)


def _near(actual: np.ndarray, expected: tuple[int, int, int]) -> bool:
    return bool(np.all(np.abs(actual.astype(int) - np.asarray(expected)) <= COLOUR_TOLERANCE))


class TestPlacedSize:
    @pytest.mark.parametrize(("key", "mark"), list(MEASURED_MARKS.items()))
    def test_a_picture_is_placed_at_its_own_pixels_like_ymm4(
        self, tmp_path: Path, key: tuple[int, int, float], mark: tuple[int, int]
    ) -> None:
        """画面と違う大きさの画像が、YMM4 と同じ大きさで出る

        画面に収めて置くと、640x360 の印が 960x540 になる 拡大率 200 は、縦の拡大率へも
        拡大率を入れていたころ縦だけ 2 回掛かり、印が 640x720 になっていた
        """
        width, height, zoom = key
        picture = _render([_image_item(_marked_picture(tmp_path, width, height), zoom=zoom)])
        assert _mark_size(picture) == mark


class TestEffectItem:
    @pytest.mark.parametrize(
        ("name", "item"),
        [
            ("invert", _effect_item(_effect("InvertEffect"))),
            ("invert_half", _effect_item(_effect("InvertEffect"), opacity=50.0)),
            (
                "fill",
                _effect_item(
                    _effect(
                        "FillForegroundEffect",
                        Opacity=_still(50.0),
                        BlendMode="Normal",
                        IsBrushOnly=False,
                        Brush={
                            "Type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushPlugin, "
                            "YukkuriMovieMaker",
                            "Parameter": {
                                "$type": "YukkuriMovieMaker.Plugin.Brush."
                                "SolidColorBrushParameter, YukkuriMovieMaker",
                                "Color": "#FF2C7AE0",
                            },
                        },
                    )
                ),
            ),
        ],
    )
    def test_the_transparent_part_below_is_worked_on_as_black(
        self, name: str, item: dict[str, Any]
    ) -> None:
        """図形の周りの透明な所にも、黒として掛かる

        フィルタのクリップ（下の絵に掛けて置き換える）で読むと周りが透明のまま残り、
        書き出すと黒になる YMM4 は反転で白、塗りつぶしで青くした
        """
        picture = _render([_shape_item(), item])
        corner = picture[50:150, 50:150].reshape(-1, 3).mean(axis=0)
        assert _near(corner, MEASURED_CORNERS[name]), f"周りの色 {corner.round()}"

    @pytest.mark.parametrize(
        "effect",
        [
            _effect("ZoomEffect", Zoom=_still(50.0), ZoomX=_still(100.0), ZoomY=_still(100.0)),
            _effect("DrawPositionEffect", X=_still(300.0), Y=_still(0.0), Z=_still(0.0)),
        ],
    )
    def test_zoom_and_position_leave_the_picture_as_it_is(
        self, tmp_path: Path, effect: dict[str, Any]
    ) -> None:
        """拡大率 50 も X 300 も、YMM4 の書き出しは下の絵のままだった

        写すと、縮めた・ずらした写しが下の絵の上に重なる（YMM4 との差 16.8 と 83.7）
        """
        below = _image_item(_marked_picture(tmp_path, WIDTH, HEIGHT))
        alone = _render([below])
        worked = _render([below, _effect_item(effect)])
        assert float(np.abs(alone.astype(int) - worked.astype(int)).mean()) < 1.0
