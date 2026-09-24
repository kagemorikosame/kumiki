"""試験が本物のクリップボードへ書かないための見張りが効いているか（#154）

見張りが外れると、試験を走らせるたびに本人がコピーしていた物が消え、同時に走る
ほかの作業と取り合った試験が読み戻しの空で落ちる
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QPushButton

from sashimono.ui import system_clipboard
from sashimono.ui.snapshot import copy_to_clipboard
from tests.fake_clipboard import FakeClipboard


def test_the_app_writes_to_the_fake_during_tests(fake_clipboard: FakeClipboard) -> None:
    # アプリの口が偽物を返さないと、静止画のコピー（#153）の試験が本人の画面を上書きする
    image = QImage(8, 6, QImage.Format.Format_RGBA8888)
    image.fill(QColor(255, 0, 0))
    copy_to_clipboard(image)
    assert system_clipboard.clipboard() is fake_clipboard
    assert fake_clipboard.image().size() == image.size()
    assert fake_clipboard.refused == []


def test_writing_to_the_real_clipboard_fails_the_test(
    qt_application: QApplication, fake_clipboard: FakeClipboard
) -> None:
    # 見張りが止めなければ、本物の中身が "書き換えた" に変わる
    with pytest.raises(pytest.fail.Exception):
        qt_application.clipboard().setText("書き換えた")
    assert fake_clipboard.refused == ["setText"]
    # 止めたことを確かめたので、後片付けで落とす記録は消しておく
    fake_clipboard.refused.clear()


def test_a_write_from_a_button_is_still_caught(
    qt_application: QApplication, fake_clipboard: FakeClipboard
) -> None:
    # ボタンの信号から呼ばれた所で投げた例外は Qt が握る 記録が残らないと試験が通ってしまう
    button = QPushButton()
    button.clicked.connect(lambda: qt_application.clipboard().setText("書き換えた"))
    button.click()
    assert fake_clipboard.refused == ["setText"]
    fake_clipboard.refused.clear()
