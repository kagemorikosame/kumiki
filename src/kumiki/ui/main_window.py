"""メインウィンドウ 各パネルを組み立て、コマンドの実行を一手に引き受ける

UI のどこから来た操作も、必ず :meth:`MainWindow.execute` を通って
:class:`~kumiki.core.commands.Document` に入る AI エージェントも同じ入口を
使う予定なので、ここが増えないようにしておく
"""

from __future__ import annotations

import contextlib
import functools
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QImage,
    QImageWriter,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from kumiki.ai.host import ToolError
from kumiki.compat.aviutl.exo import ExoFile
from kumiki.core.commands import (
    AddMedia,
    AddScene,
    Command,
    Document,
    InScene,
    ParamPath,
    RemoveMedia,
    RemoveScene,
    RenameScene,
    SetResolution,
    insert_generated,
    insert_media,
    insert_scene,
    new_scene,
)
from kumiki.core.io import (
    LEGACY_SUFFIXES,
    SUFFIX,
    HeldLock,
    ProjectFileError,
    RecoveryEntry,
    RecoverySession,
    backup_before_save,
    backup_folder,
    discard,
    find_orphans,
    hold_new,
    load_project,
    others_holding,
    project_presence_dir,
    save_project,
)
from kumiki.core.model import (
    ClipId,
    GeneratedSource,
    MediaId,
    MediaItem,
    Project,
    ProjectSettings,
    SceneId,
)
from kumiki.effects.sources import SHAPE, TEXT, TRANSITION
from kumiki.engine.audio.waveform import Waveform
from kumiki.engine.cache import MediaAnalyzer
from kumiki.engine.cache.proxy import ProxyBuilder, ProxyStore
from kumiki.engine.decode import ProbeError, probe_media
from kumiki.engine.render import FrameRenderer, RenderQuality
from kumiki.ui.chat import ChatPanel
from kumiki.ui.export_dialog import ExportDialog
from kumiki.ui.graph_editor import GraphEditor
from kumiki.ui.inspector import InspectorPanel
from kumiki.ui.media_pool import MediaPoolWidget
from kumiki.ui.playback import PlaybackController
from kumiki.ui.preview import PreviewWidget
from kumiki.ui.scene_bar import SceneBar
from kumiki.ui.subtitle import SubtitlePanel
from kumiki.ui.theme import Colors
from kumiki.ui.timeline import TimelineView
from kumiki.ui.timeline.view import HEIGHT_STEP
from kumiki.ui.transport import TransportBar
from kumiki.ui.workspace import (
    LAYOUT_VERSION,
    Preferences,
    PreferenceStore,
    ShortcutStore,
    Workspace,
)

__all__ = ["MainWindow"]

#: 解析の完了を画面へ反映する間隔（ミリ秒）
#: 解析はワーカースレッドで終わるので、その通知を待って毎回描き直すのではなく、
#: まとめて一定間隔で描き直す 素材を 100 本入れたときに描画で埋もれないように
ANALYSIS_REFRESH_MS = 250

#: 保存していない変更を退避する間隔（ミリ秒）
#: 落ちたときに失うのは最大でこの長さの作業 短くするほど書き込みが増えるが、
#: 1 回は数百 KB の JSON なので 30 秒なら気にならない
AUTOSAVE_MS = 30_000

_PORTABLE = QKeySequence.SequenceFormat.PortableText

#: AviUtl のオブジェクトファイル
EXO_FILTER = "AviUtl オブジェクト (*.exo *.exa *.exo2 *.exa2);;すべてのファイル (*)"


class MainWindow(QMainWindow):
    """編集画面"""

    project_changed = Signal(object)

    def __init__(
        self,
        project: Project | None = None,
        *,
        path: Path | None = None,
        confirm_unsaved: bool = True,
    ) -> None:
        """``confirm_unsaved`` を偽にすると、閉じるときに保存を尋ねない テスト用"""
        super().__init__()
        self.setWindowTitle("Kumiki")
        self.resize(1440, 900)

        self._document = Document(project if project is not None else Project.create())
        self._path: Path | None = path
        #: 最後に保存した（または開いた）時点のプロジェクト 同じオブジェクトなら
        #: 変更なし モデルは frozen なので、取り消して保存した状態へ戻れば
        #: 「変更なし」に戻る 数を数える方式だとここがずれる
        self._saved: Project | None = self._document.project
        self._autosaved: Project | None = self._document.project
        self._confirm_unsaved = confirm_unsaved
        self._recovery = RecoverySession()
        #: 開いているプロジェクトの錠 同じファイルを別の窓で開いたことに気付くため
        self._project_lock: HeldLock | None = None
        if path is not None and not self._claim(path):
            # 別の窓で開いていて、開くのをやめると選ばれた 中身だけ見せて保存先を
            # 持たないと、錠を持たないまま同じファイルへ保存できてしまう 空で始める
            self._path = None
            self._document.reset(Project.create())
            self._saved = self._autosaved = self._document.project
        #: 操作の名前 → （QAction、既定のキー） ショートカットの設定が使う
        self._actions: dict[str, tuple[QAction, str]] = {}
        self._analyzer = MediaAnalyzer(
            sample_rate=self._document.project.settings.sample_rate,
            channels=self._document.project.settings.channels,
        )
        #: 本人の好みの設定 プロジェクトではなく本人に付く
        self._preferences = PreferenceStore().load()
        #: プレビュー用の控えを作る係 **書き出しには渡さない**
        #: 渡すと、画面では気付かないまま低解像度の絵が最終出力に入る
        self._proxies = ProxyBuilder(ProxyStore(height=self._preferences.proxy_height))
        self._analysis_dirty = False
        #: 控えができた素材 次の間隔でこのぶんだけ開き直す
        #: ワーカースレッドが足し、画面のスレッドが取り出すので錠で守る
        #: 守らないと、取り出した直後に足されたぶんが次の回にも残らず、
        #: その素材だけ元のファイルを読み続ける
        self._proxied: set[MediaId] = set()
        self._proxied_lock = threading.Lock()
        #: AI が結果を確認するための描画係 初めて求められたときに作る
        self._ai_renderer: FrameRenderer | None = None
        #: 編集しているシーン ``None`` ならメイン モデルではなく画面の状態なので
        #: 窓が持つ（保存しない 開き直したらメインから始まる）
        self._active_scene: SceneId | None = None

        self._build_widgets()
        self._build_menus()
        self._connect()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(ANALYSIS_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._flush_analysis)
        self._refresh_timer.start()

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(AUTOSAVE_MS)
        self._autosave_timer.timeout.connect(self.autosave)
        self._autosave_timer.start()

        # 既定の並びを覚えてから、前回の並びを当てる 逆にすると「初期に戻す」が
        # 前回の並びに戻るだけになる
        self._default_layout = self.saveState(LAYOUT_VERSION)
        self._workspace = Workspace()
        self._workspace.restore(self)
        self._apply_shortcuts(ShortcutStore().load())

        # 渡されたプロジェクトの素材にも効かせる コマンドラインや関連付けから
        # 開く道はここを通るだけで、_on_project_changed を通らない
        # 抜けると、4K のプロジェクトを開いても最初の 1 回だけ等倍のまま重い
        for media in self.view_project.media:
            self._request_proxy(media)
        self._apply_auto_quality()

        self._update_title()

    # --- 組み立て ---

    def _build_widgets(self) -> None:
        project = self._document.project

        self._preview = PreviewWidget(
            project, self, proxies=self._proxies.store if self._preferences.use_proxy else None
        )
        self._transport = TransportBar(project.rate, self)
        self._timeline = TimelineView(project, self._analyzer, self)
        self._media_pool = MediaPoolWidget(project, self)
        self._inspector = InspectorPanel(self)
        # 設定パネルは選んだクリップを引くためにプロジェクトを持つ 起動直後にも渡す
        # （渡さないと、最初の編集まで何本も選んだときのまとめ当てが効かない）
        self._inspector.set_project(self.view_project)
        self._graph = GraphEditor(self)
        self._subtitles = SubtitlePanel(project, self._analyzer, self)
        self._chat = ChatPanel(self, self)
        self._playback = PlaybackController(project, self)

        viewer = QWidget(self)
        viewer_layout = QVBoxLayout(viewer)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        viewer_layout.setSpacing(0)
        viewer_layout.addWidget(self._preview, 1)
        viewer_layout.addWidget(self._transport)
        viewer.setStyleSheet(f"background-color: {Colors.VIEWER_BACKGROUND.name()};")
        self.setCentralWidget(viewer)

        pool_dock = self._dock("メディア", "media")
        pool_dock.setWidget(self._media_pool)
        pool_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, pool_dock)

        inspector_dock = self._dock("オブジェクト設定", "inspector")
        inspector_dock.setWidget(self._inspector)
        inspector_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, inspector_dock)
        self.resizeDocks([inspector_dock], [320], Qt.Orientation.Horizontal)

        graph_dock = self._dock("グラフエディタ", "graph")
        graph_dock.setWidget(self._graph)
        graph_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, graph_dock)
        # 既定では畳んでおく 曲線を触るのは慣れてからで、最初から出ていると
        # 画面が狭くなるだけになる
        graph_dock.hide()
        self._graph_dock = graph_dock

        subtitle_dock = self._dock("字幕", "subtitles")
        subtitle_dock.setWidget(self._subtitles)
        subtitle_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, subtitle_dock)
        # メディアプールと同じ場所にタブで重ねる どちらも「素材を選ぶ」ための
        # パネルで、同時に見る場面が少ない
        self.tabifyDockWidget(pool_dock, subtitle_dock)
        pool_dock.raise_()
        self._subtitle_dock = subtitle_dock

        chat_dock = self._dock("AI アシスタント", "chat")
        chat_dock.setWidget(self._chat)
        chat_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, chat_dock)
        self.tabifyDockWidget(inspector_dock, chat_dock)
        inspector_dock.raise_()
        self._chat_dock = chat_dock

        timeline_dock = self._dock("タイムライン", "timeline")
        self._scene_bar = SceneBar()
        timeline_panel = QWidget()
        timeline_layout = QVBoxLayout(timeline_panel)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(0)
        timeline_layout.addWidget(self._scene_bar)
        timeline_layout.addWidget(self._timeline, 1)
        timeline_dock.setWidget(timeline_panel)
        timeline_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, timeline_dock)
        self.resizeDocks([timeline_dock], [320], Qt.Orientation.Vertical)

        self.statusBar().showMessage("素材を読み込んでください")

    def _dock(self, title: str, name: str) -> QDockWidget:
        """パネルを 1 つ作る

        ``objectName`` が無いと、Qt は画面配置を保存も復元もしない（黙って飛ばす）
        表示名は訳や言い回しで変わりうるので、名前は別に固定の英字で付ける
        """
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        return dock

    def _build_menus(self) -> None:
        file_menu = self._menu("ファイル")
        self._add(file_menu, "新規", QKeySequence.StandardKey.New, self.new_project)
        self._add(file_menu, "開く…", QKeySequence.StandardKey.Open, self.open_project)
        self._add(file_menu, "保存", QKeySequence.StandardKey.Save, self.save_project)
        self._add(file_menu, "名前を付けて保存…", QKeySequence("Ctrl+Shift+S"), self.save_as)
        self._add(
            file_menu, "バックアップのフォルダを開く", QKeySequence(), self.open_backup_folder
        )
        file_menu.addSeparator()
        self._add(file_menu, "プロジェクト設定…", QKeySequence("Ctrl+Shift+P"), self.edit_settings)
        file_menu.addSeparator()
        self._add(file_menu, "素材を読み込む…", QKeySequence("Ctrl+I"), self._import_dialog)
        self._add(file_menu, "書き出し…", QKeySequence("Ctrl+E"), self.export)
        file_menu.addSeparator()
        self._add(file_menu, "終了", QKeySequence.StandardKey.Quit, self.close)

        edit_menu = self._menu("編集")
        self._undo_action = self._add(
            edit_menu, "元に戻す", QKeySequence.StandardKey.Undo, self.undo
        )
        self._redo_action = self._add(
            edit_menu, "やり直す", QKeySequence.StandardKey.Redo, self.redo
        )
        edit_menu.addSeparator()
        self._add(
            edit_menu, "再生ヘッドで分割", QKeySequence("S"), self._timeline.split_at_playhead
        )
        self._add(edit_menu, "削除", QKeySequence("Del"), lambda: self._timeline.delete_selected())
        self._add(
            edit_menu,
            "削除して詰める",
            QKeySequence("Shift+Del"),
            lambda: self._timeline.delete_selected(ripple=True),
        )
        edit_menu.addSeparator()
        # 字幕パネルの表の中で文字を編集しているあいだは、Qt が入力欄のほうへ
        # Ctrl+C を渡す（入力欄が標準のキーを先に取る） 文字のコピーと取り合わない
        self._add(edit_menu, "コピー", QKeySequence.StandardKey.Copy, self._timeline.copy_selected)
        self._add(edit_menu, "切り取り", QKeySequence.StandardKey.Cut, self._timeline.cut_selected)
        self._add(
            edit_menu,
            "貼り付け（再生ヘッドの位置）",
            QKeySequence.StandardKey.Paste,
            self._timeline.paste_at_playhead,
        )
        self._add(
            edit_menu, "すべて選択", QKeySequence.StandardKey.SelectAll, self._timeline.select_all
        )
        edit_menu.addSeparator()
        # Ctrl+G はグラフエディタが先に使っている 今ある割り当ては変えない
        self._add(
            edit_menu, "グループ化", QKeySequence("Ctrl+Alt+G"), self._timeline.group_selected
        )
        self._add(
            edit_menu,
            "グループ解除",
            QKeySequence("Ctrl+Alt+Shift+G"),
            self._timeline.ungroup_selected,
        )
        edit_menu.addSeparator()
        # ヘッダのボタンと同じ切り替えをメニューにも置く キーボードだけで操作する人の
        # 入口で、ショートカットの設定にも載る
        for text, key, attribute in (
            ("トラックをミュート", "Shift+M", "muted"),
            ("トラックをソロ", "Shift+S", "solo"),
            ("トラックをロック", "Shift+L", "locked"),
        ):
            self._add(
                edit_menu,
                text,
                QKeySequence(key),
                functools.partial(self._toggle_track, attribute),
            )

        object_menu = self._menu("オブジェクト")
        self._add(object_menu, "テキストを追加", QKeySequence("Ctrl+T"), self.add_text)
        self._add(object_menu, "図形を追加", QKeySequence("Ctrl+Shift+T"), self.add_shape)
        self._add(object_menu, "場面切り替えを追加", QKeySequence(), self.add_transition)

        scene_menu = self._menu("シーン")
        self._add(scene_menu, "新しいシーン…", QKeySequence("Ctrl+Alt+N"), self._ask_new_scene)
        self._add(scene_menu, "シーンを置く…", QKeySequence("Ctrl+Alt+P"), self._ask_place_scene)
        scene_menu.addSeparator()
        self._add(scene_menu, "シーンの名前を変更…", QKeySequence(), self._ask_rename_scene)
        self._add(scene_menu, "シーンを削除", QKeySequence(), self.remove_active_scene)
        scene_menu.addSeparator()
        self._add(
            scene_menu, "メインに戻る", QKeySequence("Ctrl+Alt+M"), lambda: self.open_scene(None)
        )

        subtitle_menu = self._menu("字幕")
        self._add(subtitle_menu, "字幕パネル", QKeySequence("Ctrl+Shift+U"), self.show_subtitles)
        subtitle_menu.addSeparator()
        self._add(subtitle_menu, "起こす…", QKeySequence("Ctrl+U"), self.transcribe)
        self._add(subtitle_menu, "整形…", QKeySequence("Ctrl+Shift+F"), self._subtitles.clean)
        self._add(
            subtitle_menu,
            "無音カット…",
            QKeySequence("Ctrl+Shift+J"),
            self._subtitles.jet_cut,
        )
        subtitle_menu.addSeparator()
        self._add(subtitle_menu, "焼き込み", QKeySequence(), self._subtitles.burn)
        self._add(subtitle_menu, "書き出し…", QKeySequence(), self._subtitles.export_file)

        compat_menu = self._menu("互換")
        self._add(
            compat_menu,
            "オブジェクトを読み込む…",
            QKeySequence("Ctrl+Shift+O"),
            self.import_exo,
        )
        self._add(
            compat_menu,
            "テンプレート…",
            QKeySequence("Ctrl+Shift+D"),
            self.show_templates,
        )
        compat_menu.addSeparator()
        self._add(compat_menu, "スクリプトを読み直す", QKeySequence(), self.rescan_scripts)
        self._add(compat_menu, "スクリプトフォルダを開く", QKeySequence(), self.open_script_folder)
        self._add(compat_menu, "互換性レポート…", QKeySequence(), self.show_compatibility)

        ai_menu = self._menu("AI")
        self._add(ai_menu, "アシスタント", QKeySequence("Ctrl+Shift+A"), self.show_chat)

        view_menu = self._menu("表示")
        self._add(
            view_menu, "拡大", QKeySequence.StandardKey.ZoomIn, lambda: self._timeline.zoom(1.25)
        )
        self._add(
            view_menu, "縮小", QKeySequence.StandardKey.ZoomOut, lambda: self._timeline.zoom(0.8)
        )
        self._add(view_menu, "全体を表示", QKeySequence("Shift+Z"), self._timeline.zoom_to_fit)
        self._add(
            view_menu,
            "グラフエディタ",
            QKeySequence("Ctrl+G"),
            lambda: self._graph_dock.setVisible(not self._graph_dock.isVisible()),
        )
        view_menu.addSeparator()
        self._add(
            view_menu,
            "トラックを高く",
            QKeySequence("Ctrl+Shift+Up"),
            lambda: self._timeline.adjust_track_heights(HEIGHT_STEP),
        )
        self._add(
            view_menu,
            "トラックを低く",
            QKeySequence("Ctrl+Shift+Down"),
            lambda: self._timeline.adjust_track_heights(-HEIGHT_STEP),
        )
        self._add(
            view_menu, "トラックの高さを戻す", QKeySequence(), self._timeline.reset_track_heights
        )
        view_menu.addSeparator()
        self._add(view_menu, "画面配置を初期に戻す", QKeySequence(), self.reset_layout)
        self._add(view_menu, "ショートカットの設定…", QKeySequence(), self.customize_shortcuts)
        self._add(view_menu, "設定…", QKeySequence(), self.edit_preferences)

        playback_menu = self._menu("再生")
        self._add(playback_menu, "再生 / 停止", QKeySequence("Space"), self._playback.toggle)

        self._update_history_actions()

    def _add(
        self,
        menu: QMenu,
        text: str,
        shortcut: QKeySequence | QKeySequence.StandardKey,
        slot: Callable[[], object],
    ) -> QAction:
        action = QAction(text, self)
        action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        # 名前は作った時点の表示で決める 「元に戻す: 分割」のように表示が
        # あとから変わる項目があり、そちらで引くと保存した割り当てが外れる
        self._actions[f"{menu.title()}/{text}"] = (action, action.shortcut().toString(_PORTABLE))
        return action

    def _apply_shortcuts(self, bindings: dict[str, str]) -> None:
        """割り当てを当てる 知らない名前は飛ばす（版が変わって消えた項目など）"""
        for name, key in bindings.items():
            entry = self._actions.get(name)
            if entry is not None:
                entry[0].setShortcut(QKeySequence(key, _PORTABLE))

    def customize_shortcuts(self) -> None:
        from kumiki.ui.shortcut_dialog import ShortcutDialog, ShortcutRow

        rows = [
            ShortcutRow(name, action.shortcut().toString(_PORTABLE), default)
            for name, (action, default) in self._actions.items()
        ]
        dialog = ShortcutDialog(rows, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        bindings = dialog.bindings()
        self._apply_shortcuts(bindings)
        overrides = {name: key for name, key in bindings.items() if key != self._actions[name][1]}
        try:
            ShortcutStore().save(overrides)
        except OSError as exc:
            self.statusBar().showMessage(f"ショートカットを保存できなかった: {exc}", 5000)

    def edit_preferences(self) -> None:
        """本人の好みの設定を変える"""
        from kumiki.ui.preferences_dialog import PreferencesDialog

        dialog = PreferencesDialog(self._preferences, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_preferences(dialog.preferences())

    def _apply_preferences(self, preferences: Preferences) -> None:
        """設定を今の画面へ反映して保存する

        控えの大きさを変えたら別の鍵になるので、作り直しを頼む
        古い控えは残るが、掴むことはない（鍵に大きさを混ぜてある）
        """
        # 作り直すのは大きさが変わったときと、控えを切ったとき
        # 大きさは置き場の鍵が変わるため 切ったときは**走っている変換を止める**ため
        # （切ったのに裏で変換が続くなら、切った意味が無い）
        # 何が変わっても作り直すと、画質の設定を触っただけで進行中の変換が止まる
        resized = preferences.proxy_height != self._preferences.proxy_height
        stopped = self._preferences.use_proxy and not preferences.use_proxy
        self._preferences = preferences
        try:
            PreferenceStore().save(preferences)
        except OSError as exc:
            self.statusBar().showMessage(f"設定を保存できなかった: {exc}", 5000)

        if resized or stopped:
            self._proxies.close()
            self._proxies = ProxyBuilder(ProxyStore(height=preferences.proxy_height))
        self._preview.set_proxies(self._proxies.store if preferences.use_proxy else None)
        for media in self.view_project.media:
            self._request_proxy(media)
        self._apply_auto_quality()

    def _apply_auto_quality(self) -> None:
        """置いてある素材の大きさに合わせて、プレビューの画質を決める

        測った結果（``tools/bench_proxy.py`` 3840x2160 を 3 枚重ねて blur と glow
        95 パーセンタイル）控えだけでは 23.7ms、画質を落とすだけでは 59.5ms で、
        どちらも 60fps の予算 16.7ms に入らない 両方で 14.8ms

        1 枚だけなら元の素材でも 11.3ms で入る それでも大きさで決めるのは、
        重ねた時点で入らなくなるため（3 枚で 62.0ms）落としたくない人は切れる
        """
        tallest = max(
            (
                stream.display_size[1]
                for media in self.view_project.media
                for stream in media.video_streams
            ),
            default=0,
        )
        self._transport.set_quality(self._preferences.quality_for(tallest))

    def reset_layout(self) -> None:
        self.restoreState(self._default_layout, LAYOUT_VERSION)

    def _menu(self, title: str) -> QMenu:
        """メニューを 1 つ作る ``addMenu`` は None を返しうるので、ここで確定させる"""
        menu = self.menuBar().addMenu(title)
        if menu is None:  # pragma: no cover - Qt が None を返すのは異常系のみ
            raise RuntimeError(f"メニューを作れない: {title}")
        return menu

    def _connect(self) -> None:
        self._timeline.commands_requested.connect(self.execute_all)
        self._timeline.commands_continued.connect(
            lambda commands, label: self.execute_all(commands, label, merge=True)
        )
        self._timeline.playhead_moved.connect(self._on_playhead_moved)

        self._media_pool.import_requested.connect(self.import_media)
        self._media_pool.insert_requested.connect(self._insert_media_by_id)
        self._media_pool.transcribe_requested.connect(self._transcribe_media)
        self._media_pool.remove_requested.connect(self._remove_media)
        self._timeline.status_message.connect(
            lambda message: self.statusBar().showMessage(message, 4000)
        )

        self._timeline.selection_changed.connect(self._on_selection_changed)
        self._timeline.scene_open_requested.connect(
            lambda scene_id: self.open_scene(SceneId(scene_id))
        )
        self._scene_bar.scene_selected.connect(
            lambda scene_id: self.open_scene(SceneId(scene_id) if scene_id else None)
        )
        self._scene_bar.add_requested.connect(self._ask_new_scene)
        self._scene_bar.rename_requested.connect(self._ask_rename_scene)
        self._scene_bar.remove_requested.connect(self.remove_active_scene)
        self._scene_bar.place_requested.connect(self._ask_place_scene)
        self._inspector.commands_requested.connect(self.execute_all)
        self._inspector.preview_requested.connect(self._preview_command)
        self._inspector.curve_selected.connect(self._show_curve)
        self._graph.commands_requested.connect(self.execute_all)
        self._graph.seek_requested.connect(self._seek)

        self._subtitles.commands_requested.connect(self.execute_all)
        self._subtitles.seek_requested.connect(self._seek)
        self._subtitles.status_message.connect(
            lambda message: self.statusBar().showMessage(message, 5000)
        )

        self._chat.status_message.connect(
            lambda message: self.statusBar().showMessage(message, 5000)
        )

        self._transport.play_toggled.connect(self._playback.toggle)
        self._transport.step_requested.connect(
            lambda delta: self._seek(self._timeline.playhead + delta)
        )
        self._transport.jump_requested.connect(self._seek)
        self._transport.quality_changed.connect(self._preview.set_quality)

        self._playback.frame_changed.connect(self._on_playback_frame)
        self._playback.state_changed.connect(self._transport.set_playing)
        self._playback.failed.connect(lambda message: self.statusBar().showMessage(message, 5000))

    # --- コマンドの実行 ---

    def execute(self, command: Command) -> None:
        """コマンドを 1 つ実行して、画面を更新する

        失敗しても落とさず、状況をステータスバーへ出す 編集操作は思いどおりに
        いかないことが普通にあり、そのたびにダイアログが出ると邪魔になる
        """
        try:
            self._document.execute(self._in_active_scene(command))
        except (ValueError, KeyError) as exc:
            self.statusBar().showMessage(str(exc), 4000)
            return
        self._on_project_changed()

    def execute_all(self, commands: list[Command], label: str, *, merge: bool = False) -> None:
        """複数のコマンドを 1 回の Undo で戻せるようにまとめて実行する

        ``merge`` が真なら、直前の同じ操作の段へまとめる（:meth:`Document.checkpoint`）
        """
        if not commands:
            return
        try:
            with self._document.checkpoint(label, merge=merge):
                for command in commands:
                    self._document.execute(self._in_active_scene(command))
        except (ValueError, KeyError) as exc:
            self.statusBar().showMessage(str(exc), 4000)
        self._on_project_changed()

    def _in_active_scene(self, command: Command) -> Command:
        """開いているシーンの中で実行するよう包む メインなら包まない

        画面のパネルも AI も、見ているタイムライン（:attr:`view_project`）を相手に
        コマンドを作る 包み忘れると、シーンを開いて足したクリップがメインに入る
        """
        if self._active_scene is None or isinstance(command, InScene):
            return command
        return InScene(self._active_scene, command)

    @property
    def view_project(self) -> Project:
        """いま編集しているタイムラインを ``timeline`` に差し込んだプロジェクト

        タイムライン・プレビュー・設定パネルはこれを見る 保存と書き出しは
        いつもメイン（:attr:`document` のプロジェクト）
        """
        project = self._document.project
        if self._active_scene is None:
            return project
        scene = project.find_scene(self._active_scene)
        return project if scene is None else replace(project, timeline=scene.timeline)

    @property
    def active_scene(self) -> SceneId | None:
        return self._active_scene

    def open_scene(self, scene_id: SceneId | None) -> None:
        """編集するシーンを切り替える ``None`` ならメイン"""
        if scene_id is not None and self._document.project.find_scene(scene_id) is None:
            self.statusBar().showMessage("そのシーンは見つかりません", 4000)
            return
        if scene_id == self._active_scene:
            return
        self._playback.stop()
        self._active_scene = scene_id
        # 選んでいたクリップは別のタイムラインのもの 残すと、開いた先で
        # 「見つからない」になる
        self._timeline.select(None)
        self._on_project_changed()
        self._seek(0)

    def create_scene(self, name: str) -> SceneId | None:
        """空のシーンを作って開く"""
        name = name.strip()
        if not name:
            return None
        scene = new_scene(self._document.project, name)
        try:
            self._document.execute(AddScene(scene))
        except (ValueError, KeyError) as exc:
            self.statusBar().showMessage(str(exc), 4000)
            return None
        self._on_project_changed()
        self.open_scene(scene.id)
        return scene.id

    def rename_active_scene(self, name: str) -> None:
        if self._active_scene is not None:
            self.execute(RenameScene(self._active_scene, name))

    def remove_active_scene(self) -> None:
        """開いているシーンを消してメインへ戻る どこかに置かれていれば断られる"""
        target = self._active_scene
        if target is None:
            self.statusBar().showMessage("メインは消せません", 4000)
            return
        try:
            self._document.execute(RemoveScene(target))
        except (ValueError, KeyError) as exc:
            self.statusBar().showMessage(str(exc), 5000)
            return
        self._active_scene = None
        self._on_project_changed()

    def place_scene(self, scene_id: SceneId) -> None:
        """シーンを、いま編集しているタイムラインの再生ヘッドの位置へ置く"""
        try:
            commands = insert_scene(self.view_project, scene_id, at_frame=self._timeline.playhead)
        except KeyError as exc:
            self.statusBar().showMessage(str(exc), 4000)
            return
        scene = self._document.project.require_scene(scene_id)
        self.execute_all(commands, f"シーンを置く: {scene.name}")

    def _ask_new_scene(self) -> None:
        count = len(self._document.project.scenes) + 1
        name, accepted = QInputDialog.getText(
            self, "新しいシーン", "シーンの名前", text=f"シーン {count}"
        )
        if accepted:
            self.create_scene(name)

    def _ask_rename_scene(self) -> None:
        if self._active_scene is None:
            self.statusBar().showMessage("メインの名前は変えられません", 4000)
            return
        scene = self._document.project.require_scene(self._active_scene)
        name, accepted = QInputDialog.getText(self, "シーンの名前", "新しい名前", text=scene.name)
        if accepted:
            self.rename_active_scene(name)

    def _ask_place_scene(self) -> None:
        # 開いているシーン自身は置けない（入れ子が自分へ戻る） 選択肢から外す
        choices = [s for s in self._document.project.scenes if s.id != self._active_scene]
        if not choices:
            self.statusBar().showMessage(
                "置けるシーンがありません（先にシーンを作ってください）", 5000
            )
            return
        # 同じ名前のシーンがあると、名前から引き直したときに先頭のものを選んでしまう
        # 番号を付けて、選んだ行の位置でシーンを決める
        names = [f"{index}. {scene.name}" for index, scene in enumerate(choices, start=1)]
        name, accepted = QInputDialog.getItem(self, "シーンを置く", "置くシーン", names, 0, False)
        if accepted and name in names:
            self.place_scene(choices[names.index(name)].id)

    def undo(self) -> None:
        self._document.undo()
        self._on_project_changed()

    def redo(self) -> None:
        self._document.redo()
        self._on_project_changed()

    def _on_project_changed(self) -> None:
        root = self._document.project
        if self._active_scene is not None and root.find_scene(self._active_scene) is None:
            # 取り消しでシーンが消えたら、メインへ戻る
            self._active_scene = None
        project = self.view_project
        self._scene_bar.set_project(root, self._active_scene)
        self._timeline.set_project(project)
        self._media_pool.set_project(root)
        self._inspector.set_project(project)
        self._graph.set_project(project)
        self._subtitles.set_project(project)
        self._preview.set_project(project)
        self._playback.set_project(project)
        self._transport.set_rate(project.rate)
        self._transport.set_duration(project.duration)
        # 素材が増えたら画質を見直す 4K を 1 本置いた時点で重くなるので、
        # 置いたあとに自分で下げてもらうのでは遅い
        self._apply_auto_quality()
        self._update_history_actions()
        self._update_title()
        self.project_changed.emit(project)

    def _update_history_actions(self) -> None:
        self._undo_action.setEnabled(self._document.can_undo)
        self._redo_action.setEnabled(self._document.can_redo)
        undo_label = self._document.undo_label
        self._undo_action.setText(f"元に戻す: {undo_label}" if undo_label else "元に戻す")
        redo_label = self._document.redo_label
        self._redo_action.setText(f"やり直す: {redo_label}" if redo_label else "やり直す")

    @property
    def is_modified(self) -> bool:
        """同一性で比べる 中身の等しさで比べると、履歴 1 段ごとにツリー全体を
        比較することになり、大きなプロジェクトでタイトルの更新が重くなる
        """
        return self._document.project is not self._saved

    def _toggle_track(self, attribute: str) -> None:
        if not self._timeline.toggle_selected_track(attribute):
            self.statusBar().showMessage("先にクリップを選んでください（そのトラックが対象）", 4000)

    def _update_title(self) -> None:
        name = self._path.name if self._path is not None else self._document.project.name
        mark = " *" if self.is_modified else ""
        self.setWindowTitle(f"{name}{mark} — Kumiki")

    # --- 素材 ---

    def _import_dialog(self) -> None:
        from kumiki.ui.media_pool import MEDIA_FILTER

        names, _ = QFileDialog.getOpenFileNames(self, "素材を読み込む", "", MEDIA_FILTER)
        if names:
            self.import_media([Path(name) for name in names])

    def import_media(self, paths: list[Path]) -> None:
        """素材を読み込んでタイムラインへ置く

        複数選ばれた場合もまとめて 1 回の Undo で戻せるようにする
        10 本読み込んで 10 回取り消す、という操作は誰も望まない
        """
        commands: list[Command] = []
        failures: list[str] = []
        project = self.view_project

        for path in paths:
            try:
                media = probe_media(path)
            except ProbeError as exc:
                failures.append(str(exc))
                continue
            batch = insert_media(project, media, at_frame=None)
            for command in batch:
                project = command.apply(project)
            commands.extend(batch)
            self._analyzer.request(media, on_ready=self._on_analysis_ready)
            self._request_proxy(media)

        if commands:
            self.execute_all(commands, f"素材を読み込み: {len(paths)} 件")
        if failures:
            self.statusBar().showMessage(failures[0], 5000)
        elif commands:
            self.statusBar().showMessage(f"{len(paths)} 件を読み込んだ", 3000)

    def add_text(self) -> None:
        """再生ヘッドの位置にテキストを置く"""
        self._insert_generated(TEXT.create(), "テキストを追加")

    def add_shape(self) -> None:
        self._insert_generated(SHAPE.create(), "図形を追加")

    def add_transition(self) -> None:
        """再生ヘッドの位置に場面切り替えを置く 下のトラックの切れ目に重ねて使う"""
        self._insert_generated(TRANSITION.create(), "場面切り替えを追加")

    def _insert_generated(self, source: GeneratedSource, label: str) -> None:
        commands = insert_generated(self.view_project, source, at_frame=self._timeline.playhead)
        self.execute_all(commands, label)
        # 置いたものをすぐ選ぶ 設定パネルが開いていないと、
        # 追加したのに何も起きていないように見える
        placed = self._last_added_clip()
        if placed is not None:
            self._timeline.select(placed)

    def _last_added_clip(self) -> ClipId | None:
        """再生ヘッドの位置にある、生成オブジェクトのクリップ"""
        frame = self._timeline.playhead
        for track in reversed(list(self.view_project.timeline.video_tracks())):
            clip = track.clip_at(frame)
            if clip is not None and clip.source is not None:
                return clip.id
        return None

    def show_subtitles(self) -> None:
        """字幕パネルを前へ出す"""
        self._subtitle_dock.show()
        self._subtitle_dock.raise_()

    def transcribe(self) -> None:
        """選択中の素材を起こす パネルを出してから始める

        起こしの実行環境は既定では入っていない 未導入なら、そのダイアログが
        導入のボタンを出す（:mod:`kumiki.asr.environment` を参照）
        """
        self.show_subtitles()
        selected = self._media_pool.selected_media_id()
        if selected is not None:
            self._subtitles.select_media(selected)
        self._subtitles.transcribe()

    def _transcribe_media(self, media_id: str) -> None:
        """メディアプールの右クリックから起こす その素材を字幕パネルで選んでから始める"""
        self.show_subtitles()
        self._subtitles.select_media(MediaId(media_id))
        self._subtitles.transcribe()

    def _remove_media(self, media_id: str) -> None:
        """プールから外す タイムラインで使っていれば、理由がステータスバーに出て止まる

        外せたときだけ解析の結果（波形・サムネイル・走っている解析）も捨てる 残すと、
        もう使わない素材の波形をメモリに抱え続ける 外せなかったときは使い続けるので残す
        """
        target = MediaId(media_id)
        self.execute(RemoveMedia(target))
        if self._document.project.find_media(target) is None:
            self._analyzer.forget(target)
            self._proxies.forget(target)

    def _insert_media_by_id(self, media_id: str) -> None:
        project = self.view_project
        media = project.find_media(MediaId(media_id))
        if media is None:
            return
        self.execute_all(insert_media(project, media), f"配置: {media.name}")

    def _on_analysis_ready(self, media_id: MediaId) -> None:
        # ワーカースレッドから呼ばれる ここでウィジェットに触ると Qt が落ちるので、
        # 印だけ付けてメインスレッドのタイマーに描き直させる
        del media_id
        self._analysis_dirty = True

    def _request_proxy(self, media: MediaItem) -> None:
        """控えを作るよう頼む 設定で切っていれば何もしない

        素材を読み込む道は何本もある（普通に開く・復元する・AviUtl から
        取り込む・AI から） ここを 1 つにまとめておかないと、道ごとに
        書き分けることになる
        """
        if self._preferences.use_proxy:
            self._proxies.request(media, on_ready=self._on_proxy_ready)

    def _on_proxy_ready(self, media_id: MediaId) -> None:
        """控えができた ワーカースレッドから呼ばれる

        ウィジェットには触らず、どの素材かだけを覚える 次の間隔で、
        その素材のデコーダを開き直させる（開いたままだと元のファイルを
        掴み続けるので、描き直すだけでは控えに変わらない）
        """
        with self._proxied_lock:
            self._proxied.add(media_id)

    def _flush_analysis(self) -> None:
        # AI から始めた起こしの様子も、ついでにここで拾う 専用のタイマーを
        # もう 1 本増やすほどの頻度ではない
        self._subtitles.poll_transcription()
        # 控えができた 開きっぱなしのデコーダは元のファイルを掴んだままなので、
        # 開き直させる（描き直すだけでは切り替わらない）
        # できた素材のぶんだけにする 全部開き直すと、別の素材の控えが
        # できるたびに再生中のクリップまでシークし直すことになる
        with self._proxied_lock:
            ready, self._proxied = self._proxied, set()
        if ready:
            self._preview.reload_sources(ready)
        if not self._analysis_dirty:
            return
        self._analysis_dirty = False
        self._timeline.update()

    # --- 再生とシーク ---

    def _seek(self, frame: int) -> None:
        frame = max(0, min(frame, self.view_project.duration))
        self._timeline.set_playhead(frame)
        self._show_frame(frame)
        self._playback.set_frame(frame)

    def _show_frame(self, frame: int) -> None:
        self._preview.set_frame(frame)
        self._transport.set_frame(frame)
        self._inspector.set_frame(frame)
        self._graph.set_frame(frame)
        self._subtitles.set_frame(frame)

    def _on_selection_changed(self, clip_id: str) -> None:
        selected = ClipId(clip_id) if clip_id else None
        # 何本も選んでいれば、設定パネルは主の 1 本を出しつつ、触った設定を全部へ当てる
        chosen = self._timeline.selected_clips
        ordered = (selected, *(c for c in chosen if c != selected)) if selected else ()
        self._inspector.set_selection(tuple(c for c in ordered if c is not None))
        if selected is None:
            self._graph.set_path(None)

    def _show_curve(self, path: ParamPath) -> None:
        self._graph.set_path(path)
        self._graph_dock.show()
        self._graph_dock.raise_()

    def _preview_command(self, command: Command) -> None:
        """履歴に残さず、プレビューだけ更新する

        スライダーのドラッグ中に呼ばれる 1 回のドラッグで数十の取り消し段を
        作らないための逃げ道で、指を離した時点で本来のコマンドが飛んでくる
        """
        try:
            preview = command.apply(self.view_project)
        except (ValueError, KeyError):
            return
        self._preview.set_project(preview)
        self._preview.update()

    def _on_playhead_moved(self, frame: int) -> None:
        self._show_frame(frame)
        self._playback.set_frame(frame)

    def _on_playback_frame(self, frame: int) -> None:
        self._timeline.set_playhead(frame)
        self._show_frame(frame)

    # --- ファイル ---

    def new_project(self) -> None:
        """解像度とフレームレートを尋ねてから作る フレームレートはあとで変えられない"""
        from kumiki.ui.project_settings_dialog import ProjectSettingsDialog

        if not self._confirm_discard():
            return
        dialog = ProjectSettingsDialog(ProjectSettings(), self, new=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._playback.stop()
        self._document.reset(Project.create(dialog.settings()))
        self._path = None
        self._release_lock()
        self._mark_saved()
        self._on_project_changed()
        self._seek(0)

    def _mark_saved(self) -> None:
        """いまの状態を「保存済み」とする 守るものが無くなるので退避も消す"""
        self._saved = self._document.project
        self._autosaved = self._saved
        self._recovery.clear()

    def _confirm_discard(self) -> bool:
        """変更を捨ててよいか 保存を選べば保存してから真を返す"""
        if not self._confirm_unsaved or not self.is_modified:
            return True
        answer = QMessageBox.question(
            self,
            "保存していない変更",
            "変更を保存しますか",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_project()
        return answer == QMessageBox.StandardButton.Discard

    def open_project(self) -> None:
        if not self._confirm_discard():
            return
        # 改名前（NovaEdit）に保存したものも開けるようにしておく
        patterns = " ".join(f"*{s}" for s in (SUFFIX, *LEGACY_SUFFIXES))
        name, _ = QFileDialog.getOpenFileName(
            self, "プロジェクトを開く", "", f"Kumiki プロジェクト ({patterns})"
        )
        if not name:
            return
        try:
            project = load_project(Path(name))
        except ProjectFileError as exc:
            QMessageBox.warning(self, "開けない", str(exc))
            return
        if not self._claim(Path(name)):
            return

        self._playback.stop()
        self._document.reset(project)
        self._path = Path(name)
        self._mark_saved()
        self._on_project_changed()
        self._seek(0)
        for media in project.media:
            self._analyzer.request(media, on_ready=self._on_analysis_ready)
            self._request_proxy(media)

    def save_project(self) -> bool:
        """保存する 保存できたら真 名前がまだ無ければ尋ねる"""
        if self._path is None:
            return self.save_as()
        project = self._document.project
        note = ""
        try:
            backup_before_save(self._path)
        except OSError as exc:
            # 控えが取れなくても保存は止めない 止めると、控えのために
            # いまの作業のほうを失う
            note = f"（バックアップは作れなかった: {exc}）"
        try:
            save_project(project, self._path)
        except OSError as exc:
            QMessageBox.warning(self, "保存できない", f"{self._path}\n{exc}")
            return False
        self._mark_saved()
        self._update_title()
        self.statusBar().showMessage(f"保存した: {self._path}{note}", 5000 if note else 3000)
        return True

    def save_as(self) -> bool:
        suggested = self._path or Path(f"{self._document.project.name}{SUFFIX}")
        name, _ = QFileDialog.getSaveFileName(
            self, "名前を付けて保存", str(suggested), f"Kumiki プロジェクト (*{SUFFIX})"
        )
        if not name:
            return False
        # 保存できたときだけ新しい名前に切り替える 先に切り替えると、失敗しても
        # タイトル・次の保存先・退避のメモが、書けなかった場所を指したままになる
        previous = self._path
        if not self._claim(Path(name)):
            return False
        self._path = Path(name)
        saved = self.save_project()
        if not saved:
            # 元の名前へ戻すのは、元の錠を取り直せたときだけ 取れないまま戻すと、
            # 錠は新しい名前、保存先は元の名前、と食い違う
            if previous is None:
                self._release_lock()
                self._path = None
            elif self._claim(previous):
                self._path = previous
        self._update_title()
        return saved

    def _claim(self, path: Path) -> bool:
        """このファイルを開いている窓はここ、と錠で示す 別の窓が開いていれば尋ねる

        止めはしない 読み返すだけのこともあるので、知らせたうえで本人に選ばせる
        知らせずに開けると、両方で保存したとき後から保存した方が黙って勝つ
        """
        folder = project_presence_dir(path)
        if self._project_lock is not None and self._project_lock.path.parent == folder:
            return True
        if others_holding(folder) and self._confirm_unsaved:
            answer = QMessageBox.warning(
                self,
                "別の窓で開いています",
                f"{path.name} は別の Kumiki の窓で開かれています\n"
                "両方で保存すると、あとから保存した方の内容だけが残ります",
                QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Open:
                return False
        # 「それでも開く」でも自分の錠は置く 置かないと、先の窓が閉じたあとに
        # 開いた窓からこの窓が見えない
        self._release_lock()
        self._project_lock = hold_new(folder)
        return True

    def _release_lock(self) -> None:
        if self._project_lock is not None:
            self._project_lock.release()
            self._project_lock = None

    def open_backup_folder(self) -> None:
        """控えは %LOCALAPPDATA% の奥にあり、場所を知らないと辿り着けない"""
        if self._path is None:
            self.statusBar().showMessage("まだ保存していないので、バックアップはありません", 5000)
            return
        folder = backup_folder(self._path)
        if not folder.is_dir():
            self.statusBar().showMessage("バックアップは上書き保存したときに作られます", 5000)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def edit_settings(self) -> None:
        """プロジェクト設定を開く いまは解像度だけ変えられる"""
        from kumiki.ui.project_settings_dialog import ProjectSettingsDialog

        settings = self._document.project.settings
        dialog = ProjectSettingsDialog(settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        width, height = dialog.resolution()
        if (width, height) != settings.resolution:
            self.execute(SetResolution(width, height))

    # --- 退避と復元 ---

    def autosave(self) -> None:
        """保存していない変更を退避する タイマーから呼ばれる

        前回から変わっていなければ書かない 放置しているあいだ 30 秒ごとに
        同じ中身を書き直すのは、ディスクを傷めるだけで何も守らない
        """
        project = self._document.project
        if project is self._autosaved:
            return
        try:
            if self.is_modified:
                self._recovery.save(project, self._path)
            else:
                self._recovery.clear()
        except OSError as exc:
            self.statusBar().showMessage(f"自動退避に失敗した: {exc}", 5000)
            return
        self._autosaved = project

    def offer_recovery(self) -> None:
        """前回落ちた作業が残っていれば、復元するか尋ねる 起動の直後に呼ぶ"""
        from kumiki.ui.recovery_dialog import RecoveryDialog

        while entries := find_orphans():
            dialog = RecoveryDialog(entries, self)
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.choice is None:
                return
            action, entry = dialog.choice
            if action == "discard":
                discard(entry)
                continue
            self.restore_recovery(entry)
            return

    def restore_recovery(self, entry: RecoveryEntry) -> bool:
        """退避を開く 保存はしないので、開いた直後は「変更あり」になる

        元の退避は、この起動の退避へ書き写してから捨てる 先に捨てると、
        書き写す前に落ちたときに何も残らない
        """
        try:
            project = load_project(entry.path)
        except ProjectFileError as exc:
            QMessageBox.warning(self, "復元できない", str(exc))
            return False
        # load_project は「無題」をファイル名で置き換える 退避のファイル名は
        # 意味の無い英数字なので、退避したときの名前へ戻す
        project = project.renamed(entry.name)
        if entry.source is not None and not self._claim(entry.source):
            return False

        self._playback.stop()
        self._document.reset(project)
        self._path = entry.source
        self._saved = None
        self._on_project_changed()
        self._seek(0)
        for media in project.media:
            self._analyzer.request(media, on_ready=self._on_analysis_ready)
            self._request_proxy(media)

        self.autosave()
        if self._autosaved is project:
            discard(entry)
        self.statusBar().showMessage("前回の作業を復元した まだ保存していません", 6000)
        return True

    def export(self) -> None:
        self._playback.stop()
        ExportDialog(self._document.project, self).exec()

    # --- AviUtl 互換 ---

    def import_exo(self) -> None:
        """``.exo`` / ``.exa`` をタイムラインへ読み込む

        参照している素材は先に読み込んでから対応付ける 素材が見つからなくても
        止めない テキストや図形だけでも入る方が使い出がある
        """
        from kumiki.compat.aviutl.exo import ExoParseError, load_exo
        from kumiki.compat.aviutl.mapping import map_exo

        name, _ = QFileDialog.getOpenFileName(
            self, "AviUtl のオブジェクトを読み込む", "", EXO_FILTER
        )
        if not name:
            return

        source = Path(name)
        try:
            exo = load_exo(source)
        except ExoParseError as exc:
            QMessageBox.warning(self, "読み込めない", str(exc))
            return

        media, missing = self._resolve_exo_media(exo, source)
        commands = map_exo(exo, self.view_project, media=media)
        if not commands:
            self.statusBar().showMessage("読み込めるオブジェクトがありませんでした", 5000)
            return

        self.execute_all(commands, f"AviUtl から読み込み: {source.name}")
        note = f"{source.name} から {len(exo.objects)} 個を読み込んだ"
        if missing:
            note += f"（素材 {len(missing)} 件が見つかりません）"
        self.statusBar().showMessage(note, 6000)

    def _resolve_exo_media(
        self, exo: ExoFile, source: Path
    ) -> tuple[dict[str, MediaId], list[str]]:
        """``.exo`` が参照している素材を読み込む

        相対パスは ``.exo`` のある場所からも探す AviUtl のファイルは素材と
        一緒に配られることがある
        """
        from kumiki.compat.aviutl.mapping import media_paths

        found: dict[str, MediaId] = {}
        missing: list[str] = []
        for raw in media_paths(exo):
            candidates = [Path(raw), source.parent / Path(raw).name]
            path = next((c for c in candidates if c.exists()), None)
            if path is None:
                missing.append(raw)
                continue
            try:
                media = probe_media(path)
            except ProbeError:
                missing.append(raw)
                continue
            self.execute(AddMedia(media))
            self._analyzer.request(media, on_ready=self._on_analysis_ready)
            self._request_proxy(media)
            found[raw] = media.id
        return found, missing

    def show_templates(self) -> None:
        """テンプレートの棚を開いて、選ばれたものを反映する

        「置く」と「着せる」で行き先が違うだけで、どちらも 1 回の Undo で戻る
        """
        from kumiki.compat.catalog import place, restyle
        from kumiki.ui.template_dialog import TemplateDialog

        dialog = TemplateDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.choice is None:
            return

        action, objects = dialog.choice
        if action == "restyle":
            clip_id = self.selected_clip
            located = (
                self.view_project.timeline.locate_clip(clip_id) if clip_id is not None else None
            )
            if located is None:
                self.statusBar().showMessage("先にテキストのクリップを選んでください", 5000)
                return
            commands = restyle(objects, located[1])
            if not commands:
                self.statusBar().showMessage("テキストのクリップにしか適用できません", 5000)
                return
            self.execute_all(commands, "テンプレートを適用")
            self.statusBar().showMessage("テンプレートを適用した（文字と長さはそのまま）", 5000)
            return

        commands = place(objects, self.view_project, at_frame=self._timeline.playhead)
        if not commands:
            self.statusBar().showMessage("置けるオブジェクトがありませんでした", 5000)
            return
        self.execute_all(commands, "テンプレートを配置")
        self.statusBar().showMessage(f"{len(commands)} 個を置いた", 5000)

    def rescan_scripts(self) -> None:
        """スクリプトのフォルダを読み直す"""
        from kumiki.compat.aviutl.catalog import script_catalog

        catalog = script_catalog()
        catalog.scan()
        count = catalog.register_all()
        self.statusBar().showMessage(f"スクリプトを {count} 本読み込んだ", 4000)

    def open_script_folder(self) -> None:
        """スクリプトを置く場所をエクスプローラで開く"""
        from kumiki.compat.aviutl.catalog import script_catalog

        roots = script_catalog().roots
        if not roots:
            self.statusBar().showMessage("スクリプトフォルダが設定されていません", 4000)
            return
        target = roots[0]
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def show_compatibility(self) -> None:
        """互換性レポートを出す"""
        from kumiki.ui.compat_dialog import CompatibilityDialog

        CompatibilityDialog(parent=self).exec()

    # --- AI 連携（EditorHost の実装）---
    #
    # AI からの操作も UI と同じ入口を通す ここが増えないようにしておけば、
    # 「UI ではできるが AI ではできない」も、その逆も生まれない

    @property
    def document(self) -> Document:
        return self._document

    @property
    def playhead(self) -> int:
        return self._timeline.playhead

    def seek(self, frame: int) -> None:
        self._seek(frame)

    @property
    def selected_clip(self) -> ClipId | None:
        return self._timeline.selected_clip

    def select_clip(self, clip_id: ClipId | None) -> None:
        self._timeline.select(clip_id)

    @property
    def selected_clips(self) -> tuple[ClipId, ...]:
        return self._timeline.selected_clips

    def select_clips(self, clip_ids: list[ClipId]) -> None:
        self._timeline.set_selection(clip_ids)

    def apply_commands(self, commands: list[Command], label: str) -> None:
        """AI からのコマンドを実行する

        UI 経由の :meth:`execute_all` と違い、失敗を握り潰さず例外にする
        AI はエラーの文面を読んで次の手を決めるので、黙って何も起きないのが
        いちばん困る
        """
        if not commands:
            return
        try:
            with self._document.checkpoint(label):
                for command in commands:
                    self._document.execute(self._in_active_scene(command))
        except (ValueError, KeyError) as exc:
            raise ToolError(str(exc)) from exc
        finally:
            self._on_project_changed()

    @property
    def project(self) -> Project:
        """AI が読むプロジェクト 画面と同じく、開いているシーンを見る"""
        return self.view_project

    def set_active_scene(self, scene_id: SceneId | None) -> None:
        self.open_scene(scene_id)

    def stop_playback(self) -> None:
        self._playback.stop()

    def render_png(self, frame: int, *, width: int) -> bytes:
        """そのフレームを合成して PNG にする

        プレビューのウィジェットとは別のコンテキストで描く 再生用の資源を
        取り合わないようにするためで、代わりに 1 つ余分にコンテキストを持つ

        AI が編集の結果を目で確かめるためのもので、書き出しではない 開いているシーンを
        描く AI の読み取り（``list_clips`` など）もそのシーンが相手なので、メインを描くと
        AI が見ているクリップと絵が食い違う
        """
        project = self.view_project
        full_width = project.settings.width
        divisor = max(1, round(full_width / max(width, 1)))

        renderer = self._ai_renderer
        if renderer is None:
            renderer = FrameRenderer(project, quality=RenderQuality(divisor))
            self._ai_renderer = renderer
        else:
            renderer.set_project(project)
            renderer.set_quality(RenderQuality(divisor))

        image = renderer.render(frame)
        height, image_width = image.shape[0], image.shape[1]
        picture = QImage(
            image.tobytes(), image_width, height, image_width * 4, QImage.Format.Format_RGBA8888
        )
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        # QImage.save の書式引数は、この PySide6 では str しか受け取らない
        # （型情報は bytes だと言う） 食い違いを避けるため QImageWriter を使う
        if not QImageWriter(buffer, b"PNG").write(picture):
            raise ToolError("プレビュー画像を作れませんでした")
        return bytes(buffer.data().data())

    def probe(self, path: Path) -> MediaItem:
        try:
            return probe_media(path)
        except ProbeError as exc:
            raise ToolError(str(exc)) from exc

    def analyze(self, media: MediaItem) -> None:
        self._analyzer.request(media, on_ready=self._on_analysis_ready)
        self._request_proxy(media)

    def waveform(self, media: MediaItem) -> Waveform | None:
        return self._analyzer.waveform(media)

    def start_transcription(self, media_id: MediaId, model: str) -> str:
        return self._subtitles.start_transcription(media_id, model)

    def transcription_status(self) -> str:
        return self._subtitles.transcription_status()

    def show_chat(self) -> None:
        """AI パネルを前へ出す"""
        self._chat_dock.show()
        self._chat_dock.raise_()

    # --- 終了 ---

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt の命名規約
        if not self._confirm_discard():
            event.ignore()
            return
        # 並びを保存できなくても終了は止めない 次の起動が既定の並びになるだけ
        with contextlib.suppress(OSError):
            self._workspace.save(self)
        # ここまで来たら変更は保存したか、捨てると決めたもの 退避は要らない
        self._autosave_timer.stop()
        self._recovery.close()
        self._release_lock()

        # 解放の順番が大事 GL 資源はコンテキストが生きているうちに、
        # 再生スレッドはウィジェットが消える前に畳む
        self._refresh_timer.stop()
        self._chat.close_session()
        self._playback.close()
        if self._ai_renderer is not None:
            self._ai_renderer.close()
            self._ai_renderer = None
        self._analyzer.close()
        self._proxies.close()
        self._preview.shutdown()
        super().closeEvent(event)
