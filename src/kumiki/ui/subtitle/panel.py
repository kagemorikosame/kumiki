"""字幕パネル

素材を選び、その起こし結果を一覧で編集する 時刻の列に出るのは**タイムライン上の
位置**で、素材内の時刻ではない 編集中に見たいのは「動画の何分何秒に出るか」で、
素材のどこかは分かっても仕方がない その変換は投影
（:mod:`kumiki.core.projection`）が引き受ける

自分ではプロジェクトを書き換えない 操作はすべてコマンドとして外へ出す
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kumiki.asr import JobKind, TranscribeOptions, TranscriptionService, default_backend
from kumiki.asr.service import Job
from kumiki.core.commands import (
    Command,
    MergeWithNext,
    RemoveSegment,
    RippleCut,
    SetSegmentText,
    SetTranscript,
    SplitSegment,
    burn_subtitles,
)
from kumiki.core.io import SUBTITLE_FILTER, save_subtitles
from kumiki.core.jetcut import plan_cuts
from kumiki.core.model import MediaId, MediaItem, Project, SegmentId, TranscriptSegment
from kumiki.core.projection import project_clip
from kumiki.core.timebase import format_timecode
from kumiki.effects.sources import TEXT
from kumiki.engine.audio.silence import SilenceOptions, detect_silence, keep_speech
from kumiki.engine.cache import MediaAnalyzer
from kumiki.ui.subtitle.dialogs import CleanupDialog, JetCutDialog
from kumiki.ui.subtitle.transcribe_dialog import TranscribeDialog
from kumiki.ui.theme import Colors

__all__ = ["SubtitlePanel"]

#: 焼き込むテキストの既定 下寄せで、縁取りを付けて読めるようにする
BURN_DEFAULTS = {"size": 48.0, "pos_y": -380.0, "border_width": 4.0}


#: 時刻の列に足す余白（画素） 文字の幅ぴったりだと読みにくい
TIME_COLUMN_PADDING = 24

#: 時刻の列の幅を決める見本の長さ（フレーム） 1 時間ぶん
#: これより短い動画でも、この幅は空けておく 桁が増えるたびに列が動くと目が疲れる
MIN_TIME_SAMPLE_FRAMES = 108_000


class SubtitlePanel(QWidget):
    """素材ごとの字幕の一覧と編集"""

    #: 編集操作 引数はコマンドの一覧と、履歴に出す操作名
    commands_requested = Signal(list, str)
    #: 字幕をクリックしたときの移動先（フレーム）
    seek_requested = Signal(int)
    #: ステータスバーへ出す文言
    status_message = Signal(str)

    def __init__(
        self, project: Project, analyzer: MediaAnalyzer, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._analyzer = analyzer
        self._media_id: MediaId | None = None
        self._frame = 0
        #: 表示中の行に対応する字幕 行番号から引く
        self._rows: list[tuple[SegmentId, int, int]] = []
        #: 一覧を作ったときの中身の印 同じなら作り直さない
        self._signature: tuple[object, ...] | None = None
        self._updating = False
        #: 起こしの実行係 バックエンドの読み込みは重いので、初めて使うときに作る
        self._service: TranscriptionService | None = None
        #: AI から始めた起こし ダイアログを開かずに走らせる経路
        self._job: Job | None = None
        self._job_media: MediaId | None = None
        self._job_note = ""

        self._build()
        self.set_project(project)

    # --- 組み立て ---

    def _build(self) -> None:
        self._media = QComboBox(self)
        self._media.currentIndexChanged.connect(self._on_media_changed)

        self._transcribe_button = self._button("起こす…", self.transcribe)
        self._clean_button = self._button("整形…", self.clean)
        self._cut_button = self._button("無音カット…", self.jet_cut)
        self._burn_button = self._button("焼き込み", self.burn)
        self._export_button = self._button("書き出し…", self.export_file)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self._media, 1)
        top.addWidget(self._transcribe_button)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        for button in (
            self._clean_button,
            self._cut_button,
            self._burn_button,
            self._export_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)

        self._table = QTableWidget(0, 2, self)
        self._table.setHorizontalHeaderLabels(["時刻", "本文"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_menu)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.setWordWrap(True)

        header = self._table.horizontalHeader()
        # 時刻の幅は**固定**にする 中身に合わせると、1 行足すたびに Qt が
        # 全部の行を測り直すので、字幕 2000 本では作り直しに 6 秒かかっていた
        # 時刻は桁数の決まった文字列なので、見本の幅を 1 度測れば足りる
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(0, self._time_column_width())
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # 幅が変われば折り返しの行数も変わる 高さを取り直さないと、2 行目が
        # 隠れて本文の末尾が読めなくなる 本文の列だけを見る（時刻は変わらない）
        header.sectionResized.connect(self._on_section_resized)

        self._empty = QLabel("音声を持つ素材を選んでください", self)
        self._empty.setStyleSheet(f"color: {Colors.TEXT_MUTED.name()}; padding: 8px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addLayout(top)
        layout.addLayout(actions)
        layout.addWidget(self._empty)
        layout.addWidget(self._table, 1)

    def _button(self, text: str, slot: object) -> QPushButton:
        button = QPushButton(text, self)
        button.clicked.connect(slot)
        return button

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt の命名規約
        super().resizeEvent(event)
        self._table.resizeRowsToContents()

    # --- 外から差し替えるもの ---

    def set_project(self, project: Project) -> None:
        self._project = project
        # 長さが変わると、時刻の桁も変わる 幅を取り直さないと切れる
        self._table.horizontalHeader().resizeSection(0, self._time_column_width())
        self._reload_media()
        self._reload_rows()

    def set_frame(self, frame: int) -> None:
        """再生ヘッドの位置 いま出ている字幕を強調する"""
        if frame == self._frame:
            return
        self._frame = frame
        self._highlight()

    @property
    def media_id(self) -> MediaId | None:
        return self._media_id

    def select_media(self, media_id: MediaId) -> None:
        index = self._media.findData(str(media_id))
        if index >= 0:
            self._media.setCurrentIndex(index)

    # --- 一覧 ---

    def _time_column_width(self) -> int:
        """時刻の列の幅 一番長くなるタイムコードを測って決める

        見本を決め打ちにすると、100 時間を超えるタイムラインで時が 3 桁になり、
        2 桁ぶんの幅で切れる 実際の長さから決める（短いときは見本の方を使う
        短い動画で時刻の列が細くなりすぎると、桁が増えたときに毎回揺れる）
        """
        longest = max(self._project.duration, MIN_TIME_SAMPLE_FRAMES)
        sample = format_timecode(longest, self._project.rate)
        return self._table.fontMetrics().horizontalAdvance(sample) + TIME_COLUMN_PADDING

    def _on_section_resized(self, index: int, _old: int, _new: int) -> None:
        """列の幅が変わったら、折り返しに合わせて行の高さを取り直す

        本文の列だけを見る 時刻の列は幅が変わらないうえ、作り直しの最中に
        呼ばれると 1 行ごとに全部の行を測り直すことになる
        """
        if index == 1 and not self._updating:
            self._table.resizeRowsToContents()

    def _reload_media(self) -> None:
        """素材の選択欄を作り直す 選択は ID で復元する"""
        candidates = [item for item in self._project.media if item.has_audio]
        previous = self._media_id

        self._updating = True
        self._media.clear()
        for item in candidates:
            self._media.addItem(item.name, str(item.id))
        self._updating = False

        if not candidates:
            self._media_id = None
        elif previous is not None and any(item.id == previous for item in candidates):
            self.select_media(previous)
        else:
            self._media_id = candidates[0].id
            self._media.setCurrentIndex(0)

        has_media = bool(candidates)
        self._transcribe_button.setEnabled(has_media)
        self._empty.setVisible(not has_media)
        self._table.setVisible(has_media)

    def _on_media_changed(self) -> None:
        if self._updating:
            return
        data = self._media.currentData()
        self._media_id = MediaId(str(data)) if data else None
        self._reload_rows()

    def _current_media(self) -> MediaItem | None:
        if self._media_id is None:
            return None
        return self._project.find_media(self._media_id)

    def _segments(self) -> tuple[TranscriptSegment, ...]:
        media = self._current_media()
        if media is None or media.transcript is None:
            return ()
        return media.transcript.segments

    def _rows_signature(self) -> tuple[object, ...]:
        """一覧の中身が変わったかを見るための印

        字幕そのものと、それがタイムラインのどこに出るかで決まる
        モデルは作り替えでしか変わらないので、字幕の一覧は同じものかどうかを
        ``id`` で見れば足りる（中身を突き合わせると本数ぶんの手間が掛かる）
        """
        media = self._current_media()
        if media is None:
            return (None,)
        clips = tuple(
            (str(clip.id), clip.timeline_start, clip.duration, clip.source_in, clip.speed)
            for track in self._project.timeline.tracks
            for clip in track.clips
            if clip.media_id == media.id
        )
        return (str(media.id), id(media.transcript), self._project.rate, clips)

    def _reload_rows(self) -> None:
        """一覧を作り直す

        時刻はタイムライン上の位置 同じ素材を 2 回置いていれば最初の 1 回の
        位置を出す 編集の入口としてはそれで足り、2 か所の時刻を並べても迷う

        中身が前と同じなら作り直さない 編集のたびに呼ばれる所で、字幕が
        2000 本あると作り直しだけで 70ms 掛かる 字幕に関係のない編集
        （クリップの色を変えるなど）でそれを払うのは無駄
        """
        signature = self._rows_signature()
        if signature == self._signature and self._table.rowCount() == len(self._segments()):
            return
        # 印は**作り終えてから**立てる 途中で落ちたのに立てると、同じ
        # プロジェクトを開き直しても作り直さず、半端な表が残ったままになる
        self._signature = None
        media = self._current_media()
        segments = self._segments()
        placement = self._placement(media) if media is not None else {}

        self._updating = True
        # 描き直しを止めてから中身を入れ替える 途中の状態を描くと、
        # 1 行ごとに並べ直しが走って本数の 2 乗で遅くなる
        # 途中で落ちても必ず戻す 戻し損ねると、表が固まったまま何も映らない
        self._table.setUpdatesEnabled(False)
        try:
            # いったん空にしてから伸ばす 置き換えると、古い中身を捨てる手間が
            # 1 行ずつ掛かる
            self._table.setRowCount(0)
            self._table.setRowCount(len(segments))
            self._rows = []
            for row, segment in enumerate(segments):
                start, end = placement.get(segment.id, (-1, -1))
                self._rows.append((segment.id, start, end))

                when = format_timecode(start, self._project.rate) if start >= 0 else "—"
                time_item = QTableWidgetItem(when)
                time_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if start < 0:
                    # タイムラインに出ていない字幕 素材を切った先で使われていない範囲
                    time_item.setForeground(Colors.TEXT_MUTED)
                    time_item.setToolTip("いまのタイムラインには出ていません")
                self._table.setItem(row, 0, time_item)
                self._table.setItem(row, 1, QTableWidgetItem(segment.text))
        finally:
            self._table.setUpdatesEnabled(True)
            self._updating = False
        self._signature = signature

        self._table.resizeRowsToContents()
        self._update_actions()
        self._highlight()

    def _placement(self, media: MediaItem) -> dict[SegmentId, tuple[int, int]]:
        """字幕がタイムラインのどこに出るか 最初に現れた位置だけを持つ"""
        found: dict[SegmentId, tuple[int, int]] = {}
        for track in self._project.timeline.tracks:
            for clip in track.clips:
                if clip.media_id != media.id:
                    continue
                for projected in project_clip(clip, media, self._project.rate, track.id):
                    key = projected.segment.id
                    span = (projected.start_frame, projected.end_frame)
                    if key not in found or span < found[key]:
                        found[key] = span
        return found

    def _update_actions(self) -> None:
        has_segments = bool(self._segments())
        self._clean_button.setEnabled(has_segments)
        self._burn_button.setEnabled(has_segments)
        self._export_button.setEnabled(has_segments)
        media = self._current_media()
        self._cut_button.setEnabled(media is not None)

    def _highlight(self) -> None:
        """再生ヘッドの位置にある字幕を選ぶ"""
        for row, (_, start, end) in enumerate(self._rows):
            if start <= self._frame < end:
                if self._table.currentRow() != row:
                    self._updating = True
                    self._table.selectRow(row)
                    item = self._table.item(row, 1)
                    if item is not None:
                        self._table.scrollToItem(item)
                    self._updating = False
                return

    # --- 操作 ---

    def _on_row_selected(self) -> None:
        if self._updating:
            return
        row = self._table.currentRow()
        if 0 <= row < len(self._rows):
            _, start, _ = self._rows[row]
            if start >= 0:
                self.seek_requested.emit(start)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != 1 or self._media_id is None:
            return
        row = item.row()
        if not (0 <= row < len(self._rows)):
            return
        segment_id = self._rows[row][0]
        self.commands_requested.emit(
            [SetSegmentText(self._media_id, segment_id, item.text())], "字幕を編集"
        )

    def _show_menu(self, position: QPoint) -> None:
        row = self._table.currentRow()
        if not (0 <= row < len(self._rows)) or self._media_id is None:
            return
        segment_id = self._rows[row][0]

        menu = QMenu(self)
        jump = menu.addAction("ここへ移動")
        copy = menu.addAction("文字をコピー")
        menu.addSeparator()
        split = menu.addAction("再生ヘッドで分割")
        merge = menu.addAction("次と結合")
        remove = menu.addAction("削除")
        chosen = menu.exec(self._table.viewport().mapToGlobal(position))

        if chosen is jump:
            start = self._rows[row][1]
            if start >= 0:
                self.seek_requested.emit(start)
        elif chosen is copy:
            item = self._table.item(row, 1)
            clipboard = QGuiApplication.clipboard()
            if item is not None and clipboard is not None:
                clipboard.setText(item.text())
        elif chosen is remove:
            self.commands_requested.emit([RemoveSegment(self._media_id, segment_id)], "字幕を削除")
        elif chosen is merge:
            self.commands_requested.emit([MergeWithNext(self._media_id, segment_id)], "字幕を結合")
        elif chosen is split:
            self._split_at_playhead(segment_id)

    def _split_at_playhead(self, segment_id: SegmentId) -> None:
        """再生ヘッドのソース時刻で字幕を割る"""
        media = self._current_media()
        if media is None or self._media_id is None:
            return
        at = self._source_time(media, self._frame)
        if at is None:
            self.status_message.emit("再生ヘッドがこの素材の上にありません")
            return
        self.commands_requested.emit([SplitSegment(self._media_id, segment_id, at)], "字幕を分割")

    def _source_time(self, media: MediaItem, frame: int) -> Fraction | None:
        """タイムラインのフレームを、この素材のソース秒へ"""
        for track in self._project.timeline.tracks:
            for clip in track.clips:
                if clip.media_id != media.id or not clip.contains(frame):
                    continue
                elapsed = (frame - clip.timeline_start) * self._project.rate.frame_duration
                return clip.source_in + elapsed * clip.speed
        return None

    # --- 起こし・整形・カット ---

    def transcribe(self) -> None:
        media = self._current_media()
        if media is None:
            return
        if self._service is None:
            # バックエンドの生成はここで初めて行う 実行環境が未導入でも
            # パネルは開けるようにしておく
            self._service = TranscriptionService(default_backend())

        dialog = TranscribeDialog(media, self._service, self)
        if dialog.exec() and dialog.transcript is not None:
            self.commands_requested.emit(
                [SetTranscript(media.id, dialog.transcript)], f"字幕を起こす: {media.name}"
            )

    def start_transcription(self, media_id: MediaId, model: str) -> str:
        """起こしを始める AI からの依頼を受ける入口

        ダイアログを開かずに走らせる 数分かかるので、終わったかどうかは
        :meth:`transcription_status` で見る
        """
        media = self._project.find_media(media_id)
        if media is None:
            raise KeyError(f"素材が見つからない: {media_id}")
        if self._service is None:
            self._service = TranscriptionService(default_backend())
        if not self._service.backend.is_available():
            raise RuntimeError("起こしの実行環境が入っていません 字幕パネルから導入できます")

        options = TranscribeOptions(model=model)
        self._job = self._service.start(media.id, media.path, options)
        self._job_media = media.id
        self._job_note = "始めた"
        self.select_media(media.id)
        return f"{media.name} の起こしを始めました"

    def transcription_status(self) -> str:
        """走っている起こしの様子"""
        return self.poll_transcription()

    def poll_transcription(self) -> str:
        """AI から始めた起こしの様子を拾い、終わっていれば結果を取り込む

        定期的に呼ばれる AI が結果を聞きに来なかった場合でも、起こした内容が
        捨てられないようにするため
        """
        job = self._job
        if job is None:
            return self._job_note or "起こしは走っていません"
        for event in job.poll():
            if event.kind is JobKind.PROGRESS:
                self._job_note = f"{int(event.ratio * 100)}% — {event.message}"
            elif event.kind is JobKind.DONE and event.transcript is not None:
                self._job = None
                self._job_note = event.message
                if self._job_media is not None:
                    self.commands_requested.emit(
                        [SetTranscript(self._job_media, event.transcript)], "字幕を起こす"
                    )
            else:
                self._job = None
                self._job_note = event.message or "終了した"
        return self._job_note

    def clean(self) -> None:
        media = self._current_media()
        if media is None or media.transcript is None:
            return
        dialog = CleanupDialog(media.transcript, self)
        if dialog.exec():
            self.commands_requested.emit(
                [SetTranscript(media.id, dialog.result_transcript())], "字幕を整形"
            )

    def jet_cut(self) -> None:
        media = self._current_media()
        if media is None:
            return
        waveform = self._analyzer.waveform(media)
        if waveform is None:
            self.status_message.emit("波形の解析がまだ終わっていません")
            return

        def estimate(options: SilenceOptions, protect: bool) -> tuple[int, float]:
            ranges = self._plan(media, options, protect)
            frames = sum(end - start for start, end in ranges)
            return len(ranges), float(frames * self._project.rate.frame_duration)

        dialog = JetCutDialog(
            has_transcript=media.transcript is not None, estimate=estimate, parent=self
        )
        if not dialog.exec():
            return

        ranges = self._plan(media, dialog.options(), dialog.keep_speech)
        if not ranges:
            self.status_message.emit("切る場所が見つかりませんでした")
            return
        self.commands_requested.emit([RippleCut(ranges)], f"無音カット: {len(ranges)} か所")

    def _plan(
        self, media: MediaItem, options: SilenceOptions, protect: bool
    ) -> tuple[tuple[int, int], ...]:
        """無音の検出からタイムライン上の切る範囲までを一続きに"""
        waveform = self._analyzer.waveform(media)
        if waveform is None:
            return ()
        silences = detect_silence(waveform, options)
        if protect and media.transcript is not None:
            silences = keep_speech(silences, media.transcript)
        return plan_cuts(self._project, media.id, silences)

    def burn(self) -> None:
        template = TEXT.create(**BURN_DEFAULTS)
        commands: list[Command] = burn_subtitles(self._project, template)
        if not commands:
            self.status_message.emit("焼き込む字幕がありません")
            return
        self.commands_requested.emit(commands, "字幕を焼き込み")

    def export_file(self) -> None:
        name, _ = QFileDialog.getSaveFileName(self, "字幕を書き出す", "字幕.srt", SUBTITLE_FILTER)
        if not name:
            return
        written = save_subtitles(self._project, Path(name))
        self.status_message.emit(f"書き出した: {written}")
