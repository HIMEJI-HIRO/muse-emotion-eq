"""
eq_widgets.py
=============
直感的 EQ 操作用のカスタムウィジェット.

- EQPad3D    : 3D 空間に浮かぶパックを可視化. 立方体内に座標投影.
               マウスドラッグで視点回転 (GLViewWidget デフォルト).
- AxisSlider : 1本の水平スライダー. 絵文字+ラベル付き.
- AxisSliderGroup : X/Y/Z の3本セット.

- XYPad / ZSlider : (旧バージョン. 残してあるが現在は未使用)

各ウィジェットは theme.ThemeManager を受け取り、テーマ変更で再描画.
"""
import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

try:
    import pyqtgraph as pg
    import pyqtgraph.opengl as gl
    HAS_GL = True
except Exception:
    HAS_GL = False


class XYPad(QtWidgets.QWidget):
    """2D EQ コントローラ. (-1..+1) × (-1..+1) の正規化座標を出力."""

    value_changed = QtCore.pyqtSignal(float, float)  # x, y

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.theme.subscribe(lambda *_: self.update())
        self._x = 0.0
        self._y = 0.0
        self._ghost_x = 0.0   # Auto モードで EEG が示す目標位置
        self._ghost_y = 0.0
        self._show_ghost = False
        self._dragging = False
        self._interactive = True
        self.setMinimumSize(260, 220)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.OpenHandCursor)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

    # --- public API ---
    def value(self):
        return (self._x, self._y)

    def set_value(self, x, y, emit=True):
        x = max(-1.0, min(1.0, float(x)))
        y = max(-1.0, min(1.0, float(y)))
        if x == self._x and y == self._y:
            return
        self._x, self._y = x, y
        self.update()
        if emit:
            self.value_changed.emit(x, y)

    def set_ghost(self, x, y, show=True):
        """Auto モードで EEG が示唆する目標位置を表示."""
        self._ghost_x = max(-1.0, min(1.0, float(x)))
        self._ghost_y = max(-1.0, min(1.0, float(y)))
        self._show_ghost = show
        self.update()

    def set_interactive(self, ok):
        self._interactive = ok
        self.setCursor(QtCore.Qt.OpenHandCursor if ok
                       else QtCore.Qt.ForbiddenCursor)

    # --- 幾何 ---
    def _pad_rect(self):
        m = 18
        return self.rect().adjusted(m, m, -m, -m)

    def _to_px(self, x, y):
        r = self._pad_rect()
        cx = r.left() + (x + 1.0) * 0.5 * r.width()
        cy = r.top() + (1.0 - (y + 1.0) * 0.5) * r.height()
        return cx, cy

    def _from_px(self, px, py):
        r = self._pad_rect()
        x = (px - r.left()) / max(1, r.width()) * 2.0 - 1.0
        y = 1.0 - (py - r.top()) / max(1, r.height()) * 2.0
        return max(-1, min(1, x)), max(-1, min(1, y))

    # --- paint ---
    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self._pad_rect()

        # 外枠カード
        bg = QtGui.QLinearGradient(r.topLeft(), r.bottomRight())
        bg.setColorAt(0.0, QtGui.QColor("#1e1e1e"))
        bg.setColorAt(1.0, QtGui.QColor("#141414"))
        p.setBrush(bg)
        p.setPen(QtGui.QPen(QtGui.QColor("#2a2a2a"), 1))
        p.drawRoundedRect(r, 10, 10)

        # クロスヘア
        p.setPen(QtGui.QPen(QtGui.QColor("#2d2d2d"), 1, QtCore.Qt.DashLine))
        p.drawLine(r.center().x(), r.top() + 6,
                   r.center().x(), r.bottom() - 6)
        p.drawLine(r.left() + 6, r.center().y(),
                   r.right() - 6, r.center().y())

        # 4象限のソフトな色ゾーン (ユーザに直感を与える)
        quad_colors = [
            # (x_sign, y_sign, color)
            (-1, +1, QtGui.QColor(231, 76,  60,  14)),   # 暖・鋭 (warm vocal)
            (+1, +1, QtGui.QColor(52,  152, 219, 14)),   # 明・鋭 (crisp)
            (-1, -1, QtGui.QColor(155, 89,  182, 14)),   # 暖・沈 (chill)
            (+1, -1, QtGui.QColor(26,  188, 156, 14)),   # 明・沈 (airy)
        ]
        cx, cy = r.center().x(), r.center().y()
        for xs, ys, col in quad_colors:
            qr = QtCore.QRectF(
                cx if xs > 0 else r.left(),
                r.top() if ys > 0 else cy,
                r.width() / 2.0,
                r.height() / 2.0,
            )
            p.setBrush(col)
            p.setPen(QtCore.Qt.NoPen)
            p.drawRoundedRect(qr, 8, 8)

        # 4隅の絵文字 + ムード名
        quad_labels = [
            ("🔥", "温かい",   QtCore.Qt.AlignLeft  | QtCore.Qt.AlignTop),
            ("✨", "クリア",   QtCore.Qt.AlignRight | QtCore.Qt.AlignTop),
            ("🌙", "まろやか", QtCore.Qt.AlignLeft  | QtCore.Qt.AlignBottom),
            ("💎", "きらびやか", QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom),
        ]
        # 上段
        emoji_font = QtGui.QFont("Segoe UI Emoji", 14)
        lbl_font = QtGui.QFont("Segoe UI", 8, QtGui.QFont.Bold)
        pad = r.adjusted(8, 6, -8, -6)
        for emoji, name, align in quad_labels:
            p.setFont(emoji_font)
            p.setPen(QtGui.QColor(255, 255, 255, 70))
            p.drawText(pad, align, emoji)
            p.setFont(lbl_font)
            p.setPen(QtGui.QColor("#707070"))
            # 絵文字の下/上に名前
            if align & QtCore.Qt.AlignTop:
                name_r = pad.adjusted(0, 22, 0, 0)
            else:
                name_r = pad.adjusted(0, 0, 0, -22)
            align_h = (align & (QtCore.Qt.AlignLeft | QtCore.Qt.AlignRight))
            align_v = (QtCore.Qt.AlignTop if (align & QtCore.Qt.AlignTop)
                       else QtCore.Qt.AlignBottom)
            p.drawText(name_r, align_h | align_v, name)

        # 軸ラベル (枠外)
        p.setPen(QtGui.QColor("#8a8a8a"))
        axfont = QtGui.QFont("Segoe UI", 8, QtGui.QFont.Bold)
        p.setFont(axfont)
        full = self.rect()
        p.drawText(full.adjusted(0, 0, 0, -full.height() + 14),
                   QtCore.Qt.AlignHCenter, "↑ Mid+ (鋭)")
        p.drawText(full.adjusted(0, full.height() - 14, 0, 0),
                   QtCore.Qt.AlignHCenter, "Mid- (沈) ↓")
        # 左右は縦書き風に
        p.save()
        p.translate(8, full.center().y())
        p.rotate(-90)
        p.drawText(-40, 5, "Bass+ ← 暖")
        p.restore()
        p.save()
        p.translate(full.width() - 8, full.center().y())
        p.rotate(-90)
        p.drawText(-40, -2, "明 → Treble+")
        p.restore()

        # ゴースト (Auto モードの目標位置)
        if self._show_ghost:
            gx, gy = self._to_px(self._ghost_x, self._ghost_y)
            ag = self.theme.accent_rgba(alpha=50)
            p.setBrush(QtGui.QColor(*ag))
            p.setPen(QtGui.QPen(QtGui.QColor(*self.theme.accent_glow, 120),
                                1.5, QtCore.Qt.DashLine))
            rad = 11
            p.drawEllipse(QtCore.QPointF(gx, gy), rad, rad)
            # 現在位置へのトレイル
            cx, cy = self._to_px(self._x, self._y)
            p.setPen(QtGui.QPen(QtGui.QColor(*self.theme.accent_glow, 80),
                                1, QtCore.Qt.DotLine))
            p.drawLine(QtCore.QPointF(cx, cy), QtCore.QPointF(gx, gy))

        # パック (現在位置)
        cx, cy = self._to_px(self._x, self._y)
        # 外側グロー
        for i, a in enumerate([25, 45, 80]):
            rad = 22 - i * 5
            col = QtGui.QColor(*self.theme.accent_glow, a)
            p.setBrush(col)
            p.setPen(QtCore.Qt.NoPen)
            p.drawEllipse(QtCore.QPointF(cx, cy), rad, rad)
        # 本体
        p.setBrush(QtGui.QColor(self.theme.accent))
        p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.5))
        p.drawEllipse(QtCore.QPointF(cx, cy), 9, 9)
        # 中心ハイライト
        p.setBrush(QtGui.QColor(255, 255, 255, 180))
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(QtCore.QPointF(cx - 2, cy - 2), 2.2, 2.2)

    # --- interaction ---
    def mousePressEvent(self, ev):
        if not self._interactive:
            return
        if ev.button() == QtCore.Qt.LeftButton:
            self._dragging = True
            self.setCursor(QtCore.Qt.ClosedHandCursor)
            x, y = self._from_px(ev.x(), ev.y())
            self.set_value(x, y)

    def mouseMoveEvent(self, ev):
        if self._dragging:
            x, y = self._from_px(ev.x(), ev.y())
            self.set_value(x, y)

    def mouseReleaseEvent(self, ev):
        if self._dragging:
            self._dragging = False
            self.setCursor(QtCore.Qt.OpenHandCursor)

    def mouseDoubleClickEvent(self, _):
        if self._interactive:
            self.set_value(0.0, 0.0)

    def keyPressEvent(self, ev):
        if not self._interactive:
            return
        step = 0.02
        if ev.key() == QtCore.Qt.Key_Left:
            self.set_value(self._x - step, self._y)
        elif ev.key() == QtCore.Qt.Key_Right:
            self.set_value(self._x + step, self._y)
        elif ev.key() == QtCore.Qt.Key_Up:
            self.set_value(self._x, self._y + step)
        elif ev.key() == QtCore.Qt.Key_Down:
            self.set_value(self._x, self._y - step)
        else:
            super().keyPressEvent(ev)


class ZSlider(QtWidgets.QWidget):
    """縦スライダー. 0..1 の正規化値を返す. Clarity/Intensity 用."""

    value_changed = QtCore.pyqtSignal(float)

    def __init__(self, theme, label="Clarity", parent=None):
        super().__init__(parent)
        self.theme = theme
        self.theme.subscribe(lambda *_: self.update())
        self._label = label
        self._v = 0.5
        self._dragging = False
        self._interactive = True
        self.setMinimumSize(60, 220)
        self.setCursor(QtCore.Qt.PointingHandCursor)

    def value(self):
        return self._v

    def set_value(self, v, emit=True):
        v = max(0.0, min(1.0, float(v)))
        if v == self._v:
            return
        self._v = v
        self.update()
        if emit:
            self.value_changed.emit(v)

    def set_interactive(self, ok):
        self._interactive = ok
        self.setCursor(QtCore.Qt.PointingHandCursor if ok
                       else QtCore.Qt.ForbiddenCursor)

    def _track_rect(self):
        w = 14
        cx = self.width() // 2
        top = 28
        bot = self.height() - 26
        return QtCore.QRect(cx - w // 2, top, w, max(20, bot - top))

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        # ラベル
        p.setPen(QtGui.QColor("#8a8a8a"))
        p.setFont(QtGui.QFont("Segoe UI", 8, QtGui.QFont.Bold))
        p.drawText(self.rect().adjusted(0, 6, 0, 0),
                   QtCore.Qt.AlignHCenter, self._label)

        # トラック
        tr = self._track_rect()
        p.setBrush(QtGui.QColor("#1a1a1a"))
        p.setPen(QtGui.QPen(QtGui.QColor("#2a2a2a"), 1))
        p.drawRoundedRect(tr, 7, 7)

        # フィル
        fill_h = int(tr.height() * self._v)
        fill = QtCore.QRect(tr.left(), tr.bottom() - fill_h,
                            tr.width(), fill_h)
        grad = QtGui.QLinearGradient(fill.topLeft(), fill.bottomLeft())
        grad.setColorAt(0.0, QtGui.QColor(*self.theme.accent_glow, 220))
        grad.setColorAt(1.0, QtGui.QColor(*self.theme.accent_glow, 120))
        p.setBrush(grad)
        p.setPen(QtCore.Qt.NoPen)
        p.drawRoundedRect(fill, 6, 6)

        # ハンドル
        cy = tr.bottom() - fill_h
        handle = QtCore.QRect(tr.left() - 6, cy - 6, tr.width() + 12, 12)
        for a in [30, 50, 90]:
            col = QtGui.QColor(*self.theme.accent_glow, a)
            p.setBrush(col)
            p.setPen(QtCore.Qt.NoPen)
            p.drawRoundedRect(handle.adjusted(-3, -3, 3, 3), 8, 8)
        p.setBrush(QtGui.QColor(self.theme.accent))
        p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.2))
        p.drawRoundedRect(handle, 6, 6)

        # 値表示
        p.setPen(QtGui.QColor(self.theme.accent))
        p.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Bold))
        p.drawText(self.rect().adjusted(0, 0, 0, -6),
                   QtCore.Qt.AlignHCenter | QtCore.Qt.AlignBottom,
                   f"{int(round(self._v * 100))}%")

    def _v_from_y(self, y):
        tr = self._track_rect()
        v = 1.0 - (y - tr.top()) / max(1, tr.height())
        return max(0.0, min(1.0, v))

    def mousePressEvent(self, ev):
        if not self._interactive:
            return
        if ev.button() == QtCore.Qt.LeftButton:
            self._dragging = True
            self.set_value(self._v_from_y(ev.y()))

    def mouseMoveEvent(self, ev):
        if self._dragging:
            self.set_value(self._v_from_y(ev.y()))

    def mouseReleaseEvent(self, ev):
        self._dragging = False

    def wheelEvent(self, ev):
        if not self._interactive:
            return
        step = 0.02
        delta = ev.angleDelta().y() / 120.0
        self.set_value(self._v + delta * step)


# ======================================================================
# 3D EQ Pad
# ======================================================================
class EQPad3D(QtWidgets.QWidget):
    """3D 可視化パック. X,Y ∈ [-1,1], Z ∈ [0,1].

    パックの位置を 3D 立方体内に表示. マウスドラッグで視点回転.
    値の変更は外部 (AxisSliderGroup / presets / EEG) から set_value() で行う.
    """

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.theme.subscribe(lambda *_: self._refresh_colors())
        self._x, self._y, self._z = 0.0, 0.0, 0.0
        self._ghost_xyz = None  # 将来の Auto モード目標位置

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        if not HAS_GL:
            fallback = QtWidgets.QLabel(
                "⚠ 3D 表示には PyOpenGL が必要です:\n"
                "  pip install PyOpenGL PyOpenGL-accelerate")
            fallback.setAlignment(QtCore.Qt.AlignCenter)
            fallback.setStyleSheet("color: #e67e22; font-size: 11px;")
            v.addWidget(fallback)
            self.view = None
            return

        self.view = gl.GLViewWidget()
        self.view.setBackgroundColor(QtGui.QColor("#141416"))
        # カメラ: 斜め上から (距離, 仰角, 方位角)
        self.view.setCameraPosition(distance=4.2, elevation=22, azimuth=-55)
        self.view.setMinimumSize(280, 240)
        v.addWidget(self.view)

        # --- 3D 要素構築 ---
        self._build_scene()
        # テキストラベル (2Dオーバーレイ)
        self._labels = [
            # (x, y, z, text)
            (-1.12,  0.00, 0.00, "🔥  暖"),
            ( 1.12,  0.00, 0.00, "✨  明"),
            ( 0.00, -1.12, 0.00, "🌙  沈"),
            ( 0.00,  1.12, 0.00, "⚡  鋭"),
            ( 0.00,  0.00, 1.12, "💎  強"),
        ]

        # 初回描画
        self._update_puck()

    # --- Scene ---
    def _build_scene(self):
        # 床グリッド (z = 0 平面)
        grid = gl.GLGridItem()
        grid.setSize(x=2.0, y=2.0)
        grid.setSpacing(x=0.2, y=0.2)
        grid.setColor(QtGui.QColor(60, 60, 65, 120))
        grid.translate(0, 0, 0)
        self.view.addItem(grid)

        # 背面グリッド (y = +1 平面)
        grid_yz = gl.GLGridItem()
        grid_yz.setSize(x=2.0, y=1.0)
        grid_yz.setSpacing(x=0.2, y=0.2)
        grid_yz.setColor(QtGui.QColor(50, 50, 55, 80))
        grid_yz.rotate(90, 1, 0, 0)
        grid_yz.translate(0, 1.0, 0.5)
        self.view.addItem(grid_yz)

        # 左側グリッド (x = -1 平面)
        grid_xz = gl.GLGridItem()
        grid_xz.setSize(x=2.0, y=1.0)
        grid_xz.setSpacing(x=0.2, y=0.2)
        grid_xz.setColor(QtGui.QColor(50, 50, 55, 80))
        grid_xz.rotate(90, 0, 1, 0)
        grid_xz.translate(-1.0, 0, 0.5)
        self.view.addItem(grid_xz)

        # 立方体ワイヤフレーム
        cube_pts = np.array([
            # bottom
            [-1, -1, 0], [ 1, -1, 0],
            [ 1, -1, 0], [ 1,  1, 0],
            [ 1,  1, 0], [-1,  1, 0],
            [-1,  1, 0], [-1, -1, 0],
            # top
            [-1, -1, 1], [ 1, -1, 1],
            [ 1, -1, 1], [ 1,  1, 1],
            [ 1,  1, 1], [-1,  1, 1],
            [-1,  1, 1], [-1, -1, 1],
            # verticals
            [-1, -1, 0], [-1, -1, 1],
            [ 1, -1, 0], [ 1, -1, 1],
            [ 1,  1, 0], [ 1,  1, 1],
            [-1,  1, 0], [-1,  1, 1],
        ], dtype=np.float32)
        self._cube = gl.GLLinePlotItem(
            pos=cube_pts, color=(0.35, 0.35, 0.38, 0.85),
            width=1.2, mode="lines", antialias=True)
        self.view.addItem(self._cube)

        # 軸 (原点から正方向へ伸びる太めのライン)
        axes_pts = np.array([
            [-1, 0, 0], [ 1, 0, 0],
            [ 0,-1, 0], [ 0, 1, 0],
            [ 0, 0, 0], [ 0, 0, 1],
        ], dtype=np.float32)
        self._axes = gl.GLLinePlotItem(
            pos=axes_pts, color=(0.55, 0.55, 0.6, 0.9),
            width=1.5, mode="lines", antialias=True)
        self.view.addItem(self._axes)

        # パック軌跡 (現在は単一点, 将来 Auto モードで履歴)
        # パック (グローは複数 scatter 重ね)
        c_glow = self._accent_gl(0.25)
        c_core = self._accent_gl(1.0)
        self._puck_outer = gl.GLScatterPlotItem(
            pos=np.array([[0, 0, 0.5]]), size=42.0,
            color=c_glow, pxMode=True)
        self._puck_outer.setGLOptions("additive")
        self.view.addItem(self._puck_outer)
        self._puck_mid = gl.GLScatterPlotItem(
            pos=np.array([[0, 0, 0.5]]), size=22.0,
            color=self._accent_gl(0.6), pxMode=True)
        self._puck_mid.setGLOptions("additive")
        self.view.addItem(self._puck_mid)
        self._puck = gl.GLScatterPlotItem(
            pos=np.array([[0, 0, 0.5]]), size=12.0,
            color=c_core, pxMode=True)
        self.view.addItem(self._puck)

        # 投影線 (パックから 3 面への点線)
        self._proj_xy = gl.GLLinePlotItem(
            color=self._accent_gl(0.45), width=1.0, antialias=True)
        self._proj_xz = gl.GLLinePlotItem(
            color=self._accent_gl(0.30), width=1.0, antialias=True)
        self._proj_yz = gl.GLLinePlotItem(
            color=self._accent_gl(0.30), width=1.0, antialias=True)
        for it in (self._proj_xy, self._proj_xz, self._proj_yz):
            self.view.addItem(it)

    def _accent_gl(self, alpha=1.0):
        r, g, b = self.theme.accent_glow
        return (r / 255.0, g / 255.0, b / 255.0, alpha)

    def _refresh_colors(self):
        if self.view is None:
            return
        self._puck.setData(color=self._accent_gl(1.0))
        self._puck_mid.setData(color=self._accent_gl(0.6))
        self._puck_outer.setData(color=self._accent_gl(0.25))
        self._proj_xy.setData(color=self._accent_gl(0.45))
        self._proj_xz.setData(color=self._accent_gl(0.30))
        self._proj_yz.setData(color=self._accent_gl(0.30))

    # --- Public API ---
    def value(self):
        return (self._x, self._y, self._z)

    def set_value(self, x, y, z):
        self._x = max(-1.0, min(1.0, float(x)))
        self._y = max(-1.0, min(1.0, float(y)))
        self._z = max(0.0, min(1.0, float(z)))
        self._update_puck()

    def set_ghost(self, x, y, z, show=True):
        """Auto モードで EEG が示唆する目標位置."""
        if not show:
            self._ghost_xyz = None
        else:
            self._ghost_xyz = (float(x), float(y), float(z))
        self._update_puck()

    def _update_puck(self):
        if self.view is None:
            return
        pos = np.array([[self._x, self._y, self._z]], dtype=np.float32)
        self._puck.setData(pos=pos)
        self._puck_mid.setData(pos=pos)
        self._puck_outer.setData(pos=pos)
        # 投影線
        self._proj_xy.setData(pos=np.array(
            [[self._x, self._y, self._z], [self._x, self._y, 0.0]],
            dtype=np.float32))
        self._proj_xz.setData(pos=np.array(
            [[self._x, self._y, self._z], [self._x, 1.0, self._z]],
            dtype=np.float32))
        self._proj_yz.setData(pos=np.array(
            [[self._x, self._y, self._z], [-1.0, self._y, self._z]],
            dtype=np.float32))
        # ラベルは paintGL で描画するよう viewport 更新
        self.view.update()

    # --- ラベルのオーバーレイ (GLViewWidget.paintGL フック) ---
    # 注: pyqtgraph の renderText は deprecated / Qt5 で不安定なので
    # paintEvent を QPainter で上に描く方式にする.
    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self.view is None:
            return
        # GLViewWidget 上に QPainter でラベルを描く
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(QtGui.QColor(255, 255, 255, 150))
        p.setFont(QtGui.QFont("Segoe UI Emoji", 9))
        # view の幾何変換を使ってラベル位置を推定するのは重い.
        # 代わりにコーナーにテキストでガイドを描く.
        w, h = self.width(), self.height()
        corner_font = QtGui.QFont("Segoe UI", 8, QtGui.QFont.Bold)
        p.setFont(corner_font)
        p.setPen(QtGui.QColor("#707077"))
        p.drawText(10, 16, "🔥 暖 ← X → 明 ✨")
        p.drawText(10, h - 10, "🌙 沈 ← Y → 鋭 ⚡")
        p.drawText(w - 100, 16, "💎 強 Z")
        p.end()


# ======================================================================
# Axis Slider (単軸水平スライダー)
# ======================================================================
class AxisSlider(QtWidgets.QWidget):
    """水平スライダー. (-1, +1) または (0, +1) 範囲.

    両端に絵文字ラベル. 中央に現在値の数値.
    """
    value_changed = QtCore.pyqtSignal(float)

    def __init__(self, theme, left_text, right_text, bipolar=True, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.theme.subscribe(lambda *_: self.update())
        self._left = left_text
        self._right = right_text
        self._bipolar = bipolar
        self._v = 0.0
        self._dragging = False
        self._interactive = True
        self.setMinimumHeight(44)
        self.setCursor(QtCore.Qt.PointingHandCursor)

    # --- range helpers ---
    @property
    def _vmin(self):
        return -1.0 if self._bipolar else 0.0

    @property
    def _vmax(self):
        return 1.0

    def value(self):
        return self._v

    def set_value(self, v, emit=True):
        v = max(self._vmin, min(self._vmax, float(v)))
        if v == self._v:
            return
        self._v = v
        self.update()
        if emit:
            self.value_changed.emit(v)

    def set_interactive(self, ok):
        self._interactive = ok
        self.setCursor(QtCore.Qt.PointingHandCursor if ok
                       else QtCore.Qt.ForbiddenCursor)

    def _track_rect(self):
        lpad, rpad = 42, 42
        h = 8
        cy = self.height() // 2 + 4
        return QtCore.QRect(lpad, cy - h // 2,
                            max(20, self.width() - lpad - rpad), h)

    def _px_to_v(self, px):
        tr = self._track_rect()
        t = (px - tr.left()) / max(1, tr.width())
        t = max(0.0, min(1.0, t))
        return self._vmin + t * (self._vmax - self._vmin)

    def _v_to_px(self, v):
        tr = self._track_rect()
        t = (v - self._vmin) / (self._vmax - self._vmin)
        return tr.left() + t * tr.width()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        tr = self._track_rect()

        # 左端ラベル
        p.setPen(QtGui.QColor("#8a8a8a"))
        p.setFont(QtGui.QFont("Segoe UI Emoji", 10))
        p.drawText(QtCore.QRect(0, 0, tr.left() - 4, self.height()),
                   QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight, self._left)
        # 右端
        p.drawText(QtCore.QRect(tr.right() + 4, 0,
                                self.width() - tr.right() - 4, self.height()),
                   QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, self._right)

        # トラック背景
        p.setBrush(QtGui.QColor("#1a1a1c"))
        p.setPen(QtGui.QPen(QtGui.QColor("#28282b"), 1))
        p.drawRoundedRect(tr, 4, 4)

        # bipolar 中央マーカー
        if self._bipolar:
            cxm = self._v_to_px(0.0)
            p.setPen(QtGui.QPen(QtGui.QColor("#333338"), 1, QtCore.Qt.DashLine))
            p.drawLine(int(cxm), tr.top() - 2, int(cxm), tr.bottom() + 2)

        # フィル (中央 → 現在値, または 0 → 現在値)
        if self._bipolar:
            x0 = self._v_to_px(0.0)
        else:
            x0 = tr.left()
        x1 = self._v_to_px(self._v)
        if x1 < x0:
            x0, x1 = x1, x0
        fill = QtCore.QRect(int(x0), tr.top(), int(x1 - x0), tr.height())
        grad = QtGui.QLinearGradient(fill.topLeft(), fill.topRight())
        r, g, b = self.theme.accent_glow
        grad.setColorAt(0.0, QtGui.QColor(r, g, b, 160))
        grad.setColorAt(1.0, QtGui.QColor(r, g, b, 220))
        p.setBrush(grad)
        p.setPen(QtCore.Qt.NoPen)
        p.drawRoundedRect(fill, 3, 3)

        # ハンドル
        cx = int(self._v_to_px(self._v))
        cy = tr.center().y()
        # グロー
        for a, rad in [(40, 14), (80, 10), (160, 7)]:
            p.setBrush(QtGui.QColor(*self.theme.accent_glow, a))
            p.setPen(QtCore.Qt.NoPen)
            p.drawEllipse(QtCore.QPointF(cx, cy), rad, rad)
        p.setBrush(QtGui.QColor(self.theme.accent))
        p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.2))
        p.drawEllipse(QtCore.QPointF(cx, cy), 5.5, 5.5)

        # 値
        p.setPen(QtGui.QColor(self.theme.accent))
        p.setFont(QtGui.QFont("Segoe UI", 8, QtGui.QFont.Bold))
        p.drawText(QtCore.QRect(cx - 30, cy - 28, 60, 14),
                   QtCore.Qt.AlignHCenter | QtCore.Qt.AlignBottom,
                   f"{self._v:+.2f}" if self._bipolar else f"{self._v:.2f}")

    def mousePressEvent(self, ev):
        if not self._interactive or ev.button() != QtCore.Qt.LeftButton:
            return
        self._dragging = True
        self.set_value(self._px_to_v(ev.x()))

    def mouseMoveEvent(self, ev):
        if self._dragging:
            self.set_value(self._px_to_v(ev.x()))

    def mouseReleaseEvent(self, _):
        self._dragging = False

    def mouseDoubleClickEvent(self, _):
        if self._interactive:
            self.set_value(0.0 if self._bipolar else 0.5)

    def wheelEvent(self, ev):
        if not self._interactive:
            return
        step = 0.02
        delta = ev.angleDelta().y() / 120.0
        self.set_value(self._v + delta * step)


class AxisSliderGroup(QtWidgets.QWidget):
    """X/Y/Z 3本の水平スライダー.

    X ∈ [-1,1]  : 🔥 暖 ↔ ✨ 明
    Y ∈ [-1,1]  : 🌙 沈 ↔ ⚡ 鋭
    Z ∈ [0,1]   :         弱 ↔ 💎 強
    """
    value_changed = QtCore.pyqtSignal(float, float, float)

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        self.sx = AxisSlider(theme, "🔥 暖", "明 ✨", bipolar=True)
        self.sy = AxisSlider(theme, "🌙 沈", "鋭 ⚡", bipolar=True)
        self.sz = AxisSlider(theme, "   弱", "強 💎", bipolar=False)
        self.sz.set_value(0.0, emit=False)
        for s in (self.sx, self.sy, self.sz):
            v.addWidget(s)
            s.value_changed.connect(self._emit)

    def _emit(self, _=None):
        self.value_changed.emit(self.sx.value(), self.sy.value(), self.sz.value())

    def value(self):
        return (self.sx.value(), self.sy.value(), self.sz.value())

    def set_value(self, x, y, z):
        self.sx.set_value(x, emit=False)
        self.sy.set_value(y, emit=False)
        self.sz.set_value(z, emit=False)
        self._emit()

    def set_interactive(self, ok):
        for s in (self.sx, self.sy, self.sz):
            s.set_interactive(ok)



# ======================================================================
# InstrumentFader / InstrumentFaderBank
# ======================================================================
# 楽器別 6 バンド EQ をミキサー風の縦フェーダで操作するウィジェット.
# 各フェーダ:
#   - 絵文字 + 楽器名 + 周波数ラベル
#   - 縦トラック + ハンドル (ドラッグ/クリック/ホイール)
#   - 現在 dB 値の表示
#   - ダブルクリックで 0 に戻る
#   - set_interactive(False) で表示のみ (Auto モード用)
# ======================================================================


class InstrumentFader(QtWidgets.QWidget):
    """単一バンド用の縦フェーダ. 値は dB, 範囲は ±gain_max."""

    value_changed = QtCore.pyqtSignal(str, float)   # key, db

    def __init__(self, theme, key, emoji, name, freq_hz,
                 gain_max=4.0, blurb="", parent=None):
        super().__init__(parent)
        self.theme = theme
        self.key = key
        self.emoji = emoji
        self.name = name
        self.freq_hz = freq_hz
        self.gain_max = gain_max
        self.blurb = blurb
        self._db = 0.0
        self._interactive = True
        self._dragging = False
        self._ghost_db = None
        self._ghost_show = False

        self.setMinimumSize(78, 220)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.setToolTip(f"{name} · {self._fmt_freq()} · {blurb}")
        if hasattr(theme, "subscribe"):
            theme.subscribe(lambda *_: self.update())

        # 背景波形アニメ用
        self._wave_phase = 0.0
        self._wave_timer = QtCore.QTimer(self)
        self._wave_timer.timeout.connect(self._advance_wave)
        self._wave_timer.start(60)   # ~16fps. 軽い動き

    def _advance_wave(self):
        # 走らせる速度. dB の絶対値が大きいほど早く流れる演出.
        import math as _m
        speed = 0.08 + abs(self._db) / max(0.01, self.gain_max) * 0.20
        self._wave_phase = (self._wave_phase + speed) % (2 * _m.pi)
        self.update()

    def value(self):
        return self._db

    def set_value(self, db, emit=True):
        db = max(-self.gain_max, min(self.gain_max, float(db)))
        if abs(db - self._db) < 1e-4:
            return
        self._db = db
        self.update()
        if emit:
            self.value_changed.emit(self.key, self._db)

    def set_interactive(self, ok):
        self._interactive = bool(ok)
        self.setCursor(QtCore.Qt.PointingHandCursor if ok
                       else QtCore.Qt.ArrowCursor)
        self.update()

    def set_ghost(self, db, show=True):
        self._ghost_db = max(-self.gain_max, min(self.gain_max, float(db)))
        self._ghost_show = bool(show)
        self.update()

    def _fmt_freq(self):
        if self.freq_hz >= 1000:
            return f"{self.freq_hz/1000:g} kHz"
        return f"{self.freq_hz:g} Hz"

    def _track_rect(self):
        w = self.width()
        h = self.height()
        top_reserve = 56
        bottom_reserve = 28
        track_w = 10
        track_x = (w - track_w) // 2
        track_y = top_reserve
        track_h = h - top_reserve - bottom_reserve
        return QtCore.QRectF(track_x, track_y, track_w, track_h)

    def _db_to_y(self, db):
        r = self._track_rect()
        t = (db + self.gain_max) / (2 * self.gain_max)
        return r.bottom() - t * r.height()

    def _y_to_db(self, y):
        r = self._track_rect()
        t = (r.bottom() - y) / r.height()
        t = max(0.0, min(1.0, t))
        return (t * 2.0 - 1.0) * self.gain_max

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        accent = self.theme.accent if self.theme else "#1abc9c"
        glow = (self.theme.accent_glow if self.theme
                else (26, 188, 156))

        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor("#1a1a1c"))
        p.drawRoundedRect(0, 0, w, h, 8, 8)
        p.setPen(QtGui.QPen(QtGui.QColor("#2a2a2c"), 1))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawRoundedRect(0, 0, w - 1, h - 1, 8, 8)

        emo_font = QtGui.QFont("Segoe UI Emoji", 18)
        p.setFont(emo_font)
        p.setPen(QtGui.QColor("#e8e8e8"))
        p.drawText(QtCore.QRectF(0, 4, w, 26), QtCore.Qt.AlignCenter,
                   self.emoji)
        name_font = QtGui.QFont("Segoe UI", 9, QtGui.QFont.Bold)
        p.setFont(name_font)
        p.setPen(QtGui.QColor("#d0d0d0"))
        p.drawText(QtCore.QRectF(0, 30, w, 14), QtCore.Qt.AlignCenter, self.name)
        freq_font = QtGui.QFont("Segoe UI", 7)
        p.setFont(freq_font)
        p.setPen(QtGui.QColor("#7a7a7a"))
        p.drawText(QtCore.QRectF(0, 43, w, 12), QtCore.Qt.AlignCenter,
                   self._fmt_freq())

        r = self._track_rect()
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor("#0e0e10"))
        p.drawRoundedRect(r, 4, 4)

        # --- 背景波形 (アクセント色の細い 3本サイン重ね合わせ) ---
        import math as _m
        p.save()
        # clip を track に
        clip_path = QtGui.QPainterPath()
        clip_path.addRoundedRect(r, 4, 4)
        p.setClipPath(clip_path)
        amp = r.width() * 0.35   # 振幅は track 幅の 35%
        cx = r.center().x()
        for i, (freq, opacity, phase_mul) in enumerate([
                (0.06, 28, 1.0), (0.10, 18, -1.3), (0.14, 12, 1.7)]):
            pen = QtGui.QPen(QtGui.QColor(*glow, opacity), 1.0)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            path = QtGui.QPainterPath()
            y0 = r.top()
            x0 = cx + amp * _m.sin(
                self._wave_phase * phase_mul + y0 * freq)
            path.moveTo(x0, y0)
            steps = int(r.height() / 4)
            for s in range(1, steps + 1):
                y = r.top() + s * 4
                x = cx + amp * _m.sin(
                    self._wave_phase * phase_mul + y * freq + i * 1.2)
                path.lineTo(x, y)
            p.drawPath(path)
        p.restore()

        zero_y = self._db_to_y(0.0)
        p.setPen(QtGui.QPen(QtGui.QColor("#3a3a3c"), 1, QtCore.Qt.DashLine))
        p.drawLine(QtCore.QPointF(r.left() - 3, zero_y),
                   QtCore.QPointF(r.right() + 3, zero_y))

        cur_y = self._db_to_y(self._db)
        fill_rect = QtCore.QRectF(r.left(), min(zero_y, cur_y),
                                  r.width(), abs(cur_y - zero_y))
        grad = QtGui.QLinearGradient(0, fill_rect.top(), 0, fill_rect.bottom())
        c1 = QtGui.QColor(*glow, 220)
        c2 = QtGui.QColor(*glow, 90)
        if cur_y < zero_y:
            grad.setColorAt(0.0, c1)
            grad.setColorAt(1.0, c2)
        else:
            grad.setColorAt(0.0, c2)
            grad.setColorAt(1.0, c1)
        p.setBrush(grad)
        p.setPen(QtCore.Qt.NoPen)
        p.drawRoundedRect(fill_rect, 3, 3)

        if self._ghost_show and self._ghost_db is not None:
            gy = self._db_to_y(self._ghost_db)
            p.setPen(QtGui.QPen(QtGui.QColor(*glow, 160), 1,
                                QtCore.Qt.DashLine))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawLine(QtCore.QPointF(r.left() - 6, gy),
                       QtCore.QPointF(r.right() + 6, gy))

        hx = r.center().x()
        hw = 26
        hh = 14
        handle_rect = QtCore.QRectF(hx - hw/2, cur_y - hh/2, hw, hh)
        # ネオングロー (3 段)
        p.setPen(QtCore.Qt.NoPen)
        for mult, alpha in [(2.4, 30), (1.6, 70), (1.0, 130)]:
            outer = handle_rect.adjusted(
                -3 * mult, -2 * mult, 3 * mult, 2 * mult)
            grad = QtGui.QRadialGradient(outer.center(), outer.width() / 2)
            grad.setColorAt(0.0, QtGui.QColor(*glow, alpha))
            grad.setColorAt(1.0, QtGui.QColor(*glow, 0))
            p.setBrush(grad)
            p.drawRoundedRect(outer, 8, 8)
        # 本体
        body_grad = QtGui.QLinearGradient(0, handle_rect.top(), 0,
                                           handle_rect.bottom())
        body_grad.setColorAt(0.0, QtGui.QColor(*glow, 255))
        body_grad.setColorAt(1.0, QtGui.QColor(
            max(0, glow[0] - 50), max(0, glow[1] - 50),
            max(0, glow[2] - 50), 255))
        p.setBrush(body_grad)
        p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.5))
        p.drawRoundedRect(handle_rect, 4, 4)

        val_font = QtGui.QFont("Segoe UI", 9, QtGui.QFont.Bold)
        p.setFont(val_font)
        p.setPen(QtGui.QColor(accent if abs(self._db) > 0.05 else "#8a8a8a"))
        p.drawText(QtCore.QRectF(0, h - 22, w, 16), QtCore.Qt.AlignCenter,
                   f"{self._db:+.1f} dB" if abs(self._db) > 0.05 else "0 dB")
        p.end()

    def _set_from_y(self, y):
        self.set_value(self._y_to_db(y), emit=True)

    def mousePressEvent(self, ev):
        if not self._interactive:
            return
        if ev.button() == QtCore.Qt.LeftButton:
            self._dragging = True
            self._set_from_y(ev.pos().y())

    def mouseMoveEvent(self, ev):
        if self._dragging and self._interactive:
            self._set_from_y(ev.pos().y())

    def mouseReleaseEvent(self, ev):
        self._dragging = False

    def mouseDoubleClickEvent(self, ev):
        if self._interactive:
            self.set_value(0.0, emit=True)

    def wheelEvent(self, ev):
        if not self._interactive:
            return
        step = 0.2
        delta = ev.angleDelta().y()
        if delta > 0:
            self.set_value(self._db + step, emit=True)
        elif delta < 0:
            self.set_value(self._db - step, emit=True)


class InstrumentFaderBank(QtWidgets.QWidget):
    """6 バンドぶんの InstrumentFader を横並びにしたバンク."""

    band_changed = QtCore.pyqtSignal(str, float)

    def __init__(self, theme, bands, gain_max=4.0, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.gain_max = gain_max
        self.faders = {}

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for key, emoji, name, freq, blurb in bands:
            f = InstrumentFader(theme, key, emoji, name, freq,
                                gain_max=gain_max, blurb=blurb)
            f.value_changed.connect(self._forward)
            row.addWidget(f, 1)
            self.faders[key] = f

    def _forward(self, key, db):
        self.band_changed.emit(key, db)

    def values(self):
        return {k: f.value() for k, f in self.faders.items()}

    def set_values(self, d, emit=False):
        for k, v in d.items():
            if k in self.faders:
                self.faders[k].set_value(v, emit=emit)

    def set_ghosts(self, d, show=True):
        for k, f in self.faders.items():
            if k in d:
                f.set_ghost(d[k], show=show)
            else:
                f.set_ghost(0.0, show=False)

    def clear_ghosts(self):
        for f in self.faders.values():
            f.set_ghost(0.0, show=False)

    def set_interactive(self, ok):
        for f in self.faders.values():
            f.set_interactive(ok)

    def reset(self, emit=True):
        for f in self.faders.values():
            f.set_value(0.0, emit=emit)
