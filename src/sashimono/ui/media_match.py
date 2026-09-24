"""空のプロジェクトへ最初の動画を置くとき、プロジェクトを動画の形へ合わせるか決める

起動した時点のプロジェクトは 1920x1080 30fps 60fps の動画を落とすと、何も言われずに
半分のコマが捨てられた書き出しになる（Issue #27） 最初の 1 本なら換算するクリップが
無いので、フレームレートも変えてよい（:mod:`sashimono.core.commands.project_format`）

合わせるかどうかは人によって違う（決まった形で作る人は、動画ごとに変わると困る）ので、
設定（:attr:`Preferences.match_video`）で「尋ねる・常に合わせる・合わせない」を選べる
既定は尋ねる 知らない人が、黙って変わったことにも、黙って変わらなかったことにも
気付けないのを避ける
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtWidgets import QMessageBox, QWidget

from sashimono.core.commands import Command, VideoFormat, format_to_match, match_commands
from sashimono.core.model import MediaItem, Project, ProjectSettings

__all__ = [
    "MATCH_ALWAYS",
    "MATCH_ASK",
    "MATCH_CHOICES",
    "MATCH_MODES",
    "MATCH_NEVER",
    "ask_to_match",
    "commands_to_match",
]

MATCH_ASK = "ask"
MATCH_ALWAYS = "always"
MATCH_NEVER = "never"
MATCH_MODES = (MATCH_ASK, MATCH_ALWAYS, MATCH_NEVER)

#: 設定画面に並べる表示名
MATCH_CHOICES: tuple[tuple[str, str], ...] = (
    (MATCH_ASK, "尋ねる"),
    (MATCH_ALWAYS, "常に動画に合わせる"),
    (MATCH_NEVER, "合わせない（プロジェクトの設定のまま）"),
)


def _describe(width: int, height: int, rate: object) -> str:
    return f"{width}×{height}・{rate} fps"


def ask_to_match(parent: QWidget | None, current: ProjectSettings, wanted: VideoFormat) -> bool:
    """「動画に合わせますか」と尋ねる 合わせるなら真

    試験では差し替える（``tests/conftest.py``） 窓を出すと、答える人がいないまま止まる
    """
    answer = QMessageBox.question(
        parent,
        "プロジェクトを動画に合わせる",
        "置こうとしている動画と、プロジェクトの形が違います\n"
        f"動画 {_describe(wanted.width, wanted.height, wanted.rate)}\n"
        f"プロジェクト {_describe(current.width, current.height, current.frame_rate)}\n\n"
        "プロジェクトの解像度とフレームレートを動画に合わせますか\n"
        "（タイムラインが空の今だけ変えられます 表示 → 設定… で毎回どうするかを決められます）",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    return answer == QMessageBox.StandardButton.Yes


def commands_to_match(
    parent: QWidget | None, policy: str, project: Project, media: Iterable[MediaItem]
) -> list[Command]:
    """設定に従って、プロジェクトを合わせるコマンドを決める 合わせないなら空"""
    if policy == MATCH_NEVER:
        return []
    wanted = format_to_match(project, media)
    if wanted is None:
        return []
    if policy != MATCH_ALWAYS and not ask_to_match(parent, project.settings, wanted):
        return []
    return match_commands(project, wanted)
