"""再生ボタンの並び

印を文字で出すと、書体によっては絵文字で描かれる（Windows の ``⏸`` は青い四角の絵）
ほかのボタンと揃わず、何のボタンかも読めなくなる 印は自前で描き、描いた絵で確かめる
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QPushButton

from sashimono.core.timebase import FrameRate
from sashimono.ui.theme import Colors
from sashimono.ui.transport import TransportBar, transport_icon

_NAMES = ("to_start", "back", "play", "pause", "forward", "to_end")


@pytest.fixture
def bar(qt_application: QApplication) -> Iterator[TransportBar]:
    del qt_application
    created = TransportBar(FrameRate(30))
    yield created
    created.close()


def _image(name: str) -> QImage:
    return transport_icon(name).pixmap(64, 64).toImage()


def _opaque_colors(image: QImage) -> set[str]:
    """ほぼ不透明な画素の色 縁のにじみ（半透明）は数えない"""
    return {
        image.pixelColor(x, y).name()
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 250
    }


class TestTransportButtons:
    def test_no_button_relies_on_a_font(self, bar: TransportBar) -> None:
        # 文字で出すと、⏸ が絵文字の書体で描かれて揃わなかった（Issue #27）
        bar.set_playing(True)
        buttons = bar.findChildren(QPushButton)
        assert len(buttons) == 5
        for button in buttons:
            assert button.text() == ""
            assert not button.icon().isNull()

    def test_playing_swaps_the_mark_and_back(self, bar: TransportBar) -> None:
        # 再生中に印が変わらないと、止めるボタンなのか始めるボタンなのか分からない
        play = bar._play
        before = play.icon().pixmap(32, 32).toImage()
        bar.set_playing(True)
        during = play.icon().pixmap(32, 32).toImage()
        bar.set_playing(False)
        after = play.icon().pixmap(32, 32).toImage()
        assert during != before
        assert after == before
        assert bar.playing is False

    def test_pause_is_two_bars(self) -> None:
        # 2 本の棒に見えないと、再生中の印が再生の印と見分けられず、押すと止まるのか
        # 分からない 真ん中の行を左から見て、塗り→隙間→塗り の 2 本であること
        image = _image("pause")
        row = image.height() // 2
        runs: list[bool] = []
        for x in range(image.width()):
            filled = image.pixelColor(x, row).alpha() > 128
            if not runs or runs[-1] != filled:
                runs.append(filled)
        assert runs.count(True) == 2

    def test_every_mark_has_the_same_colour(self) -> None:
        # 1 つだけ別の色（絵文字の青など）になると、並べたときに浮く
        for name in _NAMES:
            assert _opaque_colors(_image(name)) == {Colors.TEXT.name()}, name

    def test_the_marks_point_the_right_way(self) -> None:
        # 先頭へ・戻るは左向き、再生・進む・末尾へは右向き 向きを取り違えると、
        # 押したときに逆へ飛ぶように見える 三角の尖った側は、塗りが細い
        def weight(image: QImage, columns: range) -> int:
            return sum(
                1
                for x in columns
                for y in range(image.height())
                if image.pixelColor(x, y).alpha() > 128
            )

        for name, pointing_left in (
            ("to_start", True),
            ("back", True),
            ("play", False),
            ("forward", False),
            ("to_end", False),
        ):
            image = _image(name)
            # 縦棒のある端を外し、三角だけが乗る真ん中の幅で左右を比べる
            left = weight(image, range(24, 32))
            right = weight(image, range(32, 40))
            assert (left < right) is pointing_left, name
