"""時間を追って形が決まる図形（移動軌跡と星空）

形の決まりは AviUtl2 本体の ``script.obj2`` の式と、書き出した絵を測った結果から取った
ここが落ちたときに疑うのは実測の値ではなく、移した式の側
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pytest

from kumiki.core.model import AnimatedValue, GeneratedSource, Keyframe, ParamValue
from kumiki.engine.motion_shapes import StarField, Trail, TrailPath, star_field, trail, trail_path
from kumiki.engine.sources import render_source

WIDTH, HEIGHT = 1920, 1080


def _moving_right(at: float) -> tuple[float, float]:
    """-600 から 600 へ 80 フレームで動く（実物の見本 kumiki_p5_line_x と同じ）"""
    return -600.0 + 1200.0 * min(max(at, 0.0), 80.0) / 80.0, 0.0


def _trail(
    position: Callable[[float], tuple[float, float]] = _moving_right,
    path: TrailPath | None = None,
    **overrides: float,
) -> Trail:
    settings: dict[str, float] = {
        "frame": 20.0,
        "total": 81.0,
        "line_width": 16.0,
        "interval": 10.0,
        "min_step": 2.0,
        "fixed_speed": 0.0,
        "head_size": 48.0,
        "head_angle": 0.0,
        "head_offset": 70.0,
    }
    settings.update(overrides)
    return trail(position, path=path, **settings)


class TestTrail:
    def test_the_line_runs_from_the_start_to_the_current_position(self) -> None:
        # 通った跡でなく今の位置だけを描くと、ただの点が動いているだけになる
        shape = _trail()
        assert shape.stamps[0] == pytest.approx((-600.0, 0.0))
        assert shape.last == pytest.approx((-300.0, 0.0))
        assert max(x for x, _ in shape.stamps) <= -300.0 + 1e-6

    def test_the_stamps_are_as_far_apart_as_the_step(self) -> None:
        # 間隔は ライン幅 × 描画間隔 / 100 最小間隔より細かくしない（16 × 10% → 2）
        shape = _trail()
        gaps = np.diff([x for x, _ in shape.stamps])
        assert gaps == pytest.approx(np.full(len(gaps), 2.0))

    def test_a_wide_interval_makes_a_dotted_line(self) -> None:
        # 間隔を広げると点線になる 折れ線で引くと、この設定が効かなくなる
        # 16 × 500% → 80 画素おき
        shape = _trail(interval=500.0)
        gaps = np.diff([x for x, _ in shape.stamps])
        assert gaps == pytest.approx(np.full(len(gaps), 80.0))

    def test_the_head_points_the_way_it_moves(self) -> None:
        # 先端は進む向きへ 先端位置補正 70% で大きさの 2 割だけ先に出る
        # 右へ動くなら、上向きの三角形を時計回りに 90 度回す
        shape = _trail()
        assert shape.head == pytest.approx((-300.0 + 48.0 * 0.2, 0.0))
        assert shape.head_turn == pytest.approx(math.pi / 2.0)

    def test_the_head_of_a_still_object_points_up(self) -> None:
        # 動かないときは向きが決まらない 実物は上向き（Y は下が正なので上は負）
        shape = _trail(lambda _at: (0.0, 0.0))
        assert shape.head == pytest.approx((0.0, -48.0 * 0.2))
        assert shape.head_turn == pytest.approx(0.0)

    def test_a_fixed_speed_grows_by_pixels_per_frame(self) -> None:
        # 固定速度は 1 フレームにその画素ずつ 動きの時刻どおりに伸ばすと、
        # 動きの速さに関係なく一定の速さで線を引く使い方ができない
        shape = _trail(fixed_speed=5.0, frame=10.0)
        assert shape.last[0] == pytest.approx(-600.0 + 50.0, abs=2.0)

    def test_a_long_still_clip_does_not_walk_every_frame(self) -> None:
        # 止まっている区間は点が増えないので点の上限が効かず、クリップ頭から
        # 今までの位置を毎フレーム全部引いていた（長いクリップの再生が二乗で重くなる）
        # 道は 1 度だけ引いて覚え、フレームごとには先端の向きを探す分しか引かない
        calls = 0

        def still(_at: float) -> tuple[float, float]:
            nonlocal calls
            calls += 1
            return 0.0, 0.0

        path = trail_path(still, 100_000.0)
        calls = 0
        for frame in range(50_000, 50_100):
            _trail(still, frame=float(frame), total=100_000.0, path=path)
        assert calls < 100 * 2 * 2400 + 1000

    def test_a_long_fixed_speed_trail_keeps_growing(self) -> None:
        # 位置を引くフレームを 2 万で打ち切ると、固定速度の長いクリップで
        # 5 分半より後の伸びが止まり、先端が途中に残っていた
        def walking(at: float) -> tuple[float, float]:
            return at, 0.0

        shape = _trail(walking, frame=30_000.0, total=40_000.0, fixed_speed=1.0)
        assert shape.last[0] == pytest.approx(30_000.0, abs=1.0)

    def test_the_stamps_match_walking_frame_by_frame(self) -> None:
        # 道のりを先に数えて円を置いても、実物の 1 フレームずつ進める置き方と
        # 同じ所に来る（曲がった道で、間隔 10 画素の円が道のりの 10 の倍数に並ぶ）
        def curve(at: float) -> tuple[float, float]:
            return 100.0 * math.cos(at / 10.0), 100.0 * math.sin(at / 10.0)

        shape = _trail(curve, frame=30.0, total=60.0, interval=62.5)
        path = trail_path(curve, 60.0)
        for index, (x, y) in enumerate(shape.stamps[:20]):
            distance = index * 10.0
            frame = float(np.interp(distance, path.reach, np.arange(len(path.reach))))
            base = math.floor(frame)
            share = frame - base
            ax, ay = curve(float(base))
            bx, by = curve(float(base + 1))
            assert (x, y) == pytest.approx((ax + (bx - ax) * share, ay + (by - ay) * share))

    def test_a_broken_motion_does_not_hang(self) -> None:
        # 無限の彼方へ飛ぶ値でも、点の数の上限で止まる
        shape = _trail(lambda at: (at * 1e9, 0.0), frame=80.0)
        assert len(shape.stamps) <= 20000


class TestStarField:
    def _field(self, seconds: float, **overrides: float) -> StarField:
        settings: dict[str, float] = {
            "seconds": seconds,
            "count": 30.0,
            "speed": 6.0,
            "spread": 12.0,
            "depth": 20.0,
            "fade_in": 0.15,
            "fade_out": 0.15,
            "screen_width": float(WIDTH),
            "screen_height": float(HEIGHT),
        }
        settings.update(overrides)
        count = settings.pop("count")
        return star_field(count=count, **settings)

    def test_the_same_time_gives_the_same_stars(self) -> None:
        # 描くたびに位置が変わると、止めたプレビューでも星がちらつく
        first, second = self._field(0.5), self._field(0.5)
        assert np.array_equal(first.x, second.x)

    def test_stars_come_towards_the_camera(self) -> None:
        # 速度が正なら奥から手前へ 手前へ来るほど大きく、中心から離れていく
        # 実物を 1 フレームずつ追うと、30 個の粒がすべて外へ向かって流れていた
        # 周回の変わり目（位置を引き直す所）をまたがない時刻で比べる
        # 0.05 秒なら位相は 0.015 + i/30 で、次のフレームまでにどれも 1 をまたがない
        before = self._field(0.05, fade_in=0.0, fade_out=0.0)
        after = self._field(0.05 + 1.0 / 60.0, fade_in=0.0, fade_out=0.0)
        assert np.all(after.scale > before.scale)
        assert np.all(np.hypot(after.x, after.y) >= np.hypot(before.x, before.y))

    def test_a_negative_speed_goes_away(self) -> None:
        before = self._field(0.5, speed=-6.0, fade_in=0.0, fade_out=0.0)
        after = self._field(0.51, speed=-6.0, fade_in=0.0, fade_out=0.0)
        assert np.all(after.scale <= before.scale)

    def test_the_speed_is_speed_over_depth_laps_per_second(self) -> None:
        # 実物の 1 フレームあたりの広がり（中心からの距離の伸び）の中央値は、
        # 速度 6 で 0.72% 速度 12 で 1.46% だった 速さが倍なら伸びも倍になる
        growth = []
        for speed in (6.0, 12.0):
            now, then = self._field(0.4, speed=speed), self._field(0.4 + 1 / 60, speed=speed)
            ratio = then.scale / now.scale - 1.0
            growth.append(float(np.median(ratio)))
        assert growth[1] / growth[0] == pytest.approx(2.0, rel=0.25)

    def test_a_huge_count_is_capped(self) -> None:
        # キーフレームの値は設定の上限（5000）を守らない 個数をそのまま配列にすると
        # 何百万もの粒を作って描画が止まる 無限大は int() で例外になっていた
        huge = self._field(0.3, count=1e6, fade_in=0.0, fade_out=0.0)
        assert huge.x.size <= 5000
        assert self._field(0.3, count=float("inf")).x.size == 0

    def test_all_stars_are_accounted_for(self) -> None:
        # 個数ぶんの粒が居る（フェードの途中で透明な粒だけは外す）
        field = self._field(0.3, fade_in=0.0, fade_out=0.0)
        assert field.x.size == 30


def _drawn(params: dict[str, ParamValue], frame: int, duration: int = 81) -> np.ndarray:
    source = GeneratedSource(kind="shape", params=params)
    image = render_source(source, WIDTH, HEIGHT, frame=frame, fps=60.0, duration=duration)
    assert image is not None
    lit: np.ndarray = image[:, :, 3].astype(np.float32)
    return lit


def test_the_trail_is_drawn_as_an_arrow() -> None:
    # 実物の kumiki_p5_line_x のフレーム 20 横は -608（始点の円の端）から
    # -267（先端の三角形の頂点）まで 太さは 16 で、先端だけ 40 ほどに広がる
    moving = AnimatedValue(
        0.0, keyframes=(Keyframe(frame=0, value=-600.0), Keyframe(frame=80, value=600.0))
    )
    alpha = _drawn(
        {"shape": "motion_trail", "pos_x": moving, "line_width": AnimatedValue(16.0)}, 20
    )
    columns = np.nonzero(alpha.max(axis=0) > 128)[0] - WIDTH // 2
    assert columns.min() == pytest.approx(-608, abs=1)
    assert columns.max() == pytest.approx(-267, abs=2)
    rows = np.nonzero(alpha[:, WIDTH // 2 - 450] > 128)[0]
    assert len(rows) == pytest.approx(15, abs=1)
    head_rows = np.nonzero(alpha[:, WIDTH // 2 - 300] > 128)[0]
    assert len(head_rows) > 30


def test_the_star_field_moves_with_time() -> None:
    # 時計で動く図形 同じ絵を使い回すと、止まった星空になる
    params: dict[str, ParamValue] = {"shape": "starfield", "star_count": AnimatedValue(200.0)}
    assert not np.array_equal(_drawn(params, 0), _drawn(params, 30))
    assert _drawn(params, 30).max() > 200


def test_a_broken_star_size_does_not_stop_drawing() -> None:
    # 無限大の大きさは粒の絵を作るところで例外になり、フレームごと描けなかった
    params: dict[str, ParamValue] = {
        "shape": "starfield",
        "star_count": AnimatedValue(50.0),
        "star_size": AnimatedValue(float("inf")),
    }
    assert _drawn(params, 10).max() > 0
