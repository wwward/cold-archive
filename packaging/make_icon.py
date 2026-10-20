"""Render the app icon with Qt (no image libraries needed) and build the .icns.

Usage: .venv/bin/python packaging/make_icon.py
Writes packaging/ColdArchive.png (1024 px) and packaging/ColdArchive.icns.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size
    # macOS-style rounded square, cold blue gradient
    inset = s * 0.06
    rect = QRectF(inset, inset, s - 2 * inset, s - 2 * inset)
    grad = QLinearGradient(QPointF(0, 0), QPointF(0, s))
    grad.setColorAt(0.0, QColor("#4a8fe7"))
    grad.setColorAt(1.0, QColor("#173f7a"))
    path = QPainterPath()
    path.addRoundedRect(rect, s * 0.22, s * 0.22)
    p.fillPath(path, grad)
    # snowflake: six spokes with small ticks, white
    p.setPen(QPen(QColor(255, 255, 255, 235), s * 0.045, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    cx, cy, r = s / 2, s * 0.47, s * 0.26
    p.translate(cx, cy)
    for i in range(6):
        p.drawLine(QPointF(0, 0), QPointF(0, -r))
        p.drawLine(QPointF(0, -r * 0.62), QPointF(-r * 0.2, -r * 0.82))
        p.drawLine(QPointF(0, -r * 0.62), QPointF(r * 0.2, -r * 0.82))
        p.rotate(60)
    p.resetTransform()
    # green check badge, bottom right
    br = s * 0.17
    bx, by = s * 0.68, s * 0.68
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QRectF(bx - br - s * 0.012, by - br - s * 0.012, 2 * br + s * 0.024, 2 * br + s * 0.024))
    p.setBrush(QColor("#1e8e3e"))
    p.drawEllipse(QRectF(bx - br, by - br, 2 * br, 2 * br))
    p.setPen(QPen(QColor("#ffffff"), s * 0.04, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPolyline([QPointF(bx - br * 0.45, by + br * 0.02), QPointF(bx - br * 0.1, by + br * 0.38), QPointF(bx + br * 0.5, by - br * 0.35)])
    p.end()
    return img


def main() -> int:
    QGuiApplication([])
    png = os.path.join(HERE, "ColdArchive.png")
    render(1024).save(png)
    tmp = tempfile.mkdtemp(prefix="coldarchive-icon-")
    iconset = os.path.join(tmp, "ColdArchive.iconset")
    os.makedirs(iconset)
    for base in (16, 32, 128, 256, 512):
        render(base).save(os.path.join(iconset, f"icon_{base}x{base}.png"))
        render(base * 2).save(os.path.join(iconset, f"icon_{base}x{base}@2x.png"))
    icns = os.path.join(HERE, "ColdArchive.icns")
    if sys.platform == "darwin" and shutil.which("iconutil"):
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
        print("wrote", icns)
    else:
        print("iconutil not available; wrote PNG only")
    shutil.rmtree(tmp, ignore_errors=True)
    print("wrote", png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
