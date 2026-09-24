"""Issue #27 の 2 回目の要望 画面の細かい所

- Qt 標準の文言（確認の窓のボタンなど）が英語のまま出る
- 補足（ツールチップ）が暗い地に暗い文字で読めない
- 重ねたパネルのタブが下に出る（設定で上か下かを選ぶ）
- 最後まで再生すると、終わりの少し手前で止まる
- 再生位置の静止画を保存・コピーできない
- オブジェクト設定が何のクリップの物か分からない
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import shiboken6
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QTabBar,
    QTabWidget,
    QToolTip,
)

from sashimono.core.commands import RenameProject
from sashimono.core.model import (
    FILTER_KIND,
    Clip,
    Effect,
    GeneratedSource,
    MediaItem,
    Project,
    ProjectSettings,
    Scene,
    Timeline,
    Track,
    TrackKind,
    new_group_id,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects.sources import TEXT
from sashimono.engine.audio import player as player_module
from sashimono.engine.audio.player import AudioPlayer
from sashimono.ui import playback as playback_module
from sashimono.ui import snapshot as snapshot_module
from sashimono.ui.inspector import InspectorPanel
from sashimono.ui.inspector.header import identify_clip
from sashimono.ui.main_window import MainWindow
from sashimono.ui.playback import PlaybackController
from sashimono.ui.preferences_dialog import PreferencesDialog
from sashimono.ui.snapshot import render_snapshot, snapshot_frame, snapshot_name, write_png
from sashimono.ui.theme import STYLE_SHEET, Colors
from sashimono.ui.translation import install_qt_translation
from sashimono.ui.workspace import (
    DOCK_TABS_BOTTOM,
    DOCK_TABS_TOP,
    Preferences,
    PreferenceStore,
)


def _contrast(first: QColor, second: QColor) -> float:
    """WCAG のコントラスト比 4.5 を下回ると、普通の大きさの文字は読みにくい"""

    def luminance(color: QColor) -> float:
        def channel(value: float) -> float:
            return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

        return (
            0.2126 * channel(color.redF())
            + 0.7152 * channel(color.greenF())
            + 0.0722 * channel(color.blueF())
        )

    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class TestQtTranslation:
    def test_the_standard_buttons_speak_japanese(self, qt_application: QApplication) -> None:
        # 翻訳を読まないと、保存の確認が Save / Discard / Cancel と英語で出る
        assert install_qt_translation(qt_application) is not None
        box = QMessageBox()
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        texts = {button.text() for button in box.buttons()}
        assert not texts & {"Save", "Discard", "Cancel", "&Save", "&Discard"}
        assert "キャンセル" in texts

    def test_dialog_button_boxes_speak_japanese(self, qt_application: QApplication) -> None:
        # 設定やショートカットの窓の OK / Cancel も同じ翻訳から出る
        install_qt_translation(qt_application)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Close
        )
        assert {b.text() for b in buttons.buttons()} == {"キャンセル", "閉じる"}

    def test_installing_twice_does_not_stack(self, qt_application: QApplication) -> None:
        # 2 回目で同じ翻訳を重ねると、外すときに片方だけ残る
        first = install_qt_translation(qt_application)
        assert install_qt_translation(qt_application) == first

    def test_the_unsaved_changes_question_speaks_japanese(
        self, qt_application: QApplication
    ) -> None:
        # 利用者が見たのはここ 閉じるときの「変更を保存しますか」のボタンが英語だった
        install_qt_translation(qt_application)
        window = MainWindow(Project.create(), confirm_unsaved=True)
        window.execute(RenameProject("別名"))
        seen: list[str] = []

        def answer() -> None:
            box = QApplication.activeModalWidget()
            assert isinstance(box, QMessageBox)
            seen.extend(button.text() for button in box.buttons())
            cancel = box.button(QMessageBox.StandardButton.Cancel)
            assert cancel is not None
            cancel.click()

        QTimer.singleShot(0, answer)
        try:
            assert not window._confirm_discard()
        finally:
            window._confirm_unsaved = False
            window.close()
        assert seen
        assert not {"Save", "Discard", "Cancel"} & set(seen)
        assert "保存" in seen


class TestToolTip:
    def test_a_dark_system_palette_still_gives_readable_tips(
        self, qt_application: QApplication
    ) -> None:
        # Windows の暗い配色では、補足の地も文字も暗くなって読めなかった
        # OS の配色がどうであっても、スタイルシートの色で描かれること
        saved_palette, saved_sheet = qt_application.palette(), qt_application.styleSheet()
        saved_tip = QToolTip.palette()
        dark = QPalette(saved_palette)
        dark.setColor(QPalette.ColorRole.ToolTipBase, QColor("#2b2b2b"))
        dark.setColor(QPalette.ColorRole.ToolTipText, QColor("#1a1a1a"))
        try:
            qt_application.setPalette(dark)
            QToolTip.setPalette(dark)
            qt_application.setStyleSheet(STYLE_SHEET)
            anchor = QPushButton("x")
            anchor.show()
            QToolTip.showText(QPoint(10, 10), "再生 / 停止 (Space)", anchor)
            qt_application.processEvents()
            # 画面の無い試験の環境では、出したあとすぐ隠れることがある 描き方を見るだけなので
            # 隠れていても絵にする
            tip = next(
                w
                for w in QApplication.topLevelWidgets()
                if w.metaObject().className() == "QTipLabel"
            )
            tip.ensurePolished()
            image = tip.grab().toImage()
            colors: dict[str, int] = {}
            for x in range(image.width()):
                for y in range(image.height()):
                    name = image.pixelColor(x, y).name()
                    colors[name] = colors.get(name, 0) + 1
            QToolTip.hideText()
            anchor.close()
        finally:
            qt_application.setStyleSheet(saved_sheet)
            qt_application.setPalette(saved_palette)
            QToolTip.setPalette(saved_tip)
        background = QColor(max(colors, key=lambda name: colors[name]))
        assert background == Colors.TOOL_TIP
        # 地の色と文字の色が十分に離れていること
        assert _contrast(Colors.TOOL_TIP, Colors.CLIP_LABEL) >= 4.5


@pytest.fixture
def window(qt_application: QApplication) -> Iterator[MainWindow]:
    del qt_application
    settings = ProjectSettings(width=64, height=48, frame_rate=FrameRate(30))
    project = Project.create(settings)
    tracks = (Track(TrackKind.VIDEO, "V1"), Track(TrackKind.AUDIO, "A1"))
    created = MainWindow(
        project.with_timeline(replace(project.timeline, tracks=tracks)), confirm_unsaved=False
    )
    yield created
    created.close()


def _tab_positions(window: MainWindow) -> set[QTabBar.Shape]:
    """見えているタブの並びの向き

    Qt は重ねたパネルのタブの並びを使い終えても消さずに残すことがあり、残った物は
    ほかの部品（メニューやタイムライン）の下に隠れている 押せる所にある物だけを数える
    """
    return {
        bar.shape()
        for bar in window.findChildren(QTabBar)
        if bar.count() > 1 and window.childAt(bar.geometry().center()) is bar
    }


class TestDockTabs:
    def test_stacked_panels_show_their_tabs_on_top(self, window: MainWindow) -> None:
        # Qt の既定では下に出て、パネルを切り替えられることに気付かない
        window.show()
        QApplication.processEvents()
        assert _tab_positions(window) == {QTabBar.Shape.RoundedNorth}
        window.hide()

    def test_the_preference_puts_them_back_at_the_bottom(self, window: MainWindow) -> None:
        # 下が見慣れた人が戻せないと、設定がある意味が無い
        window._apply_preferences(replace(window._preferences, dock_tabs=DOCK_TABS_BOTTOM))
        window.show()
        QApplication.processEvents()
        assert _tab_positions(window) == {QTabBar.Shape.RoundedSouth}
        assert window.tabPosition(window.dockWidgetArea(window._subtitle_dock)) == (
            QTabWidget.TabPosition.South
        )
        window.hide()

    def test_switching_while_open_leaves_no_second_row_of_tabs(self, window: MainWindow) -> None:
        # 重ねた後で向きを変えると、前の向きのタブが残って上下に 2 つ出ないか
        # 見える所に出ているタブの並びを、隅々まで点で当たって確かめる
        window.show()
        for position, shape in (
            (DOCK_TABS_BOTTOM, QTabBar.Shape.RoundedSouth),
            (DOCK_TABS_TOP, QTabBar.Shape.RoundedNorth),
            (DOCK_TABS_BOTTOM, QTabBar.Shape.RoundedSouth),
        ):
            window._apply_preferences(replace(window._preferences, dock_tabs=position))
            QApplication.processEvents()
            seen: set[QTabBar] = set()
            for bar in window.findChildren(QTabBar):
                rect = bar.geometry()
                for x in range(rect.left(), rect.right() + 1, 4):
                    for y in range(rect.top(), rect.bottom() + 1, 2):
                        hit = window.childAt(x, y)
                        if isinstance(hit, QTabBar) and hit.count() > 1:
                            seen.add(hit)
            assert {bar.shape() for bar in seen} == {shape}
            # メディアと字幕、オブジェクト設定と AI アシスタントの 2 組だけ
            assert len(seen) == 2
        window.hide()

    def test_the_default_is_top(self) -> None:
        assert Preferences().dock_tabs == DOCK_TABS_TOP

    def test_the_choice_is_saved_and_a_broken_value_falls_back(self, tmp_path: Path) -> None:
        store = PreferenceStore(tmp_path / "preferences.json")
        store.save(Preferences(dock_tabs=DOCK_TABS_BOTTOM))
        assert store.load().dock_tabs == DOCK_TABS_BOTTOM
        # 知らない値のまま当てると、どちらでもないタブになる
        (tmp_path / "preferences.json").write_text('{"dock_tabs": "left"}', encoding="utf-8")
        assert store.load().dock_tabs == DOCK_TABS_TOP

    def test_the_dialog_keeps_the_choice(self, qt_application: QApplication) -> None:
        # 設定を開いて OK を押しただけで下に選んだ物が上へ戻ると、選び直すことになる
        del qt_application
        dialog = PreferencesDialog(Preferences(dock_tabs=DOCK_TABS_BOTTOM))
        assert dialog.preferences().dock_tabs == DOCK_TABS_BOTTOM


class _FakePlayer:
    """音の出口の代わり 位置と鳴っているかを試験から決める"""

    def __init__(self, mixer: object) -> None:
        del mixer
        self.position_sample = 0
        self.is_playing = False
        self.end_sample: int | None = None

    def start(self, from_sample: int, *, end_sample: int | None = None) -> None:
        self.position_sample = from_sample
        self.end_sample = end_sample
        self.is_playing = True

    def stop(self) -> None:
        self.is_playing = False

    def close(self) -> None:
        self.stop()


def _controller(
    monkeypatch: pytest.MonkeyPatch, rate: FrameRate, duration: int
) -> tuple[PlaybackController, _FakePlayer]:
    monkeypatch.setattr(playback_module, "AudioPlayer", _FakePlayer)
    settings = ProjectSettings(width=64, height=48, frame_rate=rate)
    base = Project.create(settings)
    clip = Clip(timeline_start=0, duration=duration, source=TEXT.create(text="x"))
    track = Track(TrackKind.VIDEO, "V1", clips=(clip,))
    project = base.with_timeline(replace(base.timeline, tracks=(track,)))
    controller = PlaybackController(project)
    player = controller._player
    assert isinstance(player, _FakePlayer)
    return controller, player


class TestPlaybackEnd:
    @pytest.mark.parametrize("rate", [FrameRate(30), FrameRate(30000, 1001)])
    def test_playing_to_the_end_stops_at_the_end(
        self, monkeypatch: pytest.MonkeyPatch, rate: FrameRate
    ) -> None:
        # 音が鳴り終えた（出口が止まった）ときに、終わりの手前で止めない
        # 29.97fps では終わりのサンプルをフレームへ戻すと 1 つ手前になり、届かなかった
        controller, player = _controller(monkeypatch, rate, 90)
        frames: list[int] = []
        controller.frame_changed.connect(frames.append)
        controller.play()
        assert player.end_sample is not None
        player.position_sample = player.end_sample
        player.is_playing = False
        controller._tick()
        assert controller.frame == 90
        assert frames[-1] == 90
        assert not controller.is_playing

    def test_a_device_that_dies_midway_stops_where_it_was(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 途中で出口が抜かれたのに終わりへ飛ぶと、どこまで聞いたのかが分からなくなる
        controller, player = _controller(monkeypatch, FrameRate(30), 90)
        controller.play()
        player.position_sample = 48000  # 1 秒
        controller._tick()
        player.is_playing = False
        controller._tick()
        assert controller.frame == 30
        assert not controller.is_playing


class _FakeStream:
    """音の出口の代わり 書き込みはすぐ返り、バッファには ``latency`` 秒ぶん残る"""

    def __init__(self, latency: float) -> None:
        self.latency = latency

    def write(self, block: np.ndarray) -> None:
        del block


class _SilentMixer:
    sample_rate = 48000
    channels = 2

    def render(self, start: int, count: int) -> np.ndarray:
        del start
        return np.zeros((count, 2), dtype=np.float32)


class TestPlayerClock:
    def test_the_clock_reaches_the_end_after_the_buffer_plays_out(self) -> None:
        # 書き終えた所で時計を止めると、デバイスに残った分（数フレーム）だけ手前で止まる
        player = AudioPlayer(_SilentMixer())  # type: ignore[arg-type]
        player._stream = _FakeStream(latency=0.05)
        player._start_sample = 0
        player._end_sample = 4800
        started = time.monotonic()
        player._run()
        assert player.position_sample == 4800
        # 残りが鳴り終わるまで待ってから終わりに置く すぐ終わりへ飛ばすと、絵が音より先に終わる
        assert time.monotonic() - started >= 0.04

    def test_stopping_during_the_drain_does_not_jump_to_the_end(self) -> None:
        # 止めた時点で止める 止めたのに終わりまで進むと、止めた所から再生し直せない
        player = AudioPlayer(_SilentMixer())  # type: ignore[arg-type]
        player._stream = _FakeStream(latency=1.0)
        player._start_sample = 0
        player._end_sample = 48000
        runner = threading.Thread(target=player._run)
        runner.start()
        time.sleep(0.05)
        player._stop.set()
        runner.join(timeout=2)
        assert player.position_sample < 48000

    def test_the_drain_polls_finer_than_the_controller(self) -> None:
        assert player_module.DRAIN_POLL_SECONDS * 1000 < playback_module.POLL_INTERVAL_MS


def _colored_project() -> Project:
    """64x48 の青一色 描いた絵の大きさと色を確かめる"""
    settings = ProjectSettings(width=64, height=48, frame_rate=FrameRate(30))
    base = Project.create(settings, name="作品")
    shape = GeneratedSource(
        kind="shape", params={"shape": "background", "color": (0.2, 0.4, 0.8, 1.0)}
    )
    clip = Clip(timeline_start=0, duration=30, source=shape)
    track = Track(TrackKind.VIDEO, "V1", clips=(clip,))
    return base.with_timeline(replace(base.timeline, tracks=(track,)))


class TestSnapshotName:
    def test_the_name_is_project_and_timecode(self) -> None:
        project = _colored_project()
        assert snapshot_name(project, 45) == "作品_00-00-01-15.png"

    def test_unsafe_characters_are_replaced(self) -> None:
        # Windows のファイル名に使えない文字が残ると、保存の窓が名前を受け付けない
        project = _colored_project().renamed('a/b:c?"d"')
        name = snapshot_name(project, 0)
        assert not set('/:?"') & set(name)

    def test_the_end_of_the_timeline_takes_the_last_frame(self) -> None:
        # 最後まで再生した位置（duration）には何も無い 黒い絵を撮らない
        project = _colored_project()
        assert snapshot_frame(project, project.duration) == project.duration - 1
        assert snapshot_frame(project, -3) == 0


@pytest.mark.usefixtures("gpu")
class TestSnapshotImage:
    def test_the_png_has_the_project_size_and_the_drawn_color(self, tmp_path: Path) -> None:
        # プレビューの GL を読むと、窓の大きさと画質の設定で大きさも色も変わる
        target = tmp_path / "shot.png"
        assert write_png(render_snapshot(_colored_project(), 0), target)
        loaded = QImage(str(target))
        assert (loaded.width(), loaded.height()) == (64, 48)
        color = loaded.pixelColor(32, 24)
        assert (color.red(), color.blue()) == pytest.approx((51, 204), abs=3)
        assert color.alpha() == 255

    def test_the_window_saves_what_the_playhead_shows(
        self, qt_application: QApplication, tmp_path: Path
    ) -> None:
        del qt_application
        window = MainWindow(_colored_project(), confirm_unsaved=False)
        try:
            target = window.save_snapshot(tmp_path / "shot.png")
        finally:
            window.close()
        assert target == tmp_path / "shot.png"
        loaded = QImage(str(target))
        assert (loaded.width(), loaded.height()) == (64, 48)

    def test_the_clipboard_gets_the_full_size_picture(
        self, qt_application: QApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 本物のクリップボードは使わない ほかのアプリが掴んでいると読み戻しが空になり、
        # 試験の結果が機械の様子で変わる 本人がコピーしていた物も上書きしてしまう
        del qt_application
        copied: list[QImage] = []
        monkeypatch.setattr(snapshot_module, "copy_to_clipboard", copied.append)
        window = MainWindow(_colored_project(), confirm_unsaved=False)
        try:
            window.copy_snapshot()
        finally:
            window.close()
        (image,) = copied
        assert (image.width(), image.height()) == (64, 48)


class TestSnapshotMenu:
    """GPU の要らない所 描く所は差し替える"""

    def test_the_name_and_the_picture_use_the_same_frame(
        self, qt_application: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 保存先を尋ねている間に再生ヘッドが進んでも、名前のタイムコードと描く絵は同じフレーム
        # 尋ねた後に再生ヘッドを読み直すと、名前は 10 フレーム目なのに絵は 20 フレーム目になる
        del qt_application
        window = MainWindow(_colored_project(), confirm_unsaved=False)
        window._seek(10)
        suggested: list[str] = []
        drawn: list[int] = []

        def ask(*args: object) -> tuple[str, str]:
            suggested.append(str(args[2]))
            window._timeline.set_playhead(20)
            return str(tmp_path / "shot.png"), ""

        def render(project: Project, frame: int) -> QImage:
            del project
            drawn.append(frame)
            return QImage(4, 4, QImage.Format.Format_RGBX8888)

        monkeypatch.setattr(QFileDialog, "getSaveFileName", ask)
        monkeypatch.setattr(snapshot_module, "render_snapshot", render)
        try:
            assert window.save_snapshot() == tmp_path / "shot.png"
        finally:
            window.close()
        assert suggested[0].endswith("00-00-00-10.png")
        assert drawn == [10]

    def test_the_actions_have_their_own_keys(self, window: MainWindow) -> None:
        # 割り当てが重なると Qt はどちらも動かさない（押しても何も起きない）
        keys = [action.shortcut().toString() for action, _ in window._actions.values()]
        for key in ("Ctrl+Alt+S", "Ctrl+Alt+C"):
            assert keys.count(key) == 1


def _linked_project(video_media: MediaItem) -> tuple[Project, Clip, Clip]:
    link = new_group_id()
    picture = Clip(timeline_start=0, duration=30, media_id=video_media.id, link_group=link)
    sound = Clip(timeline_start=0, duration=30, media_id=video_media.id, link_group=link)
    base = Project.create()
    tracks = (
        Track(TrackKind.VIDEO, "V1", clips=(picture,)),
        Track(TrackKind.AUDIO, "A1", clips=(sound,)),
    )
    project = replace(
        base.with_timeline(replace(base.timeline, tracks=tracks)), media=(video_media,)
    )
    return project, picture, sound


class TestClipIdentity:
    def test_linked_audio_says_it_is_the_sound(self, video_media: MediaItem) -> None:
        # 映像と音声が結ばれた素材で、どちらを開いているのかが分からなかった
        project, picture, sound = _linked_project(video_media)
        audio = identify_clip(project, sound.id)
        video = identify_clip(project, picture.id)
        assert audio is not None and video is not None
        assert audio.summary() == "音声（本編.mp4 の音） / A1"
        assert video.summary() == "映像（本編.mp4 の絵） / V1"
        # 帯の色でも見分ける タイムラインのクリップと同じ色
        assert audio.color == Colors.AUDIO_CLIP_BORDER
        assert video.color == Colors.VIDEO_CLIP_BORDER

    def test_unlinked_media_uses_the_plain_name(self, audio_media: MediaItem) -> None:
        clip = Clip(timeline_start=0, duration=30, media_id=audio_media.id)
        base = Project.create()
        tracks = (Track(TrackKind.AUDIO, "BGM", clips=(clip,)),)
        project = replace(
            base.with_timeline(replace(base.timeline, tracks=tracks)), media=(audio_media,)
        )
        identity = identify_clip(project, clip.id)
        assert identity is not None
        assert identity.summary() == "音声（bgm.wav） / BGM"

    def test_generated_clips_name_their_kind(self) -> None:
        text = Clip(timeline_start=0, duration=30, source=TEXT.create(text="こんにちは\n2 行目"))
        effect = Effect(kind="blur")
        filter_clip = Clip(
            timeline_start=30,
            duration=30,
            source=GeneratedSource(kind=FILTER_KIND),
            effects=(effect,),
        )
        scene = Scene(name="オープニング", timeline=Timeline(rate=FrameRate(30)))
        nested = Clip(timeline_start=60, duration=30, scene_id=scene.id)
        base = Project.create()
        tracks = (Track(TrackKind.VIDEO, "", clips=(text, filter_clip, nested)),)
        project = replace(base, timeline=replace(base.timeline, tracks=tracks), scenes=(scene,))

        found = [identify_clip(project, clip.id) for clip in (text, filter_clip, nested)]
        assert [i.title if i else None for i in found] == [
            "テキスト（こんにちは）",
            # フィルタは中身が何のエフェクトかを名前の代わりにする
            "フィルタ（ぼかし）",
            "シーン（オープニング）",
        ]
        assert found[1] is not None and found[1].color == Colors.FILTER_CLIP_BORDER
        # 名前の無いトラックは種類と番号で呼ぶ 空のままだと「トラック 」で途切れる
        assert found[0] is not None and found[0].track == "映像トラック 1"

    def test_the_panel_shows_which_clip_it_is(
        self, qt_application: QApplication, video_media: MediaItem
    ) -> None:
        del qt_application
        project, picture, sound = _linked_project(video_media)
        panel = InspectorPanel()
        try:
            panel.set_project(project)
            panel.set_selection((sound.id, picture.id))
            assert panel.header.title_text() == "音声（本編.mp4 の音）"
            assert "A1" in panel.header.detail_text()
            # 何本も選んでいると、触った値がほかにも当たることを言う
            assert "ほか 1 本" in panel.header.detail_text()
            panel.set_selection(())
            assert panel.header.identity is None
        finally:
            # 親の無い部品を Python の片付けに任せると、後の試験の途中で壊されて落ちる
            panel.close()
            shiboken6.delete(panel)
