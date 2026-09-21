"""本人の好みの設定（プレビューの重さに関わるもの）

自動で画質を落とす所は好みが分かれる 勝手に変わるのを嫌う人もいるので、
設定で止められること・止めたら本当に止まることを押さえる
"""

from __future__ import annotations

from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from kumiki.core.commands import AddMedia
from kumiki.core.model import MediaItem, Project, VideoStreamInfo
from kumiki.core.timebase import FrameRate
from kumiki.ui.main_window import MainWindow
from kumiki.ui.preferences_dialog import PREFETCH_BUDGETS, PROXY_HEIGHTS, PreferencesDialog
from kumiki.ui.workspace import AUTO_QUALITY_HEIGHT, Preferences, PreferenceStore


def _uhd_media() -> MediaItem:
    """4K の素材 ここでは大きさだけを見るので、ファイルの中身は要らない"""
    return MediaItem(
        path=Path("4k.mp4"),
        duration=Fraction(10),
        video_streams=(
            VideoStreamInfo(
                index=0,
                width=3840,
                height=2160,
                frame_rate=FrameRate(30),
                time_base=Fraction(1, 30),
                codec="h264",
            ),
        ),
    )


class TestTheAutomaticQuality:
    def test_a_small_source_stays_full(self) -> None:
        # 1080p までは等倍で 60fps に入る 落とす値打ちが無い
        assert Preferences().quality_for(1080) == 1

    def test_a_big_source_drops(self) -> None:
        """4K では落とす

        1 枚だけなら元の素材でも入るが、重ねた時点で外れる（実測は
        kumiki.engine.cache.proxy の表）効果を積むと控えだけでも足りず、
        画面の側も落として初めて入る
        """
        assert Preferences().quality_for(2160) == 2

    def test_the_boundary_is_the_stated_height(self) -> None:
        plain = Preferences()
        assert plain.quality_for(AUTO_QUALITY_HEIGHT) == 2
        assert plain.quality_for(AUTO_QUALITY_HEIGHT - 1) == 1

    def test_turning_it_off_keeps_full_quality(self) -> None:
        # 切ったのに落ちるなら、設定の意味が無い
        assert Preferences(auto_quality=False).quality_for(2160) == 1

    def test_the_divisor_is_the_one_chosen(self) -> None:
        assert Preferences(auto_quality_divisor=4).quality_for(2160) == 4


class TestSaving:
    def test_it_comes_back(self, tmp_path: Path) -> None:
        store = PreferenceStore(tmp_path / "preferences.json")
        chosen = Preferences(
            use_proxy=False, proxy_height=720, auto_quality=False, auto_quality_divisor=4
        )
        store.save(chosen)
        assert store.load() == chosen

    def test_nothing_saved_is_the_default(self, tmp_path: Path) -> None:
        assert PreferenceStore(tmp_path / "無い.json").load() == Preferences()

    def test_a_broken_file_does_not_stop_the_start(self, tmp_path: Path) -> None:
        """壊れていても起動は止めない 既定のまま使えれば困らない"""
        path = tmp_path / "preferences.json"
        path.write_text("これは JSON ではない", encoding="utf-8")
        assert PreferenceStore(path).load() == Preferences()

    def test_broken_bytes_do_not_stop_the_start(self, tmp_path: Path) -> None:
        """UTF-8 として読めないファイルでも起動は止めない

        UnicodeDecodeError は ValueError の仲間なので、いまの受け方で捕まる
        受け方を狭めたら、設定ファイルが壊れた人の起動が止まる
        """
        path = tmp_path / "preferences.json"
        path.write_bytes(bytes([0xFF, 0xFE]) + b"not utf-8" * 4)
        assert PreferenceStore(path).load() == Preferences()

    def test_a_wrong_type_falls_back_per_item(self, tmp_path: Path) -> None:
        """項目ごとに既定へ戻す 1 つ壊れただけで全部を捨てない"""
        path = tmp_path / "preferences.json"
        path.write_text(
            '{"use_proxy": "はい", "proxy_height": 720, "auto_quality_divisor": 3}',
            encoding="utf-8",
        )
        loaded = PreferenceStore(path).load()
        assert loaded.use_proxy is True, "文字を真偽として読んでいる"
        assert loaded.proxy_height == 720, "読める項目まで捨てている"
        assert loaded.auto_quality_divisor == 2, "半端な分母を受けている"

    def test_a_float_divisor_falls_back(self, tmp_path: Path) -> None:
        """JSON の ``2.0`` を受けない

        ``2.0 in (1, 2, 4)`` は真になる 小数のまま通すと、描画先の大きさが
        小数になって型の食い違いで落ちる
        """
        path = tmp_path / "preferences.json"
        path.write_text('{"auto_quality_divisor": 2.0}', encoding="utf-8")
        loaded = PreferenceStore(path).load()
        assert isinstance(loaded.auto_quality_divisor, int)
        assert not isinstance(loaded.auto_quality_divisor, float)

    def test_an_absurd_height_falls_back(self, tmp_path: Path) -> None:
        """0 や巨大な値を受けない

        0 だと控えが作れず、大きすぎると元の素材より重くなる
        """
        path = tmp_path / "preferences.json"
        path.write_text('{"proxy_height": 0}', encoding="utf-8")
        assert PreferenceStore(path).load().proxy_height == Preferences().proxy_height


class TestTheDialog:
    @pytest.fixture
    def dialog(self, qt_application: QApplication) -> PreferencesDialog:
        del qt_application
        return PreferencesDialog(Preferences())

    def test_it_shows_what_is_set(self, qt_application: QApplication) -> None:
        del qt_application
        chosen = Preferences(use_proxy=False, proxy_height=720, auto_quality_divisor=4)
        assert PreferencesDialog(chosen).preferences() == chosen

    def test_the_height_choices_include_the_default(self) -> None:
        # 既定が一覧に無いと、設定を開いて閉じただけで値が変わる
        assert Preferences().proxy_height in [height for _, height in PROXY_HEIGHTS]

    def test_an_unknown_height_is_kept(self, qt_application: QApplication) -> None:
        """一覧に無い値でも落ちず、**そのまま残る**

        先頭へ倒すと、設定を開いて OK を押しただけで触っていない項目が変わる
        """
        del qt_application
        dialog = PreferencesDialog(Preferences(proxy_height=1000))
        assert dialog.preferences().proxy_height == 1000

    def test_the_numbers_come_from_the_measured_table(self, dialog: PreferencesDialog) -> None:
        """画面に出す数は**控えの側の定数**から取る

        画面へ直に書くと、測り直したときにここだけ古いまま残り、
        使う人が古い数を見て設定を選ぶことになる
        """
        from kumiki.engine.cache.proxy import (
            MEASURED_ONE_LAYER_MS,
            MEASURED_PREFETCH_MS,
            MEASURED_THREE_LAYERS_MS,
        )

        shown = dialog.findChildren(QLabel)
        text = " ".join(label.text() for label in shown)
        for value in (*MEASURED_THREE_LAYERS_MS, MEASURED_ONE_LAYER_MS, *MEASURED_PREFETCH_MS):
            assert f"{value}ms" in text, f"{value}ms が画面に出ていない"

    def test_turning_off_the_proxy_disables_its_size(self, dialog: PreferencesDialog) -> None:
        # 使わない設定が押せると、効いていると思って触ってしまう
        dialog._use_proxy.setChecked(False)
        assert not dialog._proxy_height.isEnabled()

    def test_turning_off_the_auto_quality_disables_its_divisor(
        self, dialog: PreferencesDialog
    ) -> None:
        dialog._auto_quality.setChecked(False)
        assert not dialog._auto_divisor.isEnabled()


class TestTheWindowFollowsThem:
    """設定を変えたら、その場で効く 開き直しを求めない"""

    @pytest.fixture
    def window(self, qt_application: QApplication) -> Iterator[MainWindow]:
        del qt_application
        created = MainWindow(Project.create(), confirm_unsaved=False)
        yield created
        created.close()

    def test_turning_the_proxy_off_takes_it_away(self, window: MainWindow) -> None:
        # 切ったのに控えのままだと、切った意味が無い
        window._apply_preferences(Preferences(use_proxy=False))
        assert window._preview._proxies is None

    def test_turning_it_on_puts_it_back(self, window: MainWindow) -> None:
        window._apply_preferences(Preferences(use_proxy=False))
        window._apply_preferences(Preferences(use_proxy=True))
        assert window._preview._proxies is window._proxies.store

    def test_the_size_reaches_the_store(self, window: MainWindow) -> None:
        # 大きさを変えても届かないなら、設定が効いていない
        window._apply_preferences(Preferences(proxy_height=720))
        assert window._proxies.store.height == 720

    def test_opening_a_big_project_drops_the_quality_at_once(
        self, qt_application: QApplication
    ) -> None:
        """**開いた時点で**効かせる

        コマンドラインや関連付けから開く道は _on_project_changed を通らない
        抜けると、4K のプロジェクトを開いても最初の 1 回だけ等倍のまま重い
        """
        del qt_application
        project = AddMedia(_uhd_media()).apply(Project.create())
        window = MainWindow(project, confirm_unsaved=False)
        try:
            assert window._transport.quality() == 2
        finally:
            window.close()

    def test_changing_only_the_quality_keeps_the_builder(self, window: MainWindow) -> None:
        """画質だけを変えたときは、控えを作る係を作り直さない

        作り直すと、進行中の変換が止まって最初からになる
        """
        before = window._proxies
        window._apply_preferences(Preferences(auto_quality_divisor=4))
        assert window._proxies is before

    def test_turning_the_proxy_off_stops_the_running_work(self, window: MainWindow) -> None:
        """切ったら、裏で走っている変換も止まる

        止めないと、切ったのに変換が続いて CPU を食う（切った意味が無い）
        """
        before = window._proxies
        window._apply_preferences(Preferences(use_proxy=False))
        assert window._proxies is not before, "変換を止めていない"

    def test_changing_the_size_rebuilds_the_builder(self, window: MainWindow) -> None:
        # 大きさが変わると置き場の鍵が変わる 作り直さないと前の大きさのまま
        before = window._proxies
        window._apply_preferences(Preferences(proxy_height=720))
        assert window._proxies is not before

    def test_a_finished_proxy_makes_the_preview_reopen(self, window: MainWindow) -> None:
        """控えができたら素材を開き直す

        描き直すだけでは切り替わらない 先にプレビューした素材は、
        レンダラが元のファイルを掴んだまま（控えを作った意味が無くなる）
        """
        asked: list[object] = []
        # 差し替えるのは、GL コンテキストの無い試験で本物を呼べないため
        # （中で makeCurrent を呼ぶ）呼ばれたかどうかだけが見たい
        window._preview.reload_sources = (  # type: ignore[method-assign]
            lambda media_ids=None: asked.append(media_ids)
        )
        media_id = _uhd_media().id
        window._on_proxy_ready(media_id)
        assert window._proxied == {media_id}, "控えができた素材を覚えていない"
        window._flush_analysis()
        assert asked == [{media_id}], "できた素材のぶんだけ開き直していない"
        assert not window._proxied, "残ると、毎回開き直して再生が途切れる"

    def test_a_discarded_proxy_is_asked_for_again(self, window: MainWindow) -> None:
        """使えない控えを捨てたら、作り直しを頼む

        頼まないと、その回だけでなく**そのあとずっと**元の素材を読み続ける
        （置き場には何も無いままなので、次に開いたときも作られない）
        """
        media = _uhd_media()
        window.execute(AddMedia(media))
        asked: list[object] = []
        window._proxies.request = lambda item, **kwargs: asked.append(item.id)  # type: ignore[method-assign]
        window._preview.take_discarded = lambda: {media.id}  # type: ignore[method-assign]
        window._flush_analysis()
        assert asked == [media.id], "作り直しを頼んでいない"

    def test_a_proxy_is_rebuilt_only_once(self, window: MainWindow) -> None:
        """作り直しは**1 度だけ**

        作り直した控えがまた使えないと、頼み続けて 250ms ごとに変換が走り、
        編集そのものが重くなる あきらめて元の素材で映す
        """
        media = _uhd_media()
        window.execute(AddMedia(media))
        asked: list[object] = []
        window._proxies.request = lambda item, **kwargs: asked.append(item.id)  # type: ignore[method-assign]
        window._preview.take_discarded = lambda: {media.id}  # type: ignore[method-assign]
        window._flush_analysis()
        window._flush_analysis()
        assert asked == [media.id], "同じ素材を何度も作り直している"

    def test_a_new_proxy_size_may_be_rebuilt_again(self, window: MainWindow) -> None:
        """控えの大きさを変えたら、また作り直せる

        素材で覚えると、1 度あきらめた素材は窓を閉じるまで元のまま
        鍵で覚えれば、大きさを変えたときや素材を差し替えたときに作り直せる
        """
        media = _uhd_media()
        window.execute(AddMedia(media))
        asked: list[object] = []
        window._proxies.request = lambda item, **kwargs: asked.append(item.id)  # type: ignore[method-assign]
        window._preview.take_discarded = lambda: {media.id}  # type: ignore[method-assign]
        window._flush_analysis()
        window._apply_preferences(Preferences(proxy_height=720))
        window._proxies.request = lambda item, **kwargs: asked.append(item.id)  # type: ignore[method-assign]
        window._flush_analysis()
        assert asked.count(media.id) == 2, "大きさを変えたのに作り直していない"

    def test_a_big_project_drops_the_preview_quality(self, window: MainWindow) -> None:
        """4K の素材を置いたら画質が下がる

        置いたあとに自分で下げてもらうのでは、最初の 1 回が必ず重い
        """
        window.execute(AddMedia(_uhd_media()))
        assert window._transport.quality() == 2

    def test_it_stays_full_when_turned_off(self, window: MainWindow) -> None:
        window._apply_preferences(Preferences(auto_quality=False))
        window.execute(AddMedia(_uhd_media()))
        assert window._transport.quality() == 1


class TestThePrefetchSetting:
    """先読み（バックグラウンドレンダリング）の入り切りと、使うメモリ

    貯める仕組みは GPU のメモリを掴み続ける 切ったのに掴んだままだと、
    ほかの作業のために切った人の目的が果たせない
    """

    def test_it_is_on_by_default(self) -> None:
        """既定は入 知らない人が「重い所で再生が飛ぶ」に当たらない側へ倒す"""
        assert Preferences().prefetch is True

    def test_turning_it_off_leaves_no_budget(self) -> None:
        # 予算 0 は「1 枚も置けない」 下まで旗を配らずに止められる
        assert Preferences(prefetch=False).prefetch_bytes() == 0

    def test_the_budget_is_in_bytes(self) -> None:
        """MB とバイトの換算 間違えると 1024 倍ずれる

        小さい方へずれれば 1 枚も置けず、大きい方へずれれば GPU のメモリを
        使い切って、プレビューそのものが描けなくなる
        """
        assert Preferences(prefetch_budget_mb=512).prefetch_bytes() == 512 * 1024 * 1024

    def test_it_comes_back(self, tmp_path: Path) -> None:
        """保存して読み直しても同じ 落ちると、起動のたびに設定し直しになる"""
        store = PreferenceStore(tmp_path / "preferences.json")
        chosen = Preferences(prefetch=False, prefetch_budget_mb=2048)
        store.save(chosen)
        assert store.load() == chosen

    def test_an_absurd_budget_falls_back(self, tmp_path: Path) -> None:
        """小さすぎる・大きすぎる値は既定へ

        小さいと 1 枚も置けず、設定したのに何も起きない 大きいと GPU の
        メモリを使い切り、デコードや効果の側が確保に失敗する
        """
        path = tmp_path / "preferences.json"
        path.write_text('{"prefetch_budget_mb": 1}', encoding="utf-8")
        assert PreferenceStore(path).load().prefetch_budget_mb == Preferences().prefetch_budget_mb

    def test_a_float_budget_falls_back(self, tmp_path: Path) -> None:
        """JSON の ``1024.0`` を受けない 掛け算の結果が小数になり、
        入る枚数を数える所で型が食い違う
        """
        path = tmp_path / "preferences.json"
        path.write_text('{"prefetch_budget_mb": 1024.0}', encoding="utf-8")
        loaded = PreferenceStore(path).load()
        assert isinstance(loaded.prefetch_budget_mb, int)
        assert not isinstance(loaded.prefetch_budget_mb, float)

    def test_the_dialog_shows_what_is_set(self, qt_application: QApplication) -> None:
        """画面が今の設定を映す 映らないと、開いて OK を押しただけで別の値になる"""
        del qt_application
        chosen = Preferences(prefetch=False, prefetch_budget_mb=4096)
        assert PreferencesDialog(chosen).preferences() == chosen

    def test_an_unknown_budget_is_kept(self, qt_application: QApplication) -> None:
        """設定ファイルへ手で書いた予算も残る 開いて閉じただけで減らさない"""
        del qt_application
        unknown = 8192
        # 一覧に入っていないことを先に押さえる 後から選択肢へ足されると、
        # この試験は「一覧にある値を選び直せる」だけの中身になって気づけない
        assert unknown not in [budget for _, budget in PREFETCH_BUDGETS]
        dialog = PreferencesDialog(Preferences(prefetch_budget_mb=unknown))
        assert dialog.preferences().prefetch_budget_mb == unknown

    def test_the_budget_choices_include_the_default(self) -> None:
        # 既定が一覧に無いと、設定を開いて閉じただけで値が変わる
        assert Preferences().prefetch_budget_mb in [budget for _, budget in PREFETCH_BUDGETS]

    def test_turning_it_off_disables_the_budget(self, qt_application: QApplication) -> None:
        """切ったらメモリの欄は押せなくなる 押せると、効くと思って触ってしまう"""
        del qt_application
        dialog = PreferencesDialog(Preferences())
        dialog._prefetch.setChecked(False)
        assert not dialog._prefetch_budget.isEnabled()

    def test_the_window_stops_it(self, qt_application: QApplication) -> None:
        """切ったら本当に止まる 裏で描き続けるなら切った意味が無い"""
        del qt_application
        window = MainWindow(Project.create(), confirm_unsaved=False)
        try:
            window._apply_preferences(Preferences(prefetch=False))
            assert window._preview._prefetch_bytes == 0
            assert not window._preview._idle.isActive()
        finally:
            window.close()

    def test_the_window_passes_the_budget_on(self, qt_application: QApplication) -> None:
        """選んだメモリが画面まで届く 届かないと、変えても貯まる量が変わらない"""
        del qt_application
        window = MainWindow(Project.create(), confirm_unsaved=False)
        try:
            window._apply_preferences(Preferences(prefetch_budget_mb=512))
            assert window._preview._prefetch_bytes == 512 * 1024 * 1024
        finally:
            window.close()
