"""テキストと図形を絵にする。

Qt の描画系（``QPainter``）を使う。日本語の禁則処理やフォントの字形選択、
縁取りの輪郭生成を自前で書くのは現実的ではなく、Qt はそれをすべて持っている。

戻り値は常に sRGB・ストレートアルファの ``(高さ, 幅, 4)`` uint8。素材から
デコードした絵とまったく同じ形なので、この先の合成は区別せずに済む。
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QTransform,
)

from novaedit.core.model import AnimatedValue, GeneratedSource, ParamValue
from novaedit.effects.sources import SourceDefinition, source_registry

__all__ = ["render_source"]


def render_source(
    source: GeneratedSource, width: int, height: int, *, frame: int = 0
) -> np.ndarray | None:
    """生成オブジェクトを描いて配列で返す。未知の種類なら ``None``。"""
    definition = source_registry.get(source.kind)
    if definition is None:
        return None

    values = _resolve(definition, source.params, frame)
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    try:
        if source.kind == "text":
            _draw_text(painter, values, width, height)
        elif source.kind == "shape":
            _draw_shape(painter, values, width, height)
    finally:
        painter.end()

    return _to_array(image)


def _resolve(
    definition: SourceDefinition, params: dict[str, ParamValue], frame: int
) -> dict[str, object]:
    """パラメータを、その時刻での素の値へ。

    数値は :class:`~novaedit.core.model.AnimatedValue` なので、フレームを与えて
    評価する。ここを飛ばすとキーフレームが効かない。
    """
    resolved: dict[str, object] = {}
    for spec in definition.parameters:
        value = spec.coerce(params.get(spec.name))
        resolved[spec.name] = value.at(frame) if isinstance(value, AnimatedValue) else value
    return resolved


def _draw_text(painter: QPainter, values: dict[str, object], width: int, height: int) -> None:
    text = str(values.get("text", ""))
    if not text:
        return

    font = QFont(str(values.get("font", "Yu Gothic UI")))
    font.setPixelSize(max(1, int(float(values.get("size", 64)))))  # type: ignore[arg-type]
    font.setBold(bool(values.get("bold", False)))
    font.setItalic(bool(values.get("italic", False)))
    letter_spacing = float(values.get("letter_spacing", 0.0))  # type: ignore[arg-type]
    if letter_spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)

    metrics = QFontMetricsF(font)
    lines = text.split("\n")
    line_height = metrics.height() + float(values.get("line_spacing", 0.0))  # type: ignore[arg-type]
    block_height = line_height * len(lines)

    align = str(values.get("align", "center"))
    centre_x = width / 2.0 + float(values.get("pos_x", 0.0))  # type: ignore[arg-type]
    centre_y = height / 2.0 - float(values.get("pos_y", 0.0))  # type: ignore[arg-type]
    top = centre_y - block_height / 2.0

    # 文字を輪郭（パス）として組み立てる。縁取りを外側だけに出すには、
    # 塗りとは別に輪郭を太らせる必要があり、それはパスでしかできない。
    path = QPainterPath()
    for index, line in enumerate(lines):
        line_width = metrics.horizontalAdvance(line)
        if align == "left":
            x = centre_x - _widest(metrics, lines) / 2.0
        elif align == "right":
            x = centre_x + _widest(metrics, lines) / 2.0 - line_width
        else:
            x = centre_x - line_width / 2.0
        baseline = top + line_height * index + metrics.ascent()
        path.addText(QPointF(x, baseline), font, line)

    border_width = float(values.get("border_width", 0.0))  # type: ignore[arg-type]
    if border_width > 0:
        stroker = QPainterPathStroker()
        # 太さは輪郭の中心から両側へ広がるので、指定の 2 倍で外側に指定幅が出る。
        stroker.setWidth(border_width * 2.0)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.fillPath(stroker.createStroke(path), _color(values.get("border_color")))

    painter.fillPath(path, _color(values.get("color")))


def _widest(metrics: QFontMetricsF, lines: list[str]) -> float:
    return max((metrics.horizontalAdvance(line) for line in lines), default=0.0)


def _draw_shape(painter: QPainter, values: dict[str, object], width: int, height: int) -> None:
    shape_width = max(1.0, float(values.get("width", 400)))  # type: ignore[arg-type]
    shape_height = max(1.0, float(values.get("height", 400)))  # type: ignore[arg-type]
    centre_x = width / 2.0 + float(values.get("pos_x", 0.0))  # type: ignore[arg-type]
    centre_y = height / 2.0 - float(values.get("pos_y", 0.0))  # type: ignore[arg-type]

    rect = QRectF(-shape_width / 2.0, -shape_height / 2.0, shape_width, shape_height)
    path = _shape_path(str(values.get("shape", "rect")), rect, values)

    transform = QTransform()
    transform.translate(centre_x, centre_y)
    transform.rotate(float(values.get("rotation", 0.0)))  # type: ignore[arg-type]
    path = transform.map(path)

    color = _color(values.get("color"))
    line_width = float(values.get("line_width", 0.0))  # type: ignore[arg-type]

    if bool(values.get("outline_only", False)):
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, max(line_width, 1.0)))
        painter.drawPath(path)
        return

    painter.fillPath(path, color)
    if line_width > 0:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, line_width))
        painter.drawPath(path)


def _shape_path(kind: str, rect: QRectF, values: dict[str, object]) -> QPainterPath:
    path = QPainterPath()
    if kind == "ellipse":
        path.addEllipse(rect)
    elif kind == "rounded":
        radius = float(values.get("corner_radius", 24))  # type: ignore[arg-type]
        path.addRoundedRect(rect, radius, radius)
    elif kind == "triangle":
        path.moveTo(rect.center().x(), rect.top())
        path.lineTo(rect.right(), rect.bottom())
        path.lineTo(rect.left(), rect.bottom())
        path.closeSubpath()
    elif kind == "star":
        path = _star_path(rect)
    else:
        path.addRect(rect)
    return path


def _star_path(rect: QRectF, points: int = 5) -> QPainterPath:
    """5 芒星。外周と内周の頂点を交互に結ぶ。"""
    path = QPainterPath()
    outer_x, outer_y = rect.width() / 2.0, rect.height() / 2.0
    inner_ratio = 0.382  # 正五芒星の内接比
    centre = rect.center()

    for index in range(points * 2):
        angle = np.pi * index / points - np.pi / 2.0
        ratio = 1.0 if index % 2 == 0 else inner_ratio
        x = centre.x() + np.cos(angle) * outer_x * ratio
        y = centre.y() + np.sin(angle) * outer_y * ratio
        if index == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


def _color(value: object) -> QColor:
    """``(R, G, B, A)`` の 0..1（sRGB）を :class:`QColor` へ。"""
    if not isinstance(value, tuple) or len(value) < 3:
        return QColor(255, 255, 255, 255)
    channels = [round(min(max(float(v), 0.0), 1.0) * 255) for v in value[:4]]
    while len(channels) < 4:
        channels.append(255)
    return QColor(*channels)


def _to_array(image: QImage) -> np.ndarray:
    """``QImage`` を ``(高さ, 幅, 4)`` の配列へ。

    ``QImage`` は行ごとに詰め物を入れることがあるので、``bytesPerLine`` を見て
    余りを落とす。幅だけで整形すると絵が斜めにずれる。
    """
    width, height = image.width(), image.height()
    buffer = bytes(image.constBits())
    stride = image.bytesPerLine()
    rows = np.frombuffer(buffer, dtype=np.uint8).reshape(height, stride)
    return np.ascontiguousarray(rows[:, : width * 4].reshape(height, width, 4))
