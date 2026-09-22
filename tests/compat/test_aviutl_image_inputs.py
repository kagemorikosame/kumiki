"""AviUtl2 の 画像合成 と 縁取りの パターン画像

項目の名前と既定値は、AviUtl2 v2.1.6a に ``effect.name=画像合成`` だけを置かせて
埋めさせた（X=0.0 Y=0.0 拡大率=100.00 合成モード=色情報を上書き 画像= ループ画像=1
ループ再生=1） 絵の置き方は ``kumiki_p5_blend_*`` の書き出しから測った

後半は ``tests/fixtures/aviutl/probes`` の実物（配布物ではなく AviUtl2 に作らせた
見本）を通す 手元に無ければ飛ばす
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from sashimono.compat.aviutl.exo import load_exo, parse_exo
from sashimono.compat.aviutl.mapping import map_object
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import place
from sashimono.core.model import AnimatedValue, Effect, Project, ProjectSettings
from sashimono.core.timebase import FrameRate

RATE = FrameRate(60)

PROBES = Path(__file__).resolve().parent.parent / "fixtures" / "aviutl" / "probes"


def _alias(block: str) -> str:
    return (
        "[Object]\nframe=0,59\n[Object.0]\neffect.name=テキスト\nテキスト=あ\n"
        f"[Object.1]\n{block}\n"
        "[Object.2]\neffect.name=標準描画\nX=0.00\n合成モード=通常\n"
    )


def _mapped(block: str) -> tuple[Effect, CompatibilityReport]:
    report = CompatibilityReport()
    item = map_object(parse_exo(_alias(block)).objects[0], RATE, report=report)
    assert item is not None
    assert len(item.clip.effects) == 1, [effect.kind for effect in item.clip.effects]
    return item.clip.effects[0], report


def _number(effect: Effect, name: str) -> float:
    value = effect.params[name]
    assert isinstance(value, AnimatedValue)
    return value.static


def _blend_block(image: str, *, loop: str = "1", mode: str = "色情報を上書き") -> str:
    return (
        "effect.name=画像合成\nX=50.0\nY=30.0\nGroup=1\n拡大率=50.00\n"
        f"合成モード={mode}\n画像={image}\nループ画像={loop}\nループ再生=1"
    )


@pytest.fixture
def picture(tmp_path: Path) -> Path:
    # 中身は読まない（写すときはあるかどうかと拡張子だけを見る）
    path = tmp_path / "模様.png"
    path.write_bytes(b"")
    return path


class TestImageBlend:
    def test_every_item_is_carried_over(self, picture: Path) -> None:
        # 以前はフィルタごと記録に回り、文字がそのままの色で出ていた
        effect, report = _mapped(_blend_block(str(picture), loop="0"))
        assert effect.kind == "image_blend"
        assert effect.params["image_file"] == str(picture)
        assert _number(effect, "offset_x") == 50.0
        assert _number(effect, "zoom") == 50.0
        assert effect.params["loop"] is False
        assert effect.params["blend"] == "overwrite"
        assert report.is_empty, report.lines()

    def test_y_is_flipped_to_up_positive(self, picture: Path) -> None:
        # AviUtl2 で Y=30 は画像を**下へ** 30 動かした こちらは上が正
        effect, _ = _mapped(_blend_block(str(picture)))
        assert _number(effect, "offset_y") == -30.0

    @pytest.mark.parametrize(
        ("name", "mode"),
        [
            ("前方から合成", "front"),
            ("後方から合成", "back"),
            ("色情報を上書き", "overwrite"),
            ("輝度をアルファ値として上書き", "luma_alpha"),
            ("輝度をアルファ値として乗算", "luma_multiply"),
        ],
    )
    def test_every_blend_mode_is_carried_over(self, picture: Path, name: str, mode: str) -> None:
        # 名前は AviUtl2 v2.1.6a の一覧から読んだ 表に無いと上書きとして描かれ、
        # 後方から合成の文字が画像で塗り潰される
        effect, report = _mapped(_blend_block(str(picture), mode=name))
        assert effect.params["blend"] == mode
        assert report.is_empty, report.lines()

    def test_an_unknown_blend_mode_is_recorded(self, picture: Path) -> None:
        # 名前と絵を確かめていない合成を上書きとして描くと、黙って違う絵が出る
        _, report = _mapped(_blend_block(str(picture), mode="未知の合成"))
        assert any("合成モード" in line for line in report.lines()), report.lines()

    def test_a_missing_image_is_recorded(self, tmp_path: Path) -> None:
        # エイリアスは作った人の機械のパスを持つ 描くときは画像なしで描くので、
        # 記録しないと模様が消えたことに気付けない
        _, report = _mapped(_blend_block(str(tmp_path / "無い.png")))
        assert any("画像が見つからない" in line for line in report.lines()), report.lines()

    def test_looping_playback_only_matters_for_movies(self, tmp_path: Path) -> None:
        # 静止画ならループ再生で絵は変わらない 動画は先頭のコマしか使わないので記録する
        movie = tmp_path / "動き.mp4"
        movie.write_bytes(b"")
        _, report = _mapped(_blend_block(str(movie)))
        lines = report.lines()
        assert any("静止画でない" in line for line in lines), lines
        assert any("ループ再生" in line for line in lines), lines

    def test_looping_playback_on_a_filter_without_images_stays_recorded(self) -> None:
        # 画像を読まないフィルタの ループ再生 まで写せたことにすると、
        # 写していない項目が記録から消える
        _, report = _mapped("effect.name=ぼかし\n範囲=5\nループ再生=1")
        assert any("ループ再生" in line for line in report.lines()), report.lines()

    def test_an_empty_image_is_not_recorded(self) -> None:
        # AviUtl2 が既定で入れる 画像= のまま 何も読まないので写せていない物は無い
        _, report = _mapped(_blend_block(""))
        assert report.is_empty, report.lines()


class TestBorderPattern:
    def test_the_pattern_image_is_carried_over(self, picture: Path) -> None:
        # 以前は「縁取りの項目: パターン画像」として記録に回り、縁が単色で出ていた
        effect, report = _mapped(
            f"effect.name=縁取り\nサイズ=20\nぼかし=0\n縁色=ffffff\nパターン画像={picture}"
        )
        assert effect.kind == "border"
        assert effect.params["pattern"] == str(picture)
        assert report.is_empty, report.lines()

    def test_an_empty_pattern_is_not_recorded(self) -> None:
        # 手元の配布物 37 本の縁取りは、どれも パターン画像= が空
        _, report = _mapped("effect.name=縁取り\nサイズ=4\nぼかし=0\n縁色=000000\nパターン画像=")
        assert report.is_empty, report.lines()


class TestGradientMapPattern:
    def test_the_pattern_image_is_carried_over(self, picture: Path) -> None:
        # 以前は「グラデーションマップの項目: パターン画像」として記録に回り、
        # 画像の色ではなく暗部色から明部色の帯で塗られていた
        effect, report = _mapped(
            "effect.name=グラデーションマップ\n強さ=100.0\n暗部色=000000\n明部色=ffffff\n"
            f"パターン画像={picture}"
        )
        assert effect.kind == "gradient_map"
        assert effect.params["pattern"] == str(picture)
        assert report.is_empty, report.lines()


# --- AviUtl2 に作らせた見本 ---


def _probe(name: str) -> Path:
    path = PROBES / f"{name}.object"
    if not path.exists():
        pytest.skip(f"{path.name} が手元に無い")
    return path


@pytest.fixture(scope="module")
def renderer() -> Iterator[object]:
    from sashimono.engine.gpu import GLContextError, OffscreenGLContext
    from sashimono.engine.render import FrameRenderer

    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    settings = ProjectSettings(width=1920, height=1080, frame_rate=RATE)
    made = FrameRenderer(Project.create(settings), context=context)
    yield made
    made.close()
    context.release()


def _render(renderer: object, name: str) -> np.ndarray:
    from sashimono.engine.render import FrameRenderer

    assert isinstance(renderer, FrameRenderer)
    path = _probe(name)
    report = CompatibilityReport()
    settings = ProjectSettings(width=1920, height=1080, frame_rate=RATE)
    objects = [
        item
        for obj in load_exo(path).objects
        if (item := map_object(obj, RATE, report=report)) is not None
    ]
    for item in objects:
        for effect in item.clip.effects:
            image = effect.params.get("image_file", effect.params.get("pattern"))
            if isinstance(image, str) and image and not Path(image).exists():
                pytest.skip(f"見本の画像 {image} が手元に無い")
    project = Project.create(settings)
    for command in place(objects, project):
        project = command.apply(project)
    renderer.set_project(project)
    assert report.is_empty, report.lines()
    return renderer.render(10)


def _lit_columns(image: np.ndarray) -> np.ndarray:
    return np.flatnonzero(image[..., :3].max(axis=2).max(axis=0) > 40)


class TestRealProbes:
    def test_the_image_colours_the_text(self, renderer: object) -> None:
        # 田田田（白）に 4 色の画像を上書きした 真ん中の字の左上は赤、右上は緑
        # （AviUtl2 の書き出しでは、画像の中心が文字の中心に来ていた）
        image = _render(renderer, "kumiki_p5_blend_image")
        top = image[480, 900:1020, :3].astype(int)
        assert (top[:40, 0] > 200).any() and (top[:40, 1] < 60).all(), "左上が赤でない"
        assert (top[-40:, 1] > 200).any() and (top[-40:, 0] < 60).all(), "右上が緑でない"

    def test_without_loop_only_the_middle_glyph_is_left(self, renderer: object) -> None:
        # ループ画像=0 AviUtl2 では 200 画素の画像に重なる真ん中の字だけが残った
        columns = _lit_columns(_render(renderer, "kumiki_p5_blend_noloop"))
        assert columns.size > 0
        assert columns.min() >= 855 and columns.max() <= 1065, (columns.min(), columns.max())

    def test_the_pattern_starts_outside_the_border(self, renderer: object) -> None:
        # 200 の四角形（860..1060）に縁 10 AviUtl2 では上の縁が 850 から赤、
        # 950 から緑、1050 から赤に変わった 起点を四角形の左端にすると 10 ずれる
        image = _render(renderer, "kumiki_p6_border_pattern_rect10")
        row = image[435, :, :3].astype(int)
        assert row[900, 0] > 200 and row[900, 1] < 60, "850..950 が赤でない"
        assert row[1000, 1] > 200 and row[1000, 0] < 60, "950..1050 が緑でない"
        assert row[1060, 0] > 200 and row[1060, 1] < 60, "1050 から赤に戻っていない"

    def test_the_border_colour_does_not_tint_the_pattern(self, renderer: object) -> None:
        # 縁色は赤 AviUtl2 では緑の所も緑のまま（赤と混ざらない）
        image = _render(renderer, "kumiki_p6_border_pattern_red")
        # 縁 20 の上の帯（420..440）
        green = image[430, 1000, :3].astype(int)
        assert green[1] > 200 and green[0] < 60, green

    def test_the_gradient_map_reads_the_pattern(self, renderer: object) -> None:
        # 黒から白の帯に、横で色相の変わる画像 左は赤、右は紫になった
        image = _render(renderer, "kumiki_p6_gradmap_pattern")
        left = image[540, 20, :3].astype(int)
        right = image[540, 1900, :3].astype(int)
        assert left[0] > 100 and left[1] < 40 and left[2] < 40, left
        assert right[0] > 100 and right[2] > 100 and right[1] < 40, right

    def test_front_covers_the_box_and_back_keeps_the_text(self, renderer: object) -> None:
        # 前方は文字の上に画像、後方は文字の下に画像 どちらも文字の枠いっぱいに出る
        front = _render(renderer, "kumiki_p7_blend_front")
        back = _render(renderer, "kumiki_p7_blend_back")
        # 真ん中の字の上の横棒（白い文字の上）
        assert front[476, 930, 0] > 200 and front[476, 930, 1] < 60, "前方で文字が見えている"
        assert back[476, 930, :3].min() > 200, "後方で文字が画像に隠れた"

    def test_the_border_is_painted_with_the_pattern(self, renderer: object) -> None:
        # 縁色は白 模様が効いていなければ縁は白一色になる
        image = _render(renderer, "kumiki_p5_border_pattern")
        rim = image[455:465, 700:1220, :3].astype(int)
        lit = rim[rim.max(axis=2) > 100]
        assert lit.size > 0
        saturated = (lit.max(axis=1) - lit.min(axis=1)) > 150
        assert saturated.mean() > 0.5, "縁が模様ではなく単色で塗られている"
