"""プレビューの画面と先読みのつなぎ

貯める仕組みそのものは tests/engine/test_prefetch.py で見ている ここで見るのは
**いつ動かして、いつ止めて、何を捨てるか** つなぎ間違えても絵は正しく出続ける
（作り直されるだけ）ので、画面を見ていても気づけない
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QApplication

from kumiki.core.commands import AddClip, AddTrack
from kumiki.core.model import Clip, MediaId, Project, Track, TrackKind
from kumiki.engine.cache.proxy import ProxyStore
from kumiki.engine.render import Invalidation
from kumiki.ui.preview import SLOW_FRAME_MS, PreviewWidget


class StubRenderer:
    """GL を持たないレンダラ 渡されたものを覚えるだけ"""

    def __init__(self) -> None:
        self.reopened: list[Collection[MediaId] | None] = []

    def set_project(self, project: Project) -> None:
        pass

    def set_proxies(self, proxies: ProxyStore | None) -> None:
        pass

    def set_quality(self, quality: object) -> None:
        pass

    def reopen_sources(self, media_ids: Collection[MediaId] | None = None) -> None:
        self.reopened.append(media_ids)


class StubCache:
    """GL を持たない置き場 捨てるよう言われた範囲を覚える"""

    def __init__(self) -> None:
        self.enabled = True
        self.thrown: list[Invalidation] = []
        self.steps: list[int] = []
        self.budget: int | None = None

    def invalidate(self, invalidation: Invalidation) -> int:
        self.thrown.append(invalidation)
        return 0

    def step(self, playhead: int) -> bool:
        self.steps.append(playhead)
        return False

    def set_budget(self, budget_bytes: int) -> None:
        self.budget = budget_bytes

    def release(self) -> None:
        pass


def _project() -> tuple[Project, Clip, Clip]:
    """離れた所に 2 本 片方を触っても、もう片方の絵は変わらない"""
    track = Track(TrackKind.VIDEO, "V1")
    project = AddTrack(track).apply(Project.create())
    near = Clip(timeline_start=0, duration=30)
    far = Clip(timeline_start=100, duration=30)
    project = AddClip(track.id, near).apply(project)
    return AddClip(track.id, far).apply(project), near, far


@pytest.fixture
def preview(
    qt_application: QApplication, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[PreviewWidget, StubCache]]:
    del qt_application
    project, _, _ = _project()
    widget = PreviewWidget(project, prefetch_bytes=1024 * 1024 * 1024)
    # GL を作らずに中身だけ差し替える makeCurrent は本物の窓が無いと効かない
    monkeypatch.setattr(widget, "makeCurrent", lambda: None)
    monkeypatch.setattr(widget, "doneCurrent", lambda: None)
    monkeypatch.setattr(widget, "_renderer", StubRenderer())
    stub = StubCache()
    monkeypatch.setattr(widget, "_cache", stub)
    yield widget, stub
    widget._idle.stop()


class TestWhenItRuns:
    def test_it_starts_when_the_playhead_moves(
        self, preview: tuple[PreviewWidget, StubCache]
    ) -> None:
        """再生ヘッドが動いたら貯め直す 次に貯める先が変わっている"""
        widget, _ = preview
        widget.set_frame(42)
        assert widget._idle.isActive()

    def test_it_stops_while_playing(self, preview: tuple[PreviewWidget, StubCache]) -> None:
        """再生中は止める 出す側と同じ GPU を奪い合うと、いま出すコマが遅れる"""
        widget, _ = preview
        widget.set_frame(42)
        widget.set_playing(True)
        assert not widget._idle.isActive()

    def test_it_comes_back_when_playing_stops(
        self, preview: tuple[PreviewWidget, StubCache]
    ) -> None:
        widget, _ = preview
        widget.set_playing(True)
        widget.set_playing(False)
        assert widget._idle.isActive()

    def test_it_does_not_run_without_a_budget(
        self, preview: tuple[PreviewWidget, StubCache]
    ) -> None:
        widget, stub = preview
        stub.enabled = False
        widget.set_frame(5)
        assert not widget._idle.isActive()

    def test_it_stops_when_there_is_nothing_left(
        self, preview: tuple[PreviewWidget, StubCache]
    ) -> None:
        """貯まりきったら止める 止めないと、空き時間のたびに数え続ける"""
        widget, stub = preview
        widget.set_frame(7)
        widget._prefetch_step()
        assert stub.steps == [7]
        assert not widget._idle.isActive()


class TestWhatItThrowsAway:
    def test_an_edit_far_away_keeps_the_near_frames(
        self, preview: tuple[PreviewWidget, StubCache]
    ) -> None:
        """離れたクリップを触っても、こちらの絵は残る 先読みの値打ちはここ"""
        widget, stub = preview
        project, _, far = _project()
        widget.set_project(project)
        track = project.timeline.tracks[0]
        kept = tuple(clip for clip in track.clips if clip.id != far.id)
        after = replace(
            project,
            timeline=project.timeline.replace_track(track.with_clips((*kept, far.moved_to(200)))),
        )
        stub.thrown.clear()
        widget.set_project(after)
        assert len(stub.thrown) == 1
        assert not stub.thrown[0].contains(10), "触っていない所まで捨てている"
        assert stub.thrown[0].contains(100), "動かす前の場所を捨て損ねている"
        assert stub.thrown[0].contains(200), "動かした先を捨て損ねている"

    def test_swapping_the_proxies_throws_everything(
        self, preview: tuple[PreviewWidget, StubCache]
    ) -> None:
        """読む元が変われば絵も変わる 残すと、控えに切り替えても画面が変わらない"""
        widget, stub = preview
        widget.set_proxies(ProxyStore(height=540))
        assert [thrown.everything for thrown in stub.thrown] == [True]

    def test_the_same_proxies_throw_nothing(self, preview: tuple[PreviewWidget, StubCache]) -> None:
        # 設定を開いて閉じただけで貯めた絵が消えない
        widget, stub = preview
        widget.set_proxies(None)
        assert stub.thrown == []

    def test_reopening_sources_throws_everything(
        self, preview: tuple[PreviewWidget, StubCache]
    ) -> None:
        """控えができた・壊れていて元へ戻した どちらも開き直した素材の絵が変わる"""
        widget, stub = preview
        widget.reload_sources()
        assert [thrown.everything for thrown in stub.thrown] == [True]


class TestTheBudget:
    def test_it_reaches_the_cache(self, preview: tuple[PreviewWidget, StubCache]) -> None:
        widget, stub = preview
        widget.set_prefetch_bytes(512 * 1024 * 1024)
        assert stub.budget == 512 * 1024 * 1024

    def test_the_same_budget_changes_nothing(
        self, preview: tuple[PreviewWidget, StubCache]
    ) -> None:
        # 毎回渡される所なので、同じ値なら触らない
        widget, stub = preview
        widget.set_prefetch_bytes(widget._prefetch_bytes)
        assert stub.budget is None


class TestWhenPrefetchingGoesWrong:
    """先読みは無くても絵は出る 先読みの都合でアプリを落とさない"""

    def test_a_failure_does_not_escape_the_timer(
        self, preview: tuple[PreviewWidget, StubCache], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """描けなくても投げ返さない

        ここは Qt のタイマーから呼ばれる 投げるとイベントループの外まで抜け、
        編集中のプロジェクトごとアプリが終わる
        """
        widget, stub = preview

        def explode(playhead: int) -> bool:
            raise RuntimeError("デコーダが開けない")

        monkeypatch.setattr(stub, "step", explode)
        stopped: list[str] = []
        widget.prefetch_stopped.connect(stopped.append)
        widget.set_frame(3)
        widget._prefetch_step()
        assert not widget._idle.isActive()
        assert stopped and "デコーダが開けない" in stopped[0]

    def test_a_slow_frame_stops_it(
        self, preview: tuple[PreviewWidget, StubCache], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 コマに掛かりすぎるなら貯めるのをやめる

        描いているのは編集画面と同じ GL コンテキストなので、その間は操作を
        受け付けられない 2 秒掛かる素材で貯め続けると、ずっと固まって見える
        """
        widget, stub = preview
        ticks = iter([0.0, (SLOW_FRAME_MS + 1) / 1000])
        monkeypatch.setattr("kumiki.ui.preview.time.perf_counter", lambda: next(ticks))
        monkeypatch.setattr(stub, "step", lambda playhead: True)
        stopped: list[str] = []
        widget.prefetch_stopped.connect(stopped.append)
        widget.set_frame(3)
        widget._prefetch_step()
        assert not widget._idle.isActive()
        assert stopped, "止めたことを伝えていない"

    def test_a_quick_frame_keeps_going(
        self, preview: tuple[PreviewWidget, StubCache], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        widget, stub = preview
        ticks = iter([0.0, 0.001])
        monkeypatch.setattr("kumiki.ui.preview.time.perf_counter", lambda: next(ticks))
        monkeypatch.setattr(stub, "step", lambda playhead: True)
        widget.set_frame(3)
        widget._prefetch_step()
        assert widget._idle.isActive()

    def test_a_context_failure_does_not_escape_either(
        self, preview: tuple[PreviewWidget, StubCache], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """コンテキストを current にする所で落ちても同じ

        描く所だけを守っても、その手前で落ちたら結局アプリが終わる
        """
        widget, _ = preview

        def explode() -> None:
            raise RuntimeError("コンテキストを current にできない")

        monkeypatch.setattr(widget, "makeCurrent", explode)
        stopped: list[str] = []
        widget.prefetch_stopped.connect(stopped.append)
        widget.set_frame(3)
        widget._prefetch_step()
        assert not widget._idle.isActive()
        assert stopped

    def test_a_context_that_did_not_become_current_stops_it(
        self, preview: tuple[PreviewWidget, StubCache], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """current にならなくても makeCurrent は黙って戻る

        戻り値では分からないので、実際に current かどうかを見る 見ないと、
        窓が隠れている間に別のコンテキストへ描きに行く
        """
        widget, stub = preview
        monkeypatch.setattr(widget, "context", lambda: object())
        stopped: list[str] = []
        widget.prefetch_stopped.connect(stopped.append)
        widget.set_frame(3)
        widget._prefetch_step()
        assert stub.steps == [], "current でないのに描きに行っている"
        assert not widget._idle.isActive()
        assert stopped


class TestTheQueuedTick:
    def test_a_tick_that_arrives_after_playback_starts_does_nothing(
        self, preview: tuple[PreviewWidget, StubCache]
    ) -> None:
        """止めた後に届いた合図で描かない

        タイマーを止めても、すでに積まれた合図は届く そこで 1 コマ描くと、
        いま出すべきコマと GL を奪い合う
        """
        widget, stub = preview
        widget.set_frame(3)
        widget.set_playing(True)
        widget._prefetch_step()
        assert stub.steps == []
