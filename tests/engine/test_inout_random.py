"""ランダムに登場する 3 種の効き方（AviUtl の登場もの）

値の意味は AviUtl2 に見本を描かせ、**時間を追って**測った 登場の効果は途中の絵が
全部なので、進み具合ごとの位置と量を並べて読む
ここに並ぶ数はその実測から取ったもので、落ちたときに疑うのはこちらの実装の側
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import numpy as np
import pytest

from kumiki.core.model import (
    Clip,
    Effect,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from kumiki.core.timebase import FrameRate
from kumiki.effects import registry
from kumiki.effects.sources import SHAPE
from kumiki.effects.spec import ParamInput
from kumiki.engine.gpu import GLContextError, OffscreenGLContext
from kumiki.engine.render import FrameRenderer

WIDTH, HEIGHT = 200, 200
#: クリップの長さ（フレーム） 30fps なので 2 秒
DURATION = 60


@pytest.fixture(scope="module")
def gl() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture
def draw(gl: OffscreenGLContext) -> Callable[..., np.ndarray]:
    """真ん中の白い四角へ登場の効果を掛け、``frame`` の絵を返す"""

    def run(effect: Effect, frame: int) -> np.ndarray:
        source: GeneratedSource = SHAPE.create(
            shape="rect", width=60, height=20, color=(1.0, 1.0, 1.0, 1.0)
        )
        project = Project.create(
            ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(30))
        )
        track = Track(
            kind=TrackKind.VIDEO,
            clips=(Clip(timeline_start=0, duration=DURATION, source=source, effects=(effect,)),),
        )
        project = project.with_timeline(
            project.timeline.__class__(rate=project.rate, tracks=(track,))
        )
        renderer = FrameRenderer(project, context=gl)
        try:
            return renderer.render(frame)
        finally:
            renderer.close()

    return run


def _lit(image: np.ndarray) -> np.ndarray:
    return image[:, :, :3].max(axis=2) > 24


def _middle(image: np.ndarray) -> float | None:
    """光っている所の縦の重心 何も無ければ ``None``"""
    rows, _ = np.where(_lit(image))
    return float(rows.mean()) if len(rows) else None


class TestFall:
    """ランダム間隔で落ちながら登場 上から降りてきて濃くなる"""

    def _fall(self, **params: ParamInput) -> Effect:
        base: dict[str, ParamInput] = {
            "effect_in": True,
            "effect_out": False,
            "effect_time": 1.0,
            "easing": "linear",
            "easing_mode": "in",
            "distance_": 60.0,
            "interval": 0.0,
        }
        base.update(params)
        return registry.require("inout_fall").create(**base)

    def test_it_comes_down_from_above(self, draw: Callable[..., np.ndarray]) -> None:
        """**上から**降りてくる

        AviUtl2 に 距離 200 を描かせると 200px 上から降りてきた
        符号を逆にすると、下から浮き上がってくる別の動きになる
        """
        # 始まりのフレームは不透明度 0 なので、少し進んだ所で見る
        start = _middle(draw(self._fall(), 5))
        home = _middle(draw(self._fall(), DURATION - 1))
        assert start is not None and home is not None
        assert start < home - 20, "上から降りてきていない"

    def test_it_lands_on_time(self, draw: Callable[..., np.ndarray]) -> None:
        # 時間 1 秒 ＝ 30 フレームで着く 真ん中（15 フレーム）では途中にいる
        half = _middle(draw(self._fall(), 15))
        landed = _middle(draw(self._fall(), 30))
        home = _middle(draw(self._fall(), DURATION - 1))
        assert half is not None and landed is not None and home is not None
        assert landed == pytest.approx(home, abs=2)
        assert half < landed - 5

    def test_it_fades_in_while_falling(self, draw: Callable[..., np.ndarray]) -> None:
        # 落ちながら濃くなる 実測でも 118 → 205 → 239 と上がった
        early = draw(self._fall(), 5)
        late = draw(self._fall(), 25)
        # 明るさは色の側で見る 不透明度まで一緒に見ると、どちらも 255 で並ぶ
        assert int(early[..., :3][_lit(early)].max()) < int(late[..., :3][_lit(late)].max())

    def test_a_delay_holds_it_back(self, draw: Callable[..., np.ndarray]) -> None:
        # 間隔 を入れると落ち始めが遅れる 遅れが効かないと同じ絵になる
        prompt = _middle(draw(self._fall(interval=0.0), 20))
        waited = _middle(draw(self._fall(interval=1.0), 20))
        assert prompt is not None
        assert waited is None or waited < prompt


class TestBlink:
    """点滅して登場 半端な濃さは通らず、点くか消えるかのどちらか"""

    def _blink(self, **params: ParamInput) -> Effect:
        base: dict[str, ParamInput] = {
            "effect_in": True,
            "effect_out": False,
            "effect_time": 1.0,
            "easing": "linear",
            "easing_mode": "in",
            "interval": 5.0,
            "even": True,
        }
        base.update(params)
        return registry.require("inout_blink").create(**base)

    def test_it_turns_on_and_off(self, draw: Callable[..., np.ndarray]) -> None:
        # 登場のあいだに点いている絵と消えている絵の両方が出る
        seen = {bool(_lit(draw(self._blink(), frame)).any()) for frame in range(0, 30)}
        assert seen == {True, False}, "点滅していない"

    def test_it_is_solid_once_it_has_arrived(self, draw: Callable[..., np.ndarray]) -> None:
        # 時間を過ぎたら点滅しない ここが残ると、ずっとちらつく
        assert all(bool(_lit(draw(self._blink(), frame)).any()) for frame in range(35, 60, 5))

    def test_it_is_never_half_lit(self, draw: Callable[..., np.ndarray]) -> None:
        """点いているときは元の明るさのまま

        薄く出す作りにすると、点滅ではなく溶け込み（フェード）になる
        AviUtl2 の実物も、見えるときは 239 のままだった
        """
        full = draw(self._blink(), DURATION - 1)
        top = int(full[_lit(full)].max())
        for frame in range(0, 30):
            image = draw(self._blink(), frame)
            if _lit(image).any():
                assert int(image[_lit(image)].max()) == pytest.approx(top, abs=3)

    def test_an_even_interval_repeats(self, draw: Callable[..., np.ndarray]) -> None:
        # 一定にする を付けると、間隔ごとにきっちり入れ替わる
        lit = [bool(_lit(draw(self._blink(interval=5.0), frame)).any()) for frame in range(0, 20)]
        assert lit[0:5] == lit[10:15]
        assert lit[0:5] != lit[5:10]


class TestRandomDirection:
    """ランダム方向から登場 画面の外から飛んでくる"""

    def _fly(self, **params: ParamInput) -> Effect:
        base: dict[str, ParamInput] = {
            "effect_in": True,
            "effect_out": False,
            "effect_time": 1.0,
            "easing": "linear",
            "easing_mode": "in",
            "spin": 0.0,
            "light": 0.0,
        }
        base.update(params)
        return registry.require("inout_random_direction").create(**base)

    def test_it_is_away_at_the_start_and_home_at_the_end(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        assert not _lit(draw(self._fly(), 0)).any(), "始めから画面に居座っている"
        assert _lit(draw(self._fly(), DURATION - 1)).any(), "着いていない"

    def test_the_seed_changes_the_direction(self, draw: Callable[..., np.ndarray]) -> None:
        # 向きが種で変わらないと、並べたときに全部同じ方向から飛んでくる
        middles = {
            tuple(np.round(np.mean(np.where(_lit(draw(self._fly(seed=seed), 20))), axis=1), 1))
            if _lit(draw(self._fly(seed=seed), 20)).any()
            else None
            for seed in (0, 1, 2, 3)
        }
        assert len(middles) > 1

    def test_the_light_brightens_the_flight(self, draw: Callable[..., np.ndarray]) -> None:
        # ライト は飛んでいるあいだの明るさの足し算 着いたら効かない
        plain = draw(self._fly(light=0.0), DURATION - 1)
        lit_up = draw(self._fly(light=80.0), DURATION - 1)
        assert int(np.abs(plain.astype(np.int16) - lit_up.astype(np.int16)).max()) <= 2
