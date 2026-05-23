"""
Muse S Athena Real-Time Monitor.
Mind Monitor からOSCで受信し、PyQtGraphで可視化。

構成:
- Raw EEG 4ch (bandpass 1-40 Hz)
- Spectrogram (rolling STFT, jet colormap)
- Band Power (δ, θ, α, β, γ)
- Signal Quality (説明付き)
- Heart Rate & fNIRS (Muse S Athena 光学センサー)
- Emotion State — Russell's Circumplex Model (1980)
  Arousal = β/α, Valence = FAA (ln α_AF8 - ln α_AF7)
"""
import sys
import csv
import os
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
from scipy.signal import butter, sosfiltfilt, welch, find_peaks
from pythonosc import dispatcher, osc_server
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from audio_engine import AudioEngine, HAS_AUDIO
from theme import (ThemeManager, THEMES, BG_PALETTES, BG_DEEP, BG_PANEL,
                   BG_CARD, BORDER, TEXT_MAIN, TEXT_DIM, TEXT_FAINT)
from eq_widgets import InstrumentFaderBank
try:
    from sea_widget import SeaWidget
    HAS_SEA = True
except ImportError as _e:
    HAS_SEA = False
    _SEA_IMPORT_ERR = str(_e)

# ======================================================================
# 設定
# ======================================================================
LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 5000
FS = 256
WINDOW_SEC = 5
BUF_LEN = FS * WINDOW_SEC
CH_NAMES = ["TP9", "AF7", "AF8", "TP10"]
CH_COLORS = ["#ff6b6b", "#4ecdc4", "#45b7d1", "#feca57"]
CH_LOCATIONS = {
    "TP9":  "左耳後ろ",
    "AF7":  "左前頭",
    "AF8":  "右前頭",
    "TP10": "右耳後ろ",
}
BAND_NAMES = ["delta", "theta", "alpha", "beta", "gamma"]
BAND_COLORS = ["#8e44ad", "#3498db", "#2ecc71", "#f39c12", "#e74c3c"]
QUALITY_COLORS = {1.0: "#2ecc71", 2.0: "#f39c12", 4.0: "#e74c3c"}
QUALITY_TEXT = {1.0: "Good", 2.0: "OK", 4.0: "Bad"}
QUALITY_DESC = {
    1.0: "良好：クリーンな信号",
    2.0: "普通：使用可能",
    4.0: "不良：ノイズが多い",
}

_SOS_BP = butter(4, [1.0, 40.0], btype="bandpass", fs=FS, output="sos")

# PPG/光学センサー (Muse S Athena)
PPG_FS = 64                    # Mind Monitor で一般的な PPG/optics レート目安
PPG_BUF_LEN = PPG_FS * 10      # 10秒バッファ
# HR検出用バンドパス (0.7-3 Hz = 42-180 BPM)
_SOS_PPG = butter(3, [0.7, 3.0], btype="bandpass", fs=PPG_FS, output="sos")

SPEC_COLS = 180
SPEC_FMAX = 40
SPEC_NPERSEG = 128
SPEC_UPDATE_INTERVAL = 0.2

# Wavelet Scalogram
CWT_NFREQS = 80
CWT_FREQS = np.linspace(1.0, SPEC_FMAX, CWT_NFREQS)
CWT_UPDATE_INTERVAL = 0.5

# Russell's Circumplex 履歴
EMO_HIST_LEN = 300        # 時系列プロット用 (約10秒)
RUSSELL_TRAIL_LEN = 60    # Russell 2D 軌跡 (約2秒、見やすさ優先)

# 録画
REC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")


# ======================================================================
# カラーマップ (Mind Monitor 風 jet)
# ======================================================================
def make_jet_like_cmap():
    """ネオン rainbow colormap (mockup 風: 紫 → 青 → 緑 → 黄 → 赤)."""
    pos = np.array([0.0, 0.18, 0.36, 0.55, 0.75, 0.90, 1.0])
    colors = np.array([
        [12, 8, 30, 255],       # 黒紫 (背景)
        [80, 30, 180, 255],     # 深紫
        [50, 100, 240, 255],    # シアン青
        [50, 220, 180, 255],    # ターコイズ緑
        [240, 220, 60, 255],    # 黄
        [255, 120, 50, 255],    # オレンジ
        [255, 50, 90, 255],     # ピンク赤
    ], dtype=np.uint8)
    return pg.ColorMap(pos, colors)


# ======================================================================
# 共有状態
# ======================================================================
class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.eeg_buf = [deque([0.0] * BUF_LEN, maxlen=BUF_LEN) for _ in range(4)]
        self.bands = {b: [0.0, 0.0, 0.0, 0.0] for b in BAND_NAMES}
        self.quality = [4.0, 4.0, 4.0, 4.0]
        self.touching = 0
        self.blink = 0
        self.jaw = 0
        self.msg_count = 0
        self.last_eeg_time = 0.0

        # 光学・PPG
        self.optics_buf = [deque([0.0] * PPG_BUF_LEN, maxlen=PPG_BUF_LEN)
                           for _ in range(4)]
        self.ppg_buf = [deque([0.0] * PPG_BUF_LEN, maxlen=PPG_BUF_LEN)
                        for _ in range(3)]
        self.last_optics_time = 0.0
        self.last_ppg_time = 0.0
        self.heart_rate = 0.0  # BPM


state = SharedState()


# ======================================================================
# OSC ハンドラ
# ======================================================================
def on_eeg(addr, *args):
    with state.lock:
        for i in range(min(4, len(args))):
            state.eeg_buf[i].append(float(args[i]))
        state.msg_count += 1
        state.last_eeg_time = time.time()


def make_band_handler(name):
    def h(addr, *args):
        with state.lock:
            state.bands[name] = [float(v) for v in args[:4]]
    return h


def on_horseshoe(addr, *args):
    with state.lock:
        state.quality = [float(v) for v in args[:4]]


def on_touching(addr, *args):
    with state.lock:
        state.touching = int(args[0]) if args else 0


def on_blink(addr, *args):
    with state.lock:
        state.blink = int(args[0]) if args else 0


def on_jaw(addr, *args):
    with state.lock:
        state.jaw = int(args[0]) if args else 0


def on_optics(addr, *args):
    """Muse S Athena optical sensor (fNIRS)"""
    with state.lock:
        for i in range(min(4, len(args))):
            state.optics_buf[i].append(float(args[i]))
        state.last_optics_time = time.time()


def on_ppg(addr, *args):
    """Muse 2/S PPG sensor"""
    with state.lock:
        for i in range(min(3, len(args))):
            state.ppg_buf[i].append(float(args[i]))
        state.last_ppg_time = time.time()


def on_hr(addr, *args):
    with state.lock:
        state.heart_rate = float(args[0]) if args else 0.0


def start_osc_thread():
    disp = dispatcher.Dispatcher()
    disp.map("/muse/eeg", on_eeg)
    disp.map("/muse/elements/delta_absolute", make_band_handler("delta"))
    disp.map("/muse/elements/theta_absolute", make_band_handler("theta"))
    disp.map("/muse/elements/alpha_absolute", make_band_handler("alpha"))
    disp.map("/muse/elements/beta_absolute", make_band_handler("beta"))
    disp.map("/muse/elements/gamma_absolute", make_band_handler("gamma"))
    disp.map("/muse/elements/horseshoe", on_horseshoe)
    disp.map("/muse/elements/touching_forehead", on_touching)
    disp.map("/muse/elements/blink", on_blink)
    disp.map("/muse/elements/jaw_clench", on_jaw)
    disp.map("/muse/optics", on_optics)
    disp.map("/muse/ppg", on_ppg)
    disp.map("/muse/elements/experimental/heart_rate", on_hr)
    server = osc_server.ThreadingOSCUDPServer((LISTEN_IP, LISTEN_PORT), disp)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ======================================================================
# 感情推定 (Russell's Circumplex Model のみ)
# ======================================================================
def compute_russell(bands, quality):
    """
    Russell's Circumplex Model (Russell 1980):
      感情を2次元で表現: Arousal (活性度) × Valence (快/不快)
      4象限:
         (V高, A高) = 興奮・喜び
         (V低, A高) = ストレス・怒り
         (V低, A低) = 悲しみ・抑うつ
         (V高, A低) = 平静・リラックス

    指標:
      Arousal = β/α (平均)                Ramirez & Vamvakousis (2012)
      Valence = ln(α_AF8) - ln(α_AF7)    Davidson (1992)
    """
    alpha = bands["alpha"]
    beta = bands["beta"]
    valid = [i for i in range(4) if quality[i] <= 2.0]

    a_ratios = [beta[i] / alpha[i] for i in valid if alpha[i] > 0.01]
    arousal_raw = np.mean(a_ratios) if a_ratios else 1.0
    arousal = float(np.clip((arousal_raw - 0.3) / 2.7, 0.0, 1.0))

    if alpha[1] > 0.01 and alpha[2] > 0.01 and quality[1] <= 2.0 and quality[2] <= 2.0:
        val_raw = np.log(alpha[2] + 1e-6) - np.log(alpha[1] + 1e-6)
        valence = float(np.clip(val_raw * 0.5 + 0.5, 0.0, 1.0))
    else:
        valence = 0.5

    if arousal > 0.6 and valence > 0.6:
        label = "😊 Excited / Happy"
    elif arousal > 0.6 and valence < 0.4:
        label = "😠 Stressed / Angry"
    elif arousal < 0.4 and valence < 0.4:
        label = "😔 Sad / Depressed"
    elif arousal < 0.4 and valence > 0.6:
        label = "😌 Calm / Relaxed"
    else:
        label = "😐 Neutral"

    return {"arousal": arousal, "valence": valence, "label": label}


def compute_engagement(bands, quality):
    """
    Engagement Index (Pope, Bogart & Bartolome, 1995):
      Engagement = β / (α + θ)
      航空機パイロット/オペレーター研究で使用。タスクへの集中・注意度を反映。
    """
    alpha = bands["alpha"]
    beta = bands["beta"]
    theta = bands["theta"]
    valid = [i for i in range(4) if quality[i] <= 2.0]
    ratios = []
    for i in valid:
        denom = alpha[i] + theta[i]
        if denom > 0.01:
            ratios.append(beta[i] / denom)
    raw = float(np.mean(ratios)) if ratios else 0.5
    # 経験的スケーリング
    return float(np.clip((raw - 0.2) / 1.8, 0.0, 1.0))


def compute_arousal_only(bands, quality):
    """
    Arousal Index (Ramirez & Vamvakousis, 2012):
      Arousal = β / α  (全チャンネル平均)
      音楽感情研究で用いられる覚醒度メーター。値が高いほど覚醒。
    """
    alpha = bands["alpha"]
    beta = bands["beta"]
    valid = [i for i in range(4) if quality[i] <= 2.0]
    ratios = [beta[i] / alpha[i] for i in valid if alpha[i] > 0.01]
    raw = float(np.mean(ratios)) if ratios else 1.0
    return float(np.clip((raw - 0.3) / 2.7, 0.0, 1.0))


# ======================================================================
# カード
# ======================================================================
class _HoverInfoPopup(QtWidgets.QFrame):
    """カード hover で出るフローティング情報パネル.
    accent neon border + フェードイン + 軽い上昇アニメ."""

    def __init__(self, theme=None, parent=None):
        super().__init__(parent, QtCore.Qt.ToolTip
                         | QtCore.Qt.FramelessWindowHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self._theme = theme
        self._title = ""
        self._body = ""
        self._anim = None
        # 内側 QLabel 構造
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(6)
        self._title_lbl = QtWidgets.QLabel()
        self._title_lbl.setObjectName("hov_title")
        self._body_lbl = QtWidgets.QLabel()
        self._body_lbl.setObjectName("hov_body")
        self._body_lbl.setWordWrap(True)
        lay.addWidget(self._title_lbl)
        lay.addWidget(self._body_lbl)
        self.setMinimumWidth(280)
        self.setMaximumWidth(360)
        # DropShadow は translucent toplevel での DWM 衝突を避けて省略
        self._shadow = None
        self._restyle()

    def _restyle(self):
        t = self._theme
        if t is None:
            accent = "#1abc9c"
            bg = "rgba(10, 10, 16, 235)"
            text_main = "#e8e8e8"
            text_dim = "#9a9a9a"
        else:
            accent = t.accent
            bg = "rgba(10, 10, 16, 235)"
            text_main = t.text_main
            text_dim = t.text_dim
        self.setStyleSheet(
            f"_HoverInfoPopup {{ background-color: {bg}; "
            f"border: 2px solid {accent}; border-radius: 10px; }}"
            f"QLabel#hov_title {{ color: {accent}; font-size: 13px; "
            "font-weight: bold; letter-spacing: 1px; "
            "background: transparent; border: none; }"
            f"QLabel#hov_body {{ color: {text_main}; font-size: 11px; "
            "background: transparent; border: none; "
            "line-height: 1.4; }"
        )
        if self._shadow is not None:
            ar = QtGui.QColor(accent).red()
            ag = QtGui.QColor(accent).green()
            ab = QtGui.QColor(accent).blue()
            self._shadow.setColor(QtGui.QColor(ar, ag, ab, 200))

    def set_content(self, title, body):
        self._title_lbl.setText(title)
        self._body_lbl.setText(body)
        self.adjustSize()

    def show_at(self, global_pos):
        """位置調整して表示 + フェードイン."""
        self.move(global_pos)
        self.show()
        self.raise_()
        # 上昇 + フェード
        start_geom = self.geometry()
        start_geom_up = QtCore.QRect(
            start_geom.x(), start_geom.y() + 12,
            start_geom.width(), start_geom.height())
        # スタート位置 (12px 下) → 終了位置 (元)
        self.setGeometry(start_geom_up)
        anim = QtCore.QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(180)
        anim.setStartValue(start_geom_up)
        anim.setEndValue(start_geom)
        anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        anim.start(QtCore.QAbstractAnimation.DeleteWhenStopped)
        self._anim = anim


# Module-level singleton (lazy)
_HOVER_POPUP = None


def _get_hover_popup(theme):
    global _HOVER_POPUP
    if _HOVER_POPUP is None:
        _HOVER_POPUP = _HoverInfoPopup(theme=theme)
    else:
        _HOVER_POPUP._theme = theme
        _HOVER_POPUP._restyle()
    return _HOVER_POPUP


class Card(QtWidgets.QFrame):
    expand_requested = QtCore.pyqtSignal(str)
    swap_requested = QtCore.pyqtSignal(str, str)   # (src_id, dst_id)

    def __init__(self, title, content_widget, card_id, parent=None, theme=None,
                 accent_border=False):
        super().__init__(parent)
        self.card_id = card_id
        self.is_expanded = False
        self._theme = theme
        self._accent_border = accent_border
        self._hover_title = ""
        self._hover_body = ""
        self._drag_press_pos = None
        self.setAcceptDrops(True)
        self.setObjectName("card")
        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 3)
        shadow.setColor(QtGui.QColor(0, 0, 0, 110))
        self.setGraphicsEffect(shadow)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(8)
        hdr = QtWidgets.QHBoxLayout()
        self.title_lbl = QtWidgets.QLabel(title)
        hdr.addWidget(self.title_lbl)
        hdr.addStretch()
        self.expand_btn = QtWidgets.QPushButton("⛶")
        self.expand_btn.setFixedSize(26, 22)
        self.expand_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.expand_btn.setToolTip("拡大 / 元に戻す")
        self.expand_btn.clicked.connect(lambda: self.expand_requested.emit(self.card_id))
        hdr.addWidget(self.expand_btn)
        v.addLayout(hdr)
        v.addWidget(content_widget, 1)

        self._apply_theme()
        if self._theme is not None:
            self._theme.subscribe(lambda *_: self._apply_theme())

    def _apply_theme(self):
        t = self._theme
        bg_panel = t.bg_panel if t else "#1f1f21"
        border = t.border if t else "#2a2a2c"
        accent = t.accent if t else "#1abc9c"
        text_dim = t.text_dim if t else "#b0b0b0"
        # accent_border 指定時はアクセント色 + glow を強める
        # それ以外は薄いアクセント色のボーダー (静かなネオン感)
        if self._accent_border:
            base_border = accent
            glow_alpha = 70
        else:
            # アクセント色を 25% 透明度で薄く
            ag = QtGui.QColor(accent)
            ag.setAlpha(70)
            base_border = ag.name(QtGui.QColor.HexArgb) if False else accent
            # QSS は ARGB のフルセットを受け付けるので rgba() を使う
            rr, gg, bb = QtGui.QColor(accent).red(), QtGui.QColor(accent).green(), QtGui.QColor(accent).blue()
            base_border = f"rgba({rr}, {gg}, {bb}, 90)"
            glow_alpha = 40

        self.setStyleSheet(
            f"QFrame#card {{ background-color: {bg_panel}; "
            f"border: 1px solid {base_border}; border-radius: 12px; }}"
            f"QFrame#card:hover {{ border: 1px solid {accent}; }}"
            "QLabel { border: none; }"
        )
        # ホバー時のグロー (DropShadow を accent 色に)
        shadow = self.graphicsEffect()
        if shadow is not None:
            ar, ag2, ab = (QtGui.QColor(accent).red(),
                           QtGui.QColor(accent).green(),
                           QtGui.QColor(accent).blue())
            shadow.setColor(QtGui.QColor(ar, ag2, ab, glow_alpha))
            shadow.setBlurRadius(28 if self._accent_border else 18)

        self.title_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {text_dim}; "
            "letter-spacing: 0.8px;")
        self.expand_btn.setStyleSheet(
            "QPushButton {"
            f" background-color: transparent; color: {text_dim};"
            f" border: 1px solid {border}; border-radius: 6px; font-size: 12px;"
            "}"
            f"QPushButton:hover {{ background-color: {accent}; "
            f"color: #ffffff; border-color: {accent}; }}"
        )

    def set_expanded(self, expanded):
        self.is_expanded = expanded

    def set_hover_info(self, title, body):
        """カードに対する hover ポップアップ用 情報セット."""
        self._hover_title = title
        self._hover_body = body

    def enterEvent(self, event):
        if self._hover_title or self._hover_body:
            popup = _get_hover_popup(self._theme)
            popup.set_content(self._hover_title, self._hover_body)
            # カード右上に表示
            g = self.mapToGlobal(QtCore.QPoint(self.width() + 10, 0))
            popup.show_at(g)
        super().enterEvent(event)

    def leaveEvent(self, event):
        popup = _get_hover_popup(self._theme)
        popup.hide()
        super().leaveEvent(event)

    # ---- ドラッグ＆ドロップで並び替え (タイトル部分でのみドラッグ開始) ----
    def _in_title_area(self, pos):
        # タイトルバー領域 (上 36px) でドラッグ開始可
        return pos.y() <= 36 and pos.x() < self.width() - 40

    def mousePressEvent(self, event):
        if (event.button() == QtCore.Qt.LeftButton
                and self._in_title_area(event.pos())
                and not self.is_expanded):
            self._drag_press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_press_pos is not None
                and (event.buttons() & QtCore.Qt.LeftButton)):
            dist = (event.pos() - self._drag_press_pos).manhattanLength()
            if dist > QtWidgets.QApplication.startDragDistance():
                self._start_drag()
                self._drag_press_pos = None
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_press_pos = None
        super().mouseReleaseEvent(event)

    def _start_drag(self):
        drag = QtGui.QDrag(self)
        mime = QtCore.QMimeData()
        mime.setText(f"card:{self.card_id}")
        drag.setMimeData(mime)
        # プレビュー pixmap
        pix = self.grab()
        scaled = pix.scaled(220, 130,
                            QtCore.Qt.KeepAspectRatio,
                            QtCore.Qt.SmoothTransformation)
        # 半透明にする
        out = QtGui.QPixmap(scaled.size())
        out.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(out)
        p.setOpacity(0.75)
        p.drawPixmap(0, 0, scaled)
        p.end()
        drag.setPixmap(out)
        drag.setHotSpot(QtCore.QPoint(out.width() // 2, 16))
        drag.exec_(QtCore.Qt.MoveAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasText() and event.mimeData().text().startswith("card:"):
            event.acceptProposedAction()
            # ハイライト (一時的に accent ボーダー太く)
            self._set_drop_highlight(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drop_highlight(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._set_drop_highlight(False)
        text = event.mimeData().text()
        if not text.startswith("card:"):
            event.ignore()
            return
        src_id = text[len("card:"):]
        if src_id == self.card_id:
            event.ignore()
            return
        self.swap_requested.emit(src_id, self.card_id)
        event.acceptProposedAction()

    def _set_drop_highlight(self, on):
        t = self._theme
        accent = t.accent if t else "#1abc9c"
        bg_panel = t.bg_panel if t else "#1f1f21"
        if on:
            self.setStyleSheet(
                f"QFrame#card {{ background-color: {bg_panel}; "
                f"border: 3px dashed {accent}; border-radius: 12px; }}"
                "QLabel { border: none; }"
            )
        else:
            self._apply_theme()


class _OutputSpectrumBar(QtWidgets.QWidget):
    """audio_engine の出力 FFT を視覚化する小型バー.
    EQ 設定変化を**目で確認**するためのデバッグ的可視化."""

    def __init__(self, n_bins=22, parent=None):
        super().__init__(parent)
        self._n_bins = n_bins
        self._values = [0.0] * n_bins
        self._accent = QtGui.QColor("#1abc9c")
        self.setMinimumWidth(180)
        self.setMaximumWidth(240)
        self.setFixedHeight(24)
        self.setToolTip(
            "Audio output spectrum (log-frequency 50Hz–20kHz)\n"
            "EQ 操作で帯域の高さが変化する")

    def set_values(self, vals):
        if len(vals) != self._n_bins:
            return
        # 簡易減衰 (フォール感)
        for i, v in enumerate(vals):
            cur = self._values[i]
            self._values[i] = max(float(v), cur * 0.88)
        self.update()

    def set_accent(self, hex_color):
        self._accent = QtGui.QColor(hex_color)
        self.update()

    def paintEvent(self, event):
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        # 背景
        qp.setPen(QtCore.Qt.NoPen)
        qp.setBrush(QtGui.QColor(255, 255, 255, 18))
        qp.drawRoundedRect(0, 0, w, h, 3, 3)
        n = self._n_bins
        gap = 1
        bw = (w - gap * (n - 1)) / n
        a = self._accent
        for i, v in enumerate(self._values):
            x = i * (bw + gap)
            bh = max(1.5, v * (h - 2))
            y = h - bh - 1
            col = QtGui.QColor(a.red(), a.green(), a.blue(),
                                int(120 + 135 * v))
            qp.setBrush(col)
            qp.drawRoundedRect(QtCore.QRectF(x, y, bw, bh), 1.0, 1.0)
        qp.end()


class _StatusMeter(QtWidgets.QWidget):
    """Watch status bar 用の段ブロックメータ. value(0..1) で本数増える."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0.3
        self._accent = QtGui.QColor("#1abc9c")
        self._n_blocks = 14

    def set_value(self, v):
        v = max(0.0, min(1.0, float(v)))
        if abs(v - self._value) < 0.01:
            return
        self._value = v
        self.update()

    def set_accent(self, hex_color):
        self._accent = QtGui.QColor(hex_color)
        self.update()

    def paintEvent(self, event):
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        gap = 2
        block_w = (w - gap * (self._n_blocks - 1)) / self._n_blocks
        active = int(round(self._n_blocks * self._value))
        a = self._accent
        for i in range(self._n_blocks):
            x = i * (block_w + gap)
            if i < active:
                col = QtGui.QColor(a.red(), a.green(), a.blue(), 230)
            else:
                col = QtGui.QColor(a.red(), a.green(), a.blue(), 40)
            qp.setPen(QtCore.Qt.NoPen)
            qp.setBrush(col)
            qp.drawRoundedRect(QtCore.QRectF(x, 0, block_w, h), 1.5, 1.5)
        qp.end()


class _SettingsDialog(QtWidgets.QDialog):
    """設定: HR 閾値 / EMA τ / Reverb 最大 wet 等のチューニング."""

    def __init__(self, parent=None, theme=None):
        super().__init__(parent)
        self.setWindowTitle("⚙ Settings")
        self.setMinimumWidth(420)
        self._theme = theme
        self._parent_app = parent
        import sea_widget as sw
        import eq_controllers as ec

        layout = QtWidgets.QFormLayout(self)
        layout.setVerticalSpacing(12)

        # HR 閾値
        self.hr_mid_enter = QtWidgets.QSpinBox()
        self.hr_mid_enter.setRange(40, 120)
        self.hr_mid_enter.setValue(int(sw.HR_MID_ENTER))
        self.hr_mid_enter.setSuffix(" BPM")
        layout.addRow("HR Mid enter ≥", self.hr_mid_enter)

        self.hr_mid_exit = QtWidgets.QSpinBox()
        self.hr_mid_exit.setRange(40, 120)
        self.hr_mid_exit.setValue(int(sw.HR_MID_EXIT))
        self.hr_mid_exit.setSuffix(" BPM")
        layout.addRow("HR Mid exit ≤", self.hr_mid_exit)

        self.hr_high_enter = QtWidgets.QSpinBox()
        self.hr_high_enter.setRange(50, 160)
        self.hr_high_enter.setValue(int(sw.HR_HIGH_ENTER))
        self.hr_high_enter.setSuffix(" BPM")
        layout.addRow("HR High enter ≥", self.hr_high_enter)

        self.hr_high_exit = QtWidgets.QSpinBox()
        self.hr_high_exit.setRange(50, 160)
        self.hr_high_exit.setValue(int(sw.HR_HIGH_EXIT))
        self.hr_high_exit.setSuffix(" BPM")
        layout.addRow("HR High exit ≤", self.hr_high_exit)

        # EMA τ
        self.eq_alpha = QtWidgets.QDoubleSpinBox()
        self.eq_alpha.setRange(0.001, 0.5)
        self.eq_alpha.setSingleStep(0.001)
        self.eq_alpha.setDecimals(3)
        self.eq_alpha.setValue(ec.ALPHA_DEFAULT)
        layout.addRow("EQ EMA α (small=遅い)", self.eq_alpha)

        # EQ push interval
        self.push_interval = QtWidgets.QDoubleSpinBox()
        self.push_interval.setRange(0.02, 1.0)
        self.push_interval.setSingleStep(0.01)
        self.push_interval.setDecimals(2)
        self.push_interval.setValue(ec.PUSH_MIN_INTERVAL_SEC)
        self.push_interval.setSuffix(" s")
        layout.addRow("EQ push min interval", self.push_interval)

        # Reverb wet max
        import audio_engine as ae
        self.reverb_max = QtWidgets.QDoubleSpinBox()
        self.reverb_max.setRange(0.0, 1.0)
        self.reverb_max.setSingleStep(0.05)
        self.reverb_max.setDecimals(2)
        self.reverb_max.setValue(ae.REVERB_WET_MAX)
        layout.addRow("Reverb wet max", self.reverb_max)

        # Apply / Cancel
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._apply)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

        # スタイル
        accent = theme.accent if theme else "#1abc9c"
        self.setStyleSheet(
            "QDialog { background-color: #131318; color: #e8e8e8; }"
            "QLabel { color: #c0c0c0; }"
            "QSpinBox, QDoubleSpinBox { background-color: #1e1e22; "
            "color: #f0f0f0; border: 1px solid #353539; border-radius: 4px; "
            "padding: 3px 6px; min-width: 120px; }"
            f"QPushButton {{ background-color: {accent}; color: #000; "
            "border: none; border-radius: 6px; padding: 5px 16px; "
            "font-weight: bold; }}"
            f"QPushButton:hover {{ background-color: #ffffff; }}"
        )

    def _apply(self):
        import sea_widget as sw
        import eq_controllers as ec
        import audio_engine as ae
        sw.HR_MID_ENTER = float(self.hr_mid_enter.value())
        sw.HR_MID_EXIT = float(self.hr_mid_exit.value())
        sw.HR_HIGH_ENTER = float(self.hr_high_enter.value())
        sw.HR_HIGH_EXIT = float(self.hr_high_exit.value())
        ec.ALPHA_DEFAULT = float(self.eq_alpha.value())
        ec.PUSH_MIN_INTERVAL_SEC = float(self.push_interval.value())
        ae.REVERB_WET_MAX = float(self.reverb_max.value())
        # ReflectController の現値も更新 (parent が MainWindow)
        if self._parent_app is not None and hasattr(self._parent_app, "_reflect_ctrl"):
            ctrl = self._parent_app._reflect_ctrl
            ctrl.alpha = ec.ALPHA_DEFAULT
            ctrl.push_interval = ec.PUSH_MIN_INTERVAL_SEC
        self.accept()


class _WelcomeOverlay(QtWidgets.QWidget):
    """初回起動時のウェルカム + 機能ハイライト. 半透明 + 中央メッセージ."""

    closed = QtCore.pyqtSignal()

    def __init__(self, parent=None, accent="#1abc9c"):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self._accent = accent
        # 中央パネル
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(QtCore.Qt.AlignCenter)
        panel = QtWidgets.QFrame()
        panel.setObjectName("welcome_panel")
        panel.setFixedWidth(520)
        pv = QtWidgets.QVBoxLayout(panel)
        pv.setContentsMargins(36, 28, 36, 28)
        pv.setSpacing(14)

        title = QtWidgets.QLabel("🧠  Welcome to EEG Adaptive EQ")
        title.setObjectName("welcome_title")
        title.setAlignment(QtCore.Qt.AlignCenter)
        sub = QtWidgets.QLabel(
            "Your brain controls your music.<br>"
            "Real-time EEG / PPG → audio + visuals.")
        sub.setObjectName("welcome_sub")
        sub.setAlignment(QtCore.Qt.AlignCenter)
        sub.setTextFormat(QtCore.Qt.RichText)
        sub.setWordWrap(True)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        sep.setStyleSheet(f"background-color: {accent}; max-height: 1px;")

        tips = QtWidgets.QLabel(
            "<table cellspacing=6>"
            "<tr><td><b>1 / 2 / 3</b></td>"
            "<td>🧠 Studio  /  🎚 Listen  /  🌊 Watch</td></tr>"
            "<tr><td><b>Space</b></td>"
            "<td>♪ Audio ON/OFF</td></tr>"
            "<tr><td><b>R</b></td><td>● Toggle REC</td></tr>"
            "<tr><td><b>F1</b></td><td>Show shortcuts</td></tr>"
            "<tr><td>&nbsp;</td><td>&nbsp;</td></tr>"
            "<tr><td>📡</td>"
            "<td>Mind Monitor → OSC :5000 で接続</td></tr>"
            "<tr><td>🎵</td>"
            "<td>Spotify → VB-CABLE → EQ → 出力</td></tr>"
            "</table>")
        tips.setObjectName("welcome_tips")
        tips.setTextFormat(QtCore.Qt.RichText)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        ok = QtWidgets.QPushButton("Got it  →")
        ok.setObjectName("welcome_ok")
        ok.setCursor(QtCore.Qt.PointingHandCursor)
        ok.setFixedHeight(36)
        ok.clicked.connect(self._on_close)
        btn_row.addWidget(ok)
        btn_row.addStretch()

        pv.addWidget(title)
        pv.addWidget(sub)
        pv.addWidget(sep)
        pv.addWidget(tips)
        pv.addLayout(btn_row)

        lay.addWidget(panel)
        self._panel = panel
        self._restyle()

    def _restyle(self):
        accent = self._accent
        self.setStyleSheet(
            "_WelcomeOverlay { background-color: rgba(0, 0, 0, 180); }"
            f"QFrame#welcome_panel {{ background-color: rgba(10, 10, 16, 250); "
            f"border: 2px solid {accent}; border-radius: 14px; }}"
            f"QLabel#welcome_title {{ color: {accent}; font-size: 20px; "
            "font-weight: bold; letter-spacing: 1px; "
            "background: transparent; border: none; }"
            "QLabel#welcome_sub { color: #d0d0d0; font-size: 13px; "
            "background: transparent; border: none; line-height: 1.5; }"
            "QLabel#welcome_tips { color: #c0c0c0; font-size: 12px; "
            "background: transparent; border: none; }"
            f"QPushButton#welcome_ok {{ background-color: {accent}; "
            "color: #000000; border: none; border-radius: 18px; "
            "padding: 6px 24px; font-size: 12px; font-weight: bold; "
            "letter-spacing: 1px; }}"
            f"QPushButton#welcome_ok:hover {{ background-color: #ffffff; }}"
        )
        # accent glow on panel
        eff = QtWidgets.QGraphicsDropShadowEffect(self._panel)
        eff.setBlurRadius(40)
        eff.setOffset(0, 0)
        ar, ag, ab = (QtGui.QColor(accent).red(),
                       QtGui.QColor(accent).green(),
                       QtGui.QColor(accent).blue())
        eff.setColor(QtGui.QColor(ar, ag, ab, 220))
        self._panel.setGraphicsEffect(eff)

    def _on_close(self):
        # フェードアウト
        try:
            eff = QtWidgets.QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(eff)
            eff.setOpacity(1.0)
            anim = QtCore.QPropertyAnimation(eff, b"opacity", self)
            anim.setDuration(280)
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.finished.connect(self._finish)
            anim.start(QtCore.QAbstractAnimation.DeleteWhenStopped)
        except Exception:
            self._finish()

    def _finish(self):
        self.closed.emit()
        self.deleteLater()

    def resizeEvent(self, event):
        # 親の上に重ねるためフルサイズ
        if self.parent() is not None:
            self.setGeometry(self.parent().rect())
        super().resizeEvent(event)


class _Toast(QtWidgets.QFrame):
    """画面右下に出る通知トースト. accent neon border + 自動フェードアウト."""

    def __init__(self, parent, text, accent="#1abc9c", duration_ms=2800):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setObjectName("toast")
        self._text = text
        self._accent = accent
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 16, 10)
        lay.setSpacing(10)
        self._label = QtWidgets.QLabel(text)
        self._label.setObjectName("toast_label")
        lay.addWidget(self._label)
        self.adjustSize()
        # スタイル
        self.setStyleSheet(
            f"QFrame#toast {{ background-color: rgba(8, 8, 14, 235); "
            f"border: 2px solid {accent}; border-radius: 10px; }}"
            f"QLabel#toast_label {{ color: #ffffff; font-size: 12px; "
            "font-weight: 500; letter-spacing: 0.5px; "
            "background: transparent; border: none; }"
        )
        # DropShadow は translucent 子 widget で
        # Windows DWM の UpdateLayeredWindowIndirect 警告を出すため省略
        # 自動フェードアウト
        QtCore.QTimer.singleShot(duration_ms, self._fade_out)

    def _fade_out(self):
        try:
            eff = QtWidgets.QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(eff)
            eff.setOpacity(1.0)
            anim = QtCore.QPropertyAnimation(eff, b"opacity", self)
            anim.setDuration(280)
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.finished.connect(self.deleteLater)
            anim.start(QtCore.QAbstractAnimation.DeleteWhenStopped)
        except Exception:
            self.deleteLater()


class _DemoExplainOverlay(QtWidgets.QFrame):
    """Watch のデモモード中だけ右側に出る説明パネル.

    Qt の QVBoxLayout + QLabel + QProgressBar で組むため、
    幅変化やフォントメトリクスのズレで切れる事がない (custom paint版を撤去).
    """

    EEG_ACCENT = "#9b59b6"
    HR_ACCENT = "#e74c3c"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("demoPanel")
        # 半透明の暗いパネル背景 + 角丸枠
        self.setStyleSheet("""
            QFrame#demoPanel {
                background-color: rgba(10, 10, 14, 230);
                border: 1px solid rgba(255, 255, 255, 30);
                border-radius: 12px;
            }
            QLabel { color: #e6e6e6; }
        """)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(22, 22, 22, 22)
        v.setSpacing(12)

        # ----- ヘッダ (大きめ) -----
        hdr = QtWidgets.QHBoxLayout()
        hdr.setSpacing(8)
        self._title_lbl = QtWidgets.QLabel("▶  DEMO  MODE")
        self._title_lbl.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #ffffff; "
            "letter-spacing: 3px;")
        hdr.addWidget(self._title_lbl)
        hdr.addStretch()
        self._time_lbl = QtWidgets.QLabel("⏱ 0.0 / 60s")
        self._time_lbl.setStyleSheet(
            "font-family: 'Consolas'; font-size: 13px; color: #b0b0b0;")
        hdr.addWidget(self._time_lbl)
        v.addLayout(hdr)

        # ----- Phase ピル (高さ大きく) -----
        self._phase_pill = QtWidgets.QLabel("PHASE · CALM")
        self._phase_pill.setAlignment(QtCore.Qt.AlignCenter)
        self._phase_pill.setFixedHeight(38)
        self._update_phase_pill_style("surface")
        v.addWidget(self._phase_pill)

        # ----- Progress bar -----
        self._prog = QtWidgets.QProgressBar()
        self._prog.setRange(0, 1000)
        self._prog.setValue(0)
        self._prog.setTextVisible(False)
        self._prog.setFixedHeight(8)
        self._prog.setStyleSheet(self._progress_style("#9b59b6"))
        v.addWidget(self._prog)

        v.addStretch(1)   # ↓ EEG セクションまでに余白
        v.addWidget(self._make_divider())

        # ----- EEG section -----
        self._eeg_title = QtWidgets.QLabel("🧠  EEG  →  Surface")
        self._eeg_title.setStyleSheet(
            f"font-size: 14px; font-weight: bold; "
            f"color: {self.EEG_ACCENT}; padding: 4px 0;")
        v.addWidget(self._eeg_title)

        self._aro_row, self._aro_bar, self._aro_val = \
            self._make_meter_row("Arousal", "#e74c3c")
        self._val_row, self._val_bar, self._val_val = \
            self._make_meter_row("Valence", "#2ecc71")
        self._eng_row, self._eng_bar, self._eng_val = \
            self._make_meter_row("Engagement", "#3498db")
        v.addLayout(self._aro_row)
        v.addLayout(self._val_row)
        v.addLayout(self._eng_row)

        v.addStretch(1)
        v.addWidget(self._make_divider())

        # ----- HR section -----
        self._hr_title = QtWidgets.QLabel("♥  HEART RATE  →  Underwater")
        self._hr_title.setStyleSheet(
            f"font-size: 14px; font-weight: bold; "
            f"color: {self.HR_ACCENT}; padding: 4px 0;")
        v.addWidget(self._hr_title)

        self._bpm_row, self._bpm_bar, self._bpm_val = \
            self._make_meter_row("BPM", "#e74c3c", value_text="60")
        v.addLayout(self._bpm_row)

        zone_row = QtWidgets.QHBoxLayout()
        zlbl = QtWidgets.QLabel("Zone")
        zlbl.setStyleSheet("font-size: 13px; color: #c0c0c0;")
        zlbl.setFixedWidth(110)
        zone_row.addWidget(zlbl)
        self._zone_lbl = QtWidgets.QLabel("LOW")
        self._zone_lbl.setStyleSheet(
            "font-family: 'Consolas'; font-size: 15px; "
            "font-weight: bold; color: #5fc7ff;")
        zone_row.addWidget(self._zone_lbl)
        zone_row.addStretch()
        v.addLayout(zone_row)

        v.addStretch(1)
        v.addWidget(self._make_divider())

        # ----- 解説 (下半分を占める) -----
        wh_title = QtWidgets.QLabel("▼  WHAT'S HAPPENING")
        wh_title.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #bdbdbd; "
            "letter-spacing: 2px; padding: 4px 0;")
        v.addWidget(wh_title)
        self._expl_lbl = QtWidgets.QLabel("")
        self._expl_lbl.setWordWrap(True)
        self._expl_lbl.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self._expl_lbl.setStyleSheet(
            "font-size: 13px; color: #e8e8e8; line-height: 1.6; "
            "padding: 4px 2px;")
        v.addWidget(self._expl_lbl, 2)   # stretch 2: 下半分を埋める

        # ----- フッタ: タイムライン (5 phase を 5 ブロックで可視化) -----
        v.addWidget(self._make_divider())
        tl_title = QtWidgets.QLabel("⌁  TIMELINE  (60s loop)")
        tl_title.setStyleSheet(
            "font-size: 11px; color: #909090; "
            "letter-spacing: 1px; padding: 2px 0;")
        v.addWidget(tl_title)
        # 5 つの phase インジケータ (現在 phase をハイライト)
        self._timeline_row = QtWidgets.QHBoxLayout()
        self._timeline_row.setSpacing(4)
        self._timeline_segments = []
        seg_defs = [
            ("CALM", self.EEG_ACCENT),
            ("RISING", self.EEG_ACCENT),
            ("INTENSE", self.EEG_ACCENT),
            ("STORMY", self.EEG_ACCENT),
            ("HR-arc", self.HR_ACCENT),
        ]
        for label, color in seg_defs:
            seg = QtWidgets.QLabel(label)
            seg.setAlignment(QtCore.Qt.AlignCenter)
            seg.setFixedHeight(22)
            seg.setStyleSheet(
                "QLabel { background-color: rgba(255,255,255,18); "
                "color: #a0a0a0; border-radius: 4px; "
                "font-size: 9px; font-weight: bold; "
                "letter-spacing: 1px; padding: 2px 4px; }")
            self._timeline_row.addWidget(seg, 1)
            self._timeline_segments.append((seg, label, color))
        v.addLayout(self._timeline_row)

    # ------------------------------------------------------------------
    def _make_divider(self):
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet("color: rgba(255,255,255,30);")
        line.setFixedHeight(1)
        return line

    def _make_meter_row(self, label_text, color_hex, value_text="0.00"):
        """ラベル + 横バー + 数値. 3 wedge レイアウトで重ならない."""
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        # ラベル (固定幅、大きめ)
        lbl = QtWidgets.QLabel(label_text)
        lbl.setStyleSheet("font-size: 13px; color: #d2d2d2;")
        lbl.setFixedWidth(110)
        row.addWidget(lbl)
        # バー (伸縮、太め)
        bar = QtWidgets.QProgressBar()
        bar.setRange(0, 1000)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(14)
        bar.setStyleSheet(self._progress_style(color_hex))
        row.addWidget(bar, 1)
        # 値テキスト (固定幅、右揃え)
        val = QtWidgets.QLabel(value_text)
        val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        val.setStyleSheet(
            "font-family: 'Consolas'; font-size: 13px; "
            "font-weight: bold; color: #f5f5f5;")
        val.setFixedWidth(52)
        row.addWidget(val)
        return row, bar, val

    def _progress_style(self, color_hex):
        return f"""
            QProgressBar {{
                background-color: rgba(255,255,255,22);
                border: none; border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background-color: {color_hex};
                border-radius: 4px;
            }}
        """

    def _update_phase_pill_style(self, sub_view):
        accent = self.EEG_ACCENT if sub_view == "surface" else self.HR_ACCENT
        self._phase_pill.setStyleSheet(
            f"QLabel {{ background-color: {accent}; color: #ffffff; "
            f"border-radius: 19px; padding: 6px 18px; "
            f"font-size: 14px; font-weight: bold; "
            f"letter-spacing: 3px; }}")

    def _highlight_timeline(self, phase, sub_view):
        """Phase 名に応じて 5 セグメントのどれかをハイライト."""
        if not hasattr(self, "_timeline_segments"):
            return
        # マッピング: phase 文字列 → segment index
        if sub_view == "underwater":
            active_idx = 4   # HR-arc
        else:
            mapping = {"CALM": 0, "RISING": 1, "INTENSE": 2, "STORMY": 3}
            active_idx = mapping.get(str(phase).upper(), 0)
        for i, (seg, label, color) in enumerate(self._timeline_segments):
            if i == active_idx:
                seg.setStyleSheet(
                    f"QLabel {{ background-color: {color}; "
                    f"color: #ffffff; border-radius: 4px; "
                    f"font-size: 9px; font-weight: bold; "
                    f"letter-spacing: 1px; padding: 2px 4px; }}")
            else:
                seg.setStyleSheet(
                    "QLabel { background-color: rgba(255,255,255,18); "
                    "color: #a0a0a0; border-radius: 4px; "
                    "font-size: 9px; font-weight: bold; "
                    "letter-spacing: 1px; padding: 2px 4px; }")

    # ------------------------------------------------------------------
    def set_state(self, arousal, valence, engagement, hr,
                  phase, sub_view, elapsed, total, explanation):
        # 時間 + 進捗
        self._time_lbl.setText(f"⏱ {elapsed:4.1f} / {int(total)}s")
        if total > 0:
            self._prog.setValue(int(elapsed / total * 1000))
        # phase ピル
        self._phase_pill.setText(f"PHASE · {phase}")
        self._update_phase_pill_style(sub_view)
        # タイムライン
        self._highlight_timeline(phase, sub_view)
        # progress bar 色も切替
        prog_color = self.EEG_ACCENT if sub_view == "surface" else self.HR_ACCENT
        self._prog.setStyleSheet(self._progress_style(prog_color))

        # アクティブ/非アクティブで section 透過
        eeg_active = (sub_view == "surface")
        hr_active = (sub_view == "underwater")
        eeg_op = 1.0 if eeg_active else 0.35
        hr_op = 1.0 if hr_active else 0.35
        for wid, op in [(self._eeg_title, eeg_op),
                        (self._aro_bar, eeg_op),
                        (self._val_bar, eeg_op),
                        (self._eng_bar, eeg_op),
                        (self._aro_val, eeg_op),
                        (self._val_val, eeg_op),
                        (self._eng_val, eeg_op),
                        (self._hr_title, hr_op),
                        (self._bpm_bar, hr_op),
                        (self._bpm_val, hr_op),
                        (self._zone_lbl, hr_op)]:
            self._apply_opacity(wid, op)

        # アクティブ section の suffix
        self._eeg_title.setText(
            "🧠  EEG  →  Surface" + ("   ← DRIVING NOW" if eeg_active else ""))
        self._hr_title.setText(
            "♥  HEART RATE  →  Underwater"
            + ("   ← DRIVING NOW" if hr_active else ""))

        # メータ値
        self._aro_bar.setValue(int(max(0.0, min(1.0, arousal)) * 1000))
        self._val_bar.setValue(int(max(0.0, min(1.0, valence)) * 1000))
        self._eng_bar.setValue(int(max(0.0, min(1.0, engagement)) * 1000))
        self._aro_val.setText(f"{arousal:.2f}")
        self._val_val.setText(f"{valence:.2f}")
        self._eng_val.setText(f"{engagement:.2f}")
        # HR (50-120 BPM → 0..1)
        hr_norm = max(0.0, min(1.0, (hr - 50.0) / 70.0))
        self._bpm_bar.setValue(int(hr_norm * 1000))
        self._bpm_val.setText(f"{int(hr)}")
        # zone
        zone = ("LOW" if hr < 75 else "MID" if hr < 90 else "HIGH")
        zone_color = {"LOW": "#5fc7ff", "MID": "#7ce8a0",
                      "HIGH": "#ff7aa0"}[zone]
        self._zone_lbl.setText(zone)
        self._zone_lbl.setStyleSheet(
            f"font-family: 'Consolas'; font-size: 12px; "
            f"font-weight: bold; color: {zone_color};")
        # 解説
        self._expl_lbl.setText(explanation)

    def _apply_opacity(self, widget, opacity):
        eff = widget.graphicsEffect()
        if not isinstance(eff, QtWidgets.QGraphicsOpacityEffect):
            eff = QtWidgets.QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(eff)
        eff.setOpacity(opacity)


class _BigControlPanel(QtWidgets.QDialog):
    """ヘッダクリックで開く大型操作パネル.
    モード切替 / Audio / REC / Volume / Spectrum を 1.8倍 サイズで表示."""

    def __init__(self, main_window):
        super().__init__(main_window)
        self.setWindowFlags(QtCore.Qt.Dialog | QtCore.Qt.FramelessWindowHint
                            | QtCore.Qt.WindowStaysOnTopHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, False)
        self._mw = main_window
        self.setFixedSize(720, 180)
        accent = main_window.theme.accent

        # 中央
        scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.move(scr.center().x() - self.width() // 2,
                  scr.center().y() - self.height() // 2)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)

        # row 1: モード 3 ボタン
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(10)
        self._mode_btns = []
        for key, label in [("studio", "🧠 STUDIO"),
                           ("listen", "🎚 LISTEN"),
                           ("watch", "🌊 WATCH")]:
            b = QtWidgets.QPushButton(label)
            b.setFixedHeight(46)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.clicked.connect(lambda _, k=key: self._mw._set_mode(k))
            mode_row.addWidget(b)
            self._mode_btns.append(b)
        outer.addLayout(mode_row)

        # row 2: Audio + REC + Replay + Volume + Close
        action_row = QtWidgets.QHBoxLayout()
        action_row.setSpacing(10)

        self.audio_btn = QtWidgets.QPushButton("♪ Audio")
        self.audio_btn.setFixedHeight(40)
        self.audio_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.audio_btn.clicked.connect(self._mw._toggle_audio)
        action_row.addWidget(self.audio_btn)

        self.rec_btn = QtWidgets.QPushButton("● REC")
        self.rec_btn.setFixedHeight(40)
        self.rec_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.rec_btn.clicked.connect(self._mw._toggle_recording)
        action_row.addWidget(self.rec_btn)

        vol_lbl = QtWidgets.QLabel("🔊")
        vol_lbl.setStyleSheet("font-size: 18px; color: #c0c0c0;")
        action_row.addWidget(vol_lbl)
        self.volume = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.volume.setRange(0, 150)
        self.volume.setValue(main_window.volume_slider.value()
                              if hasattr(main_window, "volume_slider")
                              else 100)
        self.volume.setFixedHeight(28)
        self.volume.valueChanged.connect(self._on_volume)
        action_row.addWidget(self.volume, 1)
        self.vol_val = QtWidgets.QLabel("100%")
        self.vol_val.setStyleSheet(
            "color: #f0f0f0; font-family: 'Consolas', monospace; "
            "font-size: 14px; min-width: 50px;")
        action_row.addWidget(self.vol_val)

        close_btn = QtWidgets.QPushButton("✕")
        close_btn.setFixedSize(40, 40)
        close_btn.setCursor(QtCore.Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        action_row.addWidget(close_btn)
        outer.addLayout(action_row)

        # スタイル
        ar = QtGui.QColor(accent).red()
        ag = QtGui.QColor(accent).green()
        ab = QtGui.QColor(accent).blue()
        self.setStyleSheet(
            f"_BigControlPanel {{ background-color: #0a0a10; "
            f"border: 2px solid {accent}; border-radius: 14px; }}"
            "QPushButton { background-color: #1e1e22; color: #d0d0d0; "
            f"border: 1px solid {accent}; border-radius: 10px; "
            "font-size: 14px; font-weight: bold; "
            "letter-spacing: 1.5px; padding: 4px 16px; }"
            f"QPushButton:hover {{ background-color: {accent}; "
            "color: #000000; }"
            "QSlider::groove:horizontal { background: #2a2a2c; "
            "height: 6px; border-radius: 3px; }"
            f"QSlider::sub-page:horizontal {{ background: {accent}; "
            "border-radius: 3px; }}"
            f"QSlider::handle:horizontal {{ background: {accent}; "
            "width: 18px; border-radius: 9px; "
            "margin-top: -6px; margin-bottom: -6px; }}"
        )
        # Esc で閉じる
        QtWidgets.QShortcut(QtGui.QKeySequence("Esc"), self,
                            activated=self.close)

    def _on_volume(self, val):
        self.vol_val.setText(f"{int(val)}%")
        if hasattr(self._mw, "volume_slider"):
            # 元のスライダにも反映 (signal で audio engine も更新)
            self._mw.volume_slider.setValue(val)


class SplashScreen(QtWidgets.QWidget):
    """起動時スプラッシュ. 回路パターン + neon タイトル + プログレスバー."""

    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.SplashScreen
                          | QtCore.Qt.FramelessWindowHint
                          | QtCore.Qt.WindowStaysOnTopHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, False)
        self.setFixedSize(720, 380)
        # 中央寄せ
        scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.move(scr.center().x() - self.width() // 2,
                  scr.center().y() - self.height() // 2)
        self._progress = 0.0
        self._circuit = None
        circuit_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets", "bg", "bg_circuit.png")
        if os.path.exists(circuit_path):
            self._circuit = QtGui.QImage(circuit_path)
        # ロード文言
        self._status_text = "Initializing..."
        # 自動進行タイマ
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

    def set_progress(self, p, text=None):
        self._progress = max(0.0, min(1.0, float(p)))
        if text is not None:
            self._status_text = text
        self.update()

    def _tick(self):
        # 自動で 0→1 (約 1.6 秒)
        if self._progress < 1.0:
            self._progress = min(1.0, self._progress + 0.025)
            self.update()

    def paintEvent(self, event):
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        accent = QtGui.QColor("#1abc9c")
        # 背景 (暗い)
        qp.fillRect(self.rect(), QtGui.QColor(8, 8, 14))
        # 回路パターン (あれば被せる、暗めに)
        if self._circuit is not None and not self._circuit.isNull():
            qp.setOpacity(0.55)
            ar = self._circuit.width() / max(1, self._circuit.height())
            dst_r = w / max(1, h)
            if ar > dst_r:
                dh = h
                dw = int(dh * ar)
                x = (w - dw) // 2
                y = 0
            else:
                dw = w
                dh = int(dw / ar)
                x = 0
                y = (h - dh) // 2
            qp.drawImage(QtCore.QRectF(x, y, dw, dh), self._circuit)
            qp.setOpacity(1.0)
            # 中央暗ぼかし
            grad = QtGui.QRadialGradient(QtCore.QPointF(w / 2, h / 2),
                                          w * 0.6)
            grad.setColorAt(0.0, QtGui.QColor(0, 0, 0, 130))
            grad.setColorAt(1.0, QtGui.QColor(0, 0, 0, 220))
            qp.fillRect(self.rect(), grad)
        # 外周 neon border
        border_pen = QtGui.QPen(accent, 2)
        qp.setPen(border_pen)
        qp.setBrush(QtCore.Qt.NoBrush)
        qp.drawRoundedRect(QtCore.QRectF(1, 1, w - 2, h - 2), 12, 12)

        # ロゴ (絵文字 + タイトル)
        emoji_font = QtGui.QFont()
        emoji_font.setPointSize(56)
        qp.setFont(emoji_font)
        qp.setPen(QtGui.QColor(240, 240, 240))
        qp.drawText(QtCore.QRectF(0, 60, w, 80),
                    QtCore.Qt.AlignCenter, "🧠")
        # タイトル
        title_font = QtGui.QFont()
        title_font.setPointSize(28)
        title_font.setBold(True)
        title_font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 4)
        qp.setFont(title_font)
        qp.setPen(accent)
        qp.drawText(QtCore.QRectF(0, 150, w, 50),
                    QtCore.Qt.AlignCenter, "EEG ADAPTIVE EQ")
        # サブタイトル
        sub_font = QtGui.QFont()
        sub_font.setPointSize(11)
        sub_font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 2)
        qp.setFont(sub_font)
        qp.setPen(QtGui.QColor(180, 180, 180))
        qp.drawText(QtCore.QRectF(0, 200, w, 24),
                    QtCore.Qt.AlignCenter,
                    "Vital Sensing  ×  Affective Computing  ×  Audio")

        # プログレスバー
        bar_w = int(w * 0.55)
        bar_h = 6
        bar_x = (w - bar_w) // 2
        bar_y = h - 70
        qp.setPen(QtCore.Qt.NoPen)
        qp.setBrush(QtGui.QColor(255, 255, 255, 30))
        qp.drawRoundedRect(QtCore.QRectF(bar_x, bar_y, bar_w, bar_h),
                            3, 3)
        fill_w = int(bar_w * self._progress)
        if fill_w > 1:
            grad = QtGui.QLinearGradient(bar_x, 0, bar_x + bar_w, 0)
            grad.setColorAt(0.0, QtGui.QColor(accent.red(), accent.green(),
                                               accent.blue(), 230))
            grad.setColorAt(1.0, QtGui.QColor(255, 255, 255, 240))
            qp.setBrush(grad)
            qp.drawRoundedRect(QtCore.QRectF(bar_x, bar_y, fill_w, bar_h),
                                3, 3)
        # ステータステキスト
        status_font = QtGui.QFont("Consolas")
        status_font.setPointSize(9)
        status_font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 1)
        qp.setFont(status_font)
        qp.setPen(QtGui.QColor(160, 160, 160))
        qp.drawText(QtCore.QRectF(bar_x, bar_y + 10, bar_w, 16),
                    QtCore.Qt.AlignLeft, self._status_text)
        qp.drawText(QtCore.QRectF(bar_x, bar_y + 10, bar_w, 16),
                    QtCore.Qt.AlignRight,
                    f"{int(self._progress * 100):3d}%")
        qp.end()


class _IdleScreensaver(QtWidgets.QWidget):
    """アイドル時に表示する全画面オーバーレイ. 脈動 brain + 時計."""

    closed = QtCore.pyqtSignal()

    def __init__(self, parent=None, accent="#1abc9c"):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setStyleSheet("background-color: rgba(2, 2, 8, 245);")
        self._accent = QtGui.QColor(accent)
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)
        self.setMouseTracking(True)

    def set_accent(self, hex_color):
        self._accent = QtGui.QColor(hex_color)

    def _tick(self):
        import math as _m
        self._phase = (self._phase + 0.05) % (2 * _m.pi)
        self.update()

    def paintEvent(self, event):
        import math as _m
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        cx, cy = w / 2, h / 2
        a = self._accent
        pulse = 0.5 + 0.5 * _m.sin(self._phase)

        # 多層グロー
        for mult, alpha in [(3.0, 18), (2.0, 40), (1.2, 100)]:
            r = 100 * mult * (0.85 + 0.15 * pulse)
            grad = QtGui.QRadialGradient(QtCore.QPointF(cx, cy - 40), r)
            grad.setColorAt(0.0, QtGui.QColor(a.red(), a.green(),
                                               a.blue(),
                                               int(alpha * pulse)))
            grad.setColorAt(1.0, QtGui.QColor(a.red(), a.green(),
                                               a.blue(), 0))
            qp.setPen(QtCore.Qt.NoPen)
            qp.setBrush(grad)
            qp.drawEllipse(QtCore.QPointF(cx, cy - 40), r, r)

        # 脳絵文字 (中央)
        emo_font = QtGui.QFont()
        emo_font.setPointSize(80)
        qp.setFont(emo_font)
        qp.setPen(QtGui.QColor(255, 255, 255, 240))
        qp.drawText(QtCore.QRectF(0, cy - 110, w, 140),
                    QtCore.Qt.AlignCenter, "🧠")

        # 時計
        clock_font = QtGui.QFont("Consolas")
        clock_font.setPointSize(48)
        clock_font.setBold(True)
        qp.setFont(clock_font)
        qp.setPen(a)
        qp.drawText(QtCore.QRectF(0, cy + 50, w, 70),
                    QtCore.Qt.AlignCenter, time.strftime("%H:%M"))

        # サブ
        sub_font = QtGui.QFont()
        sub_font.setPointSize(11)
        sub_font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 3)
        qp.setFont(sub_font)
        qp.setPen(QtGui.QColor(160, 160, 160))
        qp.drawText(QtCore.QRectF(0, cy + 130, w, 24),
                    QtCore.Qt.AlignCenter,
                    "EEG ADAPTIVE EQ  ·  IDLE")
        qp.drawText(QtCore.QRectF(0, h - 40, w, 20),
                    QtCore.Qt.AlignCenter,
                    "Press any key or move mouse to resume")
        qp.end()

    def mousePressEvent(self, event):
        self.closed.emit()
        self.deleteLater()

    def keyPressEvent(self, event):
        self.closed.emit()
        self.deleteLater()


class _PreviewComboBox(QtWidgets.QComboBox):
    """ドロップダウンの項目を hover した瞬間にプレビュー反映,
    選択せずに閉じたら元に戻すコンボボックス."""

    def __init__(self, on_preview, on_commit, get_current, parent=None):
        super().__init__(parent)
        self._on_preview = on_preview
        self._on_commit = on_commit
        self._get_current = get_current
        self._saved = None
        self._committed = False
        self.highlighted[str].connect(self._on_highlighted)
        self.activated[str].connect(self._on_activated)

    def showPopup(self):
        self._saved = self._get_current()
        self._committed = False
        super().showPopup()

    def hidePopup(self):
        super().hidePopup()
        if not self._committed and self._saved is not None:
            # 選択せずに閉じた場合、元のテーマに戻す
            try:
                self._on_preview(self._saved)
            except Exception:
                pass
        self._saved = None

    def _on_highlighted(self, name):
        try:
            self._on_preview(name)
        except Exception:
            pass

    def _on_activated(self, name):
        self._committed = True
        try:
            self._on_commit(name)
        except Exception:
            pass


class _CursorParticles(QtWidgets.QWidget):
    """カーソル位置にネオン粒子を撒く軽量オーバーレイ.
    Watch モード専用. setMouseTracking で常時マウス追従.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # 透過 + マウスイベントは通過 (背景の widget が普通に使えるよう)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self._accent = QtGui.QColor(120, 230, 255)
        self._particles = []   # [x, y, vx, vy, life, max_life, size]
        self._last_mouse = QtCore.QPoint(0, 0)
        self._last_spawn = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)   # 20fps
        # マウス位置監視: 親 (watch_page) で eventFilter
        if parent is not None:
            parent.setMouseTracking(True)
            parent.installEventFilter(self)

    def set_accent(self, hex_color):
        self._accent = QtGui.QColor(hex_color)

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.MouseMove:
            self._last_mouse = event.pos()
            self._maybe_spawn()
        return False

    def _maybe_spawn(self):
        import random as _r
        now = time.time()
        if (now - self._last_spawn) < 0.04:
            return
        self._last_spawn = now
        # 3 個 spawn
        for _ in range(3):
            self._particles.append([
                float(self._last_mouse.x() + _r.uniform(-4, 4)),
                float(self._last_mouse.y() + _r.uniform(-4, 4)),
                _r.uniform(-30, 30),       # vx
                _r.uniform(-30, 30),       # vy
                0.0,
                0.8 + _r.random() * 0.6,
                2.0 + _r.random() * 1.5,
            ])
        if len(self._particles) > 80:
            self._particles = self._particles[-80:]

    def _tick(self):
        if not self.isVisible():
            return
        now = time.time()
        dt = 0.05
        alive = []
        for p in self._particles:
            p[4] += dt
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            # 摩擦
            p[2] *= 0.92
            p[3] *= 0.92
            if p[4] < p[5]:
                alive.append(p)
        self._particles = alive
        self.update()

    def paintEvent(self, event):
        if not self._particles:
            return
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        qp.setPen(QtCore.Qt.NoPen)
        a = self._accent
        for x, y, vx, vy, life, max_life, size in self._particles:
            t = life / max_life
            alpha = int(220 * (1.0 - t) ** 1.5)
            if alpha < 6:
                continue
            r = size * (1.0 + 0.5 * (1.0 - t))
            qp.setBrush(QtGui.QColor(a.red(), a.green(), a.blue(), alpha))
            qp.drawEllipse(QtCore.QPointF(x, y), r, r)
        qp.end()


class _MatrixRain(QtWidgets.QWidget):
    """Matrix 風 binary rain. Watch モードの背景装飾."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self._accent = QtGui.QColor(120, 230, 255)
        self._opacity = 0.18   # うるさかったので減らす
        self._columns = []   # list of (x, head_y, speed, length, chars)
        self._w = 0
        self._h = 0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(100)   # 10fps 軽量化
        self._last_tick = time.time()
        self._initialized = False

    def set_accent(self, hex_color):
        self._accent = QtGui.QColor(hex_color)

    def _init_columns(self):
        import random as _r
        self._columns = []
        col_w = 28   # 列幅を 14→28 で密度半減
        for x in range(0, self._w, col_w):
            length = _r.randint(4, 12)   # 長さも短く
            chars = [_r.choice("01") for _ in range(length)]
            self._columns.append({
                "x": x,
                "y": _r.uniform(-self._h, 0),
                "speed": 40 + _r.uniform(0, 80),
                "length": length,
                "chars": chars,
            })
        self._initialized = True

    def resizeEvent(self, event):
        if self.width() != self._w or self.height() != self._h:
            self._w, self._h = self.width(), self.height()
            self._init_columns()
        super().resizeEvent(event)

    def _tick(self):
        if not self.isVisible():
            return
        import random as _r
        if not self._initialized and self._w > 0:
            self._init_columns()
        now = time.time()
        dt = max(0.001, now - self._last_tick)
        self._last_tick = now
        for col in self._columns:
            col["y"] += col["speed"] * dt
            # 画面外に出たらリセット
            if col["y"] - col["length"] * 14 > self._h:
                col["y"] = -_r.uniform(50, 200)
                col["chars"] = [_r.choice("01") for _ in range(col["length"])]
        self.update()

    def paintEvent(self, event):
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, False)
        font = QtGui.QFont("Consolas", 10, QtGui.QFont.Bold)
        qp.setFont(font)
        a = self._accent
        for col in self._columns:
            x = col["x"]
            y = col["y"]
            length = col["length"]
            for i, ch in enumerate(col["chars"]):
                cy = y - i * 14
                if cy < -14 or cy > self._h + 14:
                    continue
                # 先頭 (i=0) が一番明るく、下に行くほど薄く
                t = i / max(1, length - 1)
                if i == 0:
                    qp.setPen(QtGui.QColor(255, 255, 255,
                                            int(220 * self._opacity)))
                else:
                    alpha = int((1.0 - t) * 200 * self._opacity)
                    qp.setPen(QtGui.QColor(a.red(), a.green(), a.blue(),
                                            alpha))
                qp.drawText(int(x), int(cy), ch)
        qp.end()


class _TronGrid(QtWidgets.QWidget):
    """Tron 風遠近 wireframe グリッド床. Watch モードの背景装飾."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self._accent = QtGui.QColor(120, 230, 255)
        self._opacity = 0.45
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def set_accent(self, hex_color):
        self._accent = QtGui.QColor(hex_color)

    def _tick(self):
        if not self.isVisible():
            return
        self._phase = (self._phase + 0.5) % 40.0
        self.update()

    def paintEvent(self, event):
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        # 下半分にパースペクティブグリッド
        horizon_y = int(h * 0.55)
        bottom_y = h
        if bottom_y - horizon_y < 40:
            qp.end()
            return
        cx = w / 2
        a = self._accent
        ar, ag, ab = a.red(), a.green(), a.blue()

        # 縦線 (放射状) - 中央から扇形に広がる
        n_v = 16
        for i in range(-n_v, n_v + 1):
            if i == 0:
                continue
            # 遠点 (中心)
            x_top = cx
            # 近点 (画面下端) — 距離 i * 80px
            x_bot = cx + i * 80
            alpha = int(140 * self._opacity * (1.0 - abs(i) / n_v * 0.5))
            qp.setPen(QtGui.QPen(QtGui.QColor(ar, ag, ab, alpha), 1.0))
            qp.drawLine(QtCore.QPointF(x_top, horizon_y),
                        QtCore.QPointF(x_bot, bottom_y))

        # 横線 (遠近) - 下から上へ等比で密度上がる
        rows = 14
        for i in range(rows):
            # i=0 が画面下, i=rows-1 が水平線近く
            t = i / (rows - 1)
            # 遠近圧縮: 上に行くほど近接
            y = bottom_y - (1 - t ** 2.2) * (bottom_y - horizon_y)
            alpha = int(160 * self._opacity * (1.0 - t * 0.8))
            qp.setPen(QtGui.QPen(QtGui.QColor(ar, ag, ab, alpha), 1.0))
            qp.drawLine(QtCore.QPointF(0, y), QtCore.QPointF(w, y))

        # 水平線 (明るく)
        qp.setPen(QtGui.QPen(QtGui.QColor(ar, ag, ab,
                                          int(220 * self._opacity)), 1.5))
        qp.drawLine(QtCore.QPointF(0, horizon_y),
                    QtCore.QPointF(w, horizon_y))
        qp.end()


def _fibonacci_sphere(n):
    """球面上に N 点を均等配置 (Fibonacci spiral)."""
    import math as _m
    pts = []
    phi = _m.pi * (3.0 - _m.sqrt(5.0))
    for i in range(n):
        y = 1.0 - (i / max(1, n - 1)) * 2.0
        rad = _m.sqrt(max(0.0, 1.0 - y * y))
        theta = phi * i
        x = _m.cos(theta) * rad
        z = _m.sin(theta) * rad
        pts.append((x, y, z))
    return pts


class _MandalaOverlay(QtWidgets.QWidget):
    """Watch モードの中央: 神経網状オーブ + EQ 放射状ライン.
    Fibonacci球面上の点群を投影 + 近接接続線で描画.
    pulse_bpm で中心の発光が拍動.
    """

    EQ_BANDS = [
        # (key, label, emoji)
        ("drums", "Drums", "🥁"),
        ("bass",  "Bass",  "🎸"),
        ("mid",   "Mid",   "🎹"),
        ("vocal", "Vocals", "🎤"),
        ("high",  "Treble", "🎺"),
        ("air",   "Air",   "🌟"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self._yaw = 0.0
        self._pitch = 0.0
        self._pulse_phase = 0.0
        self._pulse_bpm = 0.0
        self._accent = QtGui.QColor(220, 240, 255)
        self._opacity = 0.55
        self._eq_vals = {}
        self._eq_gain_max = 6.0
        # δθαβγ バンド弧の表示フラグ (Underwater は心拍駆動なので False)
        self._show_band_arc = True
        # 球面点群 (Fibonacci, 約120点で軽め)
        self._n_points = 120
        self._pts_3d = _fibonacci_sphere(self._n_points)
        self._neighbor_dist = 0.55   # 接続距離閾値 (正規化球座標)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)   # 20fps (軽量化)
        self._last_tick = time.time()

    def set_eq_values(self, eq_dict, gain_max=6.0):
        self._eq_vals = dict(eq_dict)
        self._eq_gain_max = gain_max

    def set_accent(self, hex_color):
        self._accent = QtGui.QColor(hex_color)
        self.update()

    def set_bpm(self, bpm):
        self._pulse_bpm = float(bpm) if (bpm and bpm > 20) else 0.0

    def set_show_band_arc(self, show):
        """δθαβγ の弧ラベルを表示するか. Underwater は心拍駆動なので False."""
        s = bool(show)
        if s != self._show_band_arc:
            self._show_band_arc = s
            self.update()

    def _tick(self):
        if not self.isVisible():
            return
        import math as _m
        now = time.time()
        dt = now - self._last_tick
        self._last_tick = now
        # オーブを少しだけ回転 (yaw 主体、pitch はうねり)
        self._yaw = (self._yaw + dt * 0.18) % (2 * _m.pi)
        self._pitch = 0.25 * _m.sin(now * 0.15)
        if self._pulse_bpm > 0:
            self._pulse_phase += 2 * _m.pi * (self._pulse_bpm / 60.0) * dt
        else:
            self._pulse_phase += 2 * _m.pi * 0.4 * dt
        self.update()

    def _rotate(self, pt):
        """3D 点を yaw + pitch で回転して返す."""
        import math as _m
        x, y, z = pt
        cy_, sy_ = _m.cos(self._yaw), _m.sin(self._yaw)
        # yaw (Y軸)
        x, z = cy_ * x + sy_ * z, -sy_ * x + cy_ * z
        # pitch (X軸)
        cp, sp = _m.cos(self._pitch), _m.sin(self._pitch)
        y, z = cp * y - sp * z, sp * y + cp * z
        return (x, y, z)

    def paintEvent(self, event):
        import math as _m
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        cx, cy = w / 2, h / 2
        R = min(w, h) * 0.28
        if R < 30:
            qp.end()
            return

        a_base = self._accent
        ar, ag, ab = a_base.red(), a_base.green(), a_base.blue()

        # 中心の発光 (脈動)
        pulse = 0.4 + 0.6 * (0.5 + 0.5 * _m.sin(self._pulse_phase))

        # ===== 外側多層グロー =====
        for mult, alpha in [(2.0, 28), (1.4, 60), (1.05, 120)]:
            r = R * mult * (0.88 + 0.12 * pulse)
            grad = QtGui.QRadialGradient(QtCore.QPointF(cx, cy), r)
            c0 = QtGui.QColor(ar, ag, ab,
                              int(alpha * self._opacity * pulse))
            c1 = QtGui.QColor(ar, ag, ab, 0)
            grad.setColorAt(0.0, c0)
            grad.setColorAt(1.0, c1)
            qp.setPen(QtCore.Qt.NoPen)
            qp.setBrush(grad)
            qp.drawEllipse(QtCore.QPointF(cx, cy), r, r)

        # ===== 外周リング (mockup の白い輪) =====
        ring_pen = QtGui.QPen(QtGui.QColor(ar, ag, ab,
                              int(240 * self._opacity)), 2.0)
        qp.setPen(ring_pen)
        qp.setBrush(QtCore.Qt.NoBrush)
        qp.drawEllipse(QtCore.QPointF(cx, cy), R * 1.05, R * 1.05)
        # 細い 2 重リング
        ring_pen2 = QtGui.QPen(QtGui.QColor(ar, ag, ab,
                               int(80 * self._opacity)), 1.0)
        qp.setPen(ring_pen2)
        qp.drawEllipse(QtCore.QPointF(cx, cy), R * 1.18, R * 1.18)

        # ===== 神経網状オーブ (3D点群) =====
        # まず全点を回転 → 2D 投影 + 深度
        projected = []
        for p in self._pts_3d:
            x3, y3, z3 = self._rotate(p)
            px = cx + x3 * R
            py = cy + y3 * R
            projected.append((px, py, z3))   # z3: 前=+1 後=-1

        # 接続線 (近接ペア)
        threshold = self._neighbor_dist
        line_alpha_max = 90
        for i in range(len(projected)):
            x1, y1, z1 = projected[i]
            for j in range(i + 1, len(projected)):
                x2, y2, z2 = projected[j]
                dx = x1 - x2
                dy = y1 - y2
                d = (dx * dx + dy * dy) ** 0.5
                if d > R * threshold:
                    continue
                # 深度平均で alpha (前=濃く, 後=薄く)
                avg_z = (z1 + z2) * 0.5
                depth_factor = (avg_z + 1.0) * 0.5   # 0..1
                a = int(line_alpha_max * self._opacity
                        * depth_factor * (1.0 - d / (R * threshold)))
                if a < 6:
                    continue
                qp.setPen(QtGui.QPen(
                    QtGui.QColor(ar, ag, ab, a), 0.8))
                qp.drawLine(QtCore.QPointF(x1, y1),
                            QtCore.QPointF(x2, y2))

        # 渦巻きパーティクル (球面点に + 渦回転オフセットを加味して 銀河風)
        # マゼンタとシアンの 2色トーンを混ぜる
        magenta = QtGui.QColor(255, 80, 200)
        mr, mg, mb = magenta.red(), magenta.green(), magenta.blue()
        for idx, (px, py, z3) in enumerate(projected):
            depth_factor = (z3 + 1.0) * 0.5
            size = 1.2 + depth_factor * 2.3
            a_alpha = int(200 * self._opacity * (0.4 + 0.6 * depth_factor))
            # アクセント色 ⇔ マゼンタ を idx で交互ミックス → 渦の二色感
            mix_t = (idx * 0.137) % 1.0
            r_c = int(ar * (1 - mix_t) + mr * mix_t)
            g_c = int(ag * (1 - mix_t) + mg * mix_t)
            b_c = int(ab * (1 - mix_t) + mb * mix_t)
            # 前面ほど明色寄り
            r_c = int(r_c + (255 - r_c) * depth_factor * 0.6)
            g_c = int(g_c + (255 - g_c) * depth_factor * 0.6)
            b_c = int(b_c + (255 - b_c) * depth_factor * 0.6)
            qp.setPen(QtCore.Qt.NoPen)
            qp.setBrush(QtGui.QColor(r_c, g_c, b_c, a_alpha))
            qp.drawEllipse(QtCore.QPointF(px, py), size, size)

        # 中心の輝点 (脈動 + 強い発光)
        core_r = 10 + 6 * pulse
        # 多層発光 (外側ハロー→内側コア)
        for mult, alpha_base in [(4.0, 60), (2.5, 130), (1.5, 200), (1.0, 255)]:
            rr = core_r * mult
            grad = QtGui.QRadialGradient(QtCore.QPointF(cx, cy), rr)
            grad.setColorAt(0.0, QtGui.QColor(255, 255, 255,
                                               int(alpha_base * pulse)))
            grad.setColorAt(0.4, QtGui.QColor(ar, ag, ab,
                                               int(alpha_base * 0.6 * pulse)))
            grad.setColorAt(1.0, QtGui.QColor(ar, ag, ab, 0))
            qp.setPen(QtCore.Qt.NoPen)
            qp.setBrush(grad)
            qp.drawEllipse(QtCore.QPointF(cx, cy), rr, rr)

        # ===== EQ 放射状ライン (右側) =====
        if self._eq_vals:
            self._paint_eq_spokes(qp, cx, cy, R, ar, ag, ab)

        # ===== EEG バンドラベル弧 (左側) — Surface でのみ表示 =====
        if self._show_band_arc:
            self._paint_band_arc(qp, cx, cy, R, ar, ag, ab)

        qp.end()

    def _paint_band_arc(self, qp, cx, cy, R, ar, ag, ab):
        """δ θ α β γ をオーブ左側に弧状配置. mockup4 風."""
        import math as _m
        labels = [("δ", "#b07cff"), ("θ", "#5fc7ff"),
                  ("α", "#5fffaa"), ("β", "#ffcf4d"),
                  ("γ", "#ff7a9c")]
        n = len(labels)
        start_deg = 235
        end_deg = 125
        # オーブ + EQ ハブと十分離す
        arc_radius = R * 1.35
        label_radius = R * 1.55   # ピル中心位置

        # 弧線 (背景)
        arc_pen = QtGui.QPen(QtGui.QColor(ar, ag, ab,
                              int(120 * self._opacity)), 1.2)
        qp.setPen(arc_pen)
        qp.setBrush(QtCore.Qt.NoBrush)
        rect = QtCore.QRectF(cx - arc_radius, cy - arc_radius,
                              arc_radius * 2, arc_radius * 2)
        qp.drawArc(rect, int(-235 * 16),
                    int(-(360 - 235 + 125) * 16))

        for i, (label, col_hex) in enumerate(labels):
            t = i / (n - 1)
            sweep = (360 - start_deg + end_deg) % 360
            deg = (start_deg - t * sweep) % 360
            rad = _m.radians(deg)
            # 弧上のドット
            dx = cx + arc_radius * _m.cos(rad)
            dy = cy + arc_radius * _m.sin(rad)
            col = QtGui.QColor(col_hex)
            qp.setBrush(col)
            qp.setPen(QtCore.Qt.NoPen)
            qp.drawEllipse(QtCore.QPointF(dx, dy), 4.0, 4.0)

            # ピル背景 + ラベル (読みやすく大型化)
            lx = cx + label_radius * _m.cos(rad)
            ly = cy + label_radius * _m.sin(rad)
            pill_w, pill_h = 50, 38
            pill_rect = QtCore.QRectF(lx - pill_w / 2, ly - pill_h / 2,
                                       pill_w, pill_h)
            # 不透明な暗背景でコントラスト確保
            qp.setBrush(QtGui.QColor(4, 4, 10, 240))
            border_pen = QtGui.QPen(QtGui.QColor(col.red(), col.green(),
                                     col.blue(), 255), 2.0)
            qp.setPen(border_pen)
            qp.drawRoundedRect(pill_rect, pill_h / 2, pill_h / 2)
            # 文字 (大きく + ハイコントラスト)
            font = QtGui.QFont()
            font.setPointSize(22)
            font.setBold(True)
            qp.setFont(font)
            qp.setPen(QtGui.QColor(col.red(), col.green(), col.blue(), 255))
            qp.drawText(pill_rect, QtCore.Qt.AlignCenter, label)

    def _paint_eq_spokes(self, qp, cx, cy, R, ar, ag, ab):
        """中心オーブから 6 楽器ラベルへ放射状ライン + 六角形ラベル."""
        import math as _m
        n = len(self.EQ_BANDS)
        start_deg = -55
        end_deg = 55
        rad_inner = R * 1.35
        rad_outer = min(self.width(), self.height()) * 0.45
        for i, (key, label, emoji) in enumerate(self.EQ_BANDS):
            t = i / (n - 1)
            deg = start_deg + (end_deg - start_deg) * t
            rad = _m.radians(deg)
            x1 = cx + rad_inner * _m.cos(rad)
            y1 = cy + rad_inner * _m.sin(rad)
            x2 = cx + rad_outer * _m.cos(rad)
            y2 = cy + rad_outer * _m.sin(rad)
            v = self._eq_vals.get(key, 0.0)
            v_norm = max(-1.0, min(1.0, v / max(0.1, self._eq_gain_max)))
            intensity = abs(v_norm)
            alpha = int(70 + 150 * intensity)
            line_w = 1.2 + intensity * 2.0
            pen = QtGui.QPen(QtGui.QColor(ar, ag, ab,
                              int(alpha * self._opacity * 1.5)), line_w)
            qp.setPen(pen)
            qp.drawLine(QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2))

            # === 六角形ラベル ===
            hex_w = 86
            hex_h = 44
            # ラベル中心位置 (線の終点から少し右にオフセット)
            label_cx = x2 + (hex_w / 2 + 6) * _m.cos(rad)
            label_cy = y2 + (hex_h / 2 + 6) * _m.sin(rad) if rad > 0 else y2

            # 中心を x2 から少しずらした位置に置く
            hcx = x2 + 14 + hex_w / 2
            hcy = y2
            # 六角形パス (横長 = pointy-side-vertical の標準六角)
            # 6頂点: 左右の頂点 + 上下2点ずつ
            hex_pts = []
            for k in range(6):
                ang = _m.pi / 3 * k
                hex_pts.append(QtCore.QPointF(
                    hcx + (hex_w / 2) * _m.cos(ang),
                    hcy + (hex_h / 2) * _m.sin(ang)))
            poly = QtGui.QPolygonF(hex_pts)
            # 背景塗り (不透明寄りで読みやすく)
            qp.setBrush(QtGui.QColor(4, 4, 10, 235))
            border_pen = QtGui.QPen(QtGui.QColor(ar, ag, ab, 255),
                                     1.8 + intensity * 0.8)
            qp.setPen(border_pen)
            qp.drawPolygon(poly)
            # ラベルテキスト (大きく)
            font_lbl = QtGui.QFont()
            font_lbl.setPointSize(11)
            font_lbl.setBold(True)
            qp.setFont(font_lbl)
            qp.setPen(QtGui.QColor(255, 255, 255, 255))
            qp.drawText(
                QtCore.QRectF(hcx - hex_w / 2, hcy - hex_h / 2 + 4,
                              hex_w, hex_h / 2),
                QtCore.Qt.AlignCenter, label)
            # 値 (大きく + bright)
            font_v = QtGui.QFont("Consolas")
            font_v.setPointSize(10)
            font_v.setBold(True)
            qp.setFont(font_v)
            sign = "+" if v >= 0 else ""
            qp.setPen(QtGui.QColor(ar, ag, ab, 255))
            qp.drawText(
                QtCore.QRectF(hcx - hex_w / 2, hcy,
                              hex_w, hex_h / 2 - 2),
                QtCore.Qt.AlignCenter, f"{sign}{v:.1f}")


class _BrainWave(QtWidgets.QWidget):
    """1バンドぶんの流れる波. value(0..1) で振幅・流速・濃度が変化."""

    def __init__(self, color_hex, parent=None):
        super().__init__(parent)
        self._color = QtGui.QColor(color_hex)
        self._value = 0.5
        self._phase = 0.0
        self.setMinimumHeight(40)
        self.setMaximumHeight(60)
        self.setMinimumWidth(80)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._timer.start(80)   # 軽量化

    def set_value(self, v):
        v = max(0.0, min(1.0, float(v)))
        if abs(v - self._value) < 0.005:
            return
        self._value = v
        self.update()

    def _advance(self):
        if not self.isVisible():
            return
        import math as _m
        speed = 0.07 + self._value * 0.25
        self._phase = (self._phase + speed) % (2 * _m.pi * 1000)
        self.update()

    def paintEvent(self, event):
        import math as _m
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        cy = h / 2
        amp_max = h * 0.36

        # 3本のサインを重ねる: 異なる周波数・位相・透明度
        for i, (freq, phase_mul, alpha, line_w) in enumerate([
                (0.025, 1.0, 60 + int(self._value * 80), 1.4),
                (0.040, -1.4, 40 + int(self._value * 60), 1.0),
                (0.065, 2.1, 24 + int(self._value * 40), 0.8)]):
            amp = amp_max * (0.3 + 0.7 * self._value) * (0.9 - i * 0.15)
            c = QtGui.QColor(self._color.red(), self._color.green(),
                             self._color.blue(), alpha)
            pen = QtGui.QPen(c, line_w)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            qp.setPen(pen)
            qp.setBrush(QtCore.Qt.NoBrush)
            path = QtGui.QPainterPath()
            x = 0
            path.moveTo(x, cy + amp * _m.sin(
                self._phase * phase_mul + x * freq + i * 1.7))
            step = 4
            while x < w + step:
                x += step
                y = cy + amp * _m.sin(
                    self._phase * phase_mul + x * freq + i * 1.7)
                # 第二倍音
                y += amp * 0.4 * _m.sin(
                    self._phase * phase_mul * 1.7 + x * freq * 2.1)
                path.lineTo(x, y)
            qp.drawPath(path)
        qp.end()


class _RussellPad(QtWidgets.QWidget):
    """Russell Circumplex を綺麗に描く 2D パッド.

    set_position(valence, arousal) : 現在の値 (0..1) を set
    set_trail([(v,a), ...])         : 軌跡 (古い→新しい順) を set
    """

    QUAD_LABELS = [
        # (x, y, emoji, name, color)
        (0.80, 0.85, "😊", "Excited",  "#2ecc71"),
        (0.20, 0.85, "😠", "Stressed", "#e74c3c"),
        (0.20, 0.15, "😔", "Sad",      "#3498db"),
        (0.80, 0.15, "😌", "Calm",     "#f39c12"),
    ]

    def __init__(self, theme=None, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._pos = (0.5, 0.5)
        self._trail = []   # list of (v, a)
        self._pulse_phase = 0.0
        # 軸ラベルの下スペース確保のため、縦方向に少し大きめの最小サイズ
        self.setMinimumSize(240, 260)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(100)   # 軽量化

    def _tick(self):
        if not self.isVisible():
            return
        import math as _m
        self._pulse_phase = (self._pulse_phase + 0.13) % (2 * _m.pi)
        self.update()

    def set_position(self, valence, arousal):
        self._pos = (float(valence), float(arousal))
        self.update()

    def set_trail(self, trail):
        self._trail = list(trail)
        self.update()

    def _xy(self, v, a, rect):
        x = rect.left() + v * rect.width()
        y = rect.bottom() - a * rect.height()
        return QtCore.QPointF(x, y)

    def paintEvent(self, event):
        import math as _m
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        # accent
        accent = QtGui.QColor("#1abc9c")
        text_dim = QtGui.QColor("#8a8a8a")
        if self._theme is not None:
            try:
                accent = QtGui.QColor(self._theme.accent)
                text_dim = QtGui.QColor(self._theme.text_dim)
            except Exception:
                pass

        # 描画領域 (正方形). 軸ラベル & 角ラベル分の余白を多めに確保.
        # 縦は下に 24px (Negative/Positive) + 角ラベル分 16px を引いた範囲で正方形.
        avail_h = self.height() - 26   # 下 26px は軸ラベル用
        avail_w = self.width()
        side = min(avail_w, avail_h)
        pad_top = 18                   # 上 18px は角ラベル飛び出し許容
        x0 = (self.width() - side) / 2
        y0 = pad_top
        rect = QtCore.QRectF(x0 + 16, y0, side - 32, side - 32)

        # 背景塗り
        qp.setPen(QtCore.Qt.NoPen)
        qp.setBrush(QtGui.QColor(15, 15, 20, 230))
        qp.drawRoundedRect(rect, 10, 10)

        # グリッド線 (3x3)
        grid_pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 18), 1)
        qp.setPen(grid_pen)
        for i in range(1, 4):
            x = rect.left() + rect.width() * i / 4
            qp.drawLine(QtCore.QPointF(x, rect.top()),
                        QtCore.QPointF(x, rect.bottom()))
            y = rect.top() + rect.height() * i / 4
            qp.drawLine(QtCore.QPointF(rect.left(), y),
                        QtCore.QPointF(rect.right(), y))

        # 中央クロス (太め)
        cross_pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 55), 1.2,
                               QtCore.Qt.DashLine)
        qp.setPen(cross_pen)
        cx = rect.center().x()
        cy = rect.center().y()
        qp.drawLine(QtCore.QPointF(cx, rect.top()),
                    QtCore.QPointF(cx, rect.bottom()))
        qp.drawLine(QtCore.QPointF(rect.left(), cy),
                    QtCore.QPointF(rect.right(), cy))

        # 外枠
        frame_pen = QtGui.QPen(QtGui.QColor(accent.red(), accent.green(),
                                            accent.blue(), 100), 1)
        qp.setPen(frame_pen)
        qp.setBrush(QtCore.Qt.NoBrush)
        qp.drawRoundedRect(rect, 10, 10)

        # 軸ラベル (Negative/Positive) — 下に十分な高さを確保 (16px)
        ax_font = QtGui.QFont("Consolas")
        ax_font.setPointSize(8)
        ax_font.setBold(True)
        qp.setFont(ax_font)
        qp.setPen(text_dim)
        qp.drawText(QtCore.QRectF(rect.left(), rect.bottom() + 4,
                                  rect.width(), 16),
                    QtCore.Qt.AlignLeft, "← Negative")
        qp.drawText(QtCore.QRectF(rect.left(), rect.bottom() + 4,
                                  rect.width(), 16),
                    QtCore.Qt.AlignRight, "Positive →")
        # Y軸ラベル (縦書き風)
        qp.save()
        qp.translate(rect.left() - 6, rect.center().y())
        qp.rotate(-90)
        qp.drawText(QtCore.QRectF(-rect.height() / 2, -10,
                                  rect.height(), 14),
                    QtCore.Qt.AlignCenter, "Arousal  ↑")
        qp.restore()

        # 4 象限ラベル. 絵文字 ↑ + 名前下 のレイアウト.
        # widget 端でクリップしないよう、ラベル位置を rect 内側にクランプ.
        for (x, y, emo, name, col_hex) in self.QUAD_LABELS:
            color = QtGui.QColor(col_hex)
            pos = self._xy(x, y, rect)
            label_w, label_h = 88, 36   # 文字切れ防止に拡張
            half_w, half_h = label_w / 2, label_h / 2
            # rect の内側に収める (はみ出すと外で切れる)
            lx = max(rect.left() + 2, min(rect.right() - label_w - 2,
                                          pos.x() - half_w))
            ly = max(rect.top() + 2, min(rect.bottom() - label_h - 2,
                                         pos.y() - half_h))
            lbl_rect = QtCore.QRectF(lx, ly, label_w, label_h)
            qp.setPen(QtCore.Qt.NoPen)
            qp.setBrush(QtGui.QColor(0, 0, 0, 140))
            qp.drawRoundedRect(lbl_rect, 7, 7)
            # 絵文字 (上半分)
            emo_font = QtGui.QFont("Segoe UI Emoji", 13)
            qp.setFont(emo_font)
            qp.setPen(color)
            qp.drawText(
                QtCore.QRectF(lbl_rect.left(), lbl_rect.top() + 1,
                              lbl_rect.width(), 18),
                QtCore.Qt.AlignCenter, emo)
            # 名前 (下半分)
            nm_font = QtGui.QFont()
            nm_font.setPointSize(7)
            nm_font.setBold(True)
            qp.setFont(nm_font)
            qp.drawText(
                QtCore.QRectF(lbl_rect.left() + 2, lbl_rect.top() + 19,
                              lbl_rect.width() - 4, 16),
                QtCore.Qt.AlignCenter, name.upper())

        # 軌跡 (古い→新しい順、新しいほど濃く)
        if len(self._trail) >= 2:
            n = len(self._trail)
            for i in range(1, n):
                p1 = self._xy(*self._trail[i - 1], rect)
                p2 = self._xy(*self._trail[i], rect)
                t = i / n
                alpha = int(40 + 160 * t)
                pen = QtGui.QPen(QtGui.QColor(accent.red(), accent.green(),
                                              accent.blue(), alpha), 1.6)
                qp.setPen(pen)
                qp.drawLine(p1, p2)

        # 現在位置 (パルス付き)
        v, a = self._pos
        p = self._xy(v, a, rect)
        pulse = 0.5 + 0.5 * _m.sin(self._pulse_phase)
        # 外側グロー
        for mult, alpha_mul in [(4.0, 0.25), (2.5, 0.5), (1.5, 1.0)]:
            r_glow = 14 * mult
            g = QtGui.QRadialGradient(p, r_glow)
            c0 = QtGui.QColor(accent.red(), accent.green(), accent.blue(),
                              int(80 * alpha_mul * (0.5 + 0.5 * pulse)))
            c1 = QtGui.QColor(accent.red(), accent.green(), accent.blue(), 0)
            g.setColorAt(0.0, c0)
            g.setColorAt(1.0, c1)
            qp.setPen(QtCore.Qt.NoPen)
            qp.setBrush(g)
            qp.drawEllipse(p, r_glow, r_glow)
        # コア
        qp.setBrush(QtGui.QColor(255, 255, 255, 240))
        qp.setPen(QtGui.QPen(accent, 2))
        qp.drawEllipse(p, 6, 6)
        qp.end()


class _ParticleEEG(QtWidgets.QWidget):
    """4ch EEG をパーティクル付きで描画.

    - 各チャンネルは smooth curve (グロー付き) + パーティクル
    - パーティクルは右端 (最新値) に出生 → 左へ流れながらフェード
    - set_data(idx, x, d) で pyqtgraph curve と互換 API
    """

    def __init__(self, channel_names, channel_colors,
                 window_sec, buf_len, parent=None):
        super().__init__(parent)
        self.ch_names = list(channel_names)
        self.ch_colors = [QtGui.QColor(c) for c in channel_colors]
        self.n_ch = len(channel_names)
        self.window_sec = window_sec
        self.buf_len = buf_len
        self._data = [np.zeros(buf_len, dtype=np.float32)
                       for _ in range(self.n_ch)]
        self._particles = [[] for _ in range(self.n_ch)]
        self._last_tick = time.time()
        self._max_particles = 60   # per channel (軽量化)
        self.setMinimumHeight(220)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)   # 20fps (軽量化)

    def set_data(self, i, x, d):
        if 0 <= i < self.n_ch:
            self._data[i] = d

    def _channel_rect(self, ch_idx):
        h = self.height()
        ch_h = h / self.n_ch
        return QtCore.QRectF(0, ch_idx * ch_h, self.width(), ch_h)

    def _val_to_y(self, val, ch_rect):
        y_center = ch_rect.top() + ch_rect.height() / 2
        y_amp = ch_rect.height() * 0.40
        # EEG bandpass 1-40Hz 後の典型レンジ ±60 µV
        return y_center - max(-80, min(80, float(val))) / 80.0 * y_amp

    def _tick(self):
        if not self.isVisible():
            return
        import random as _r
        now = time.time()
        dt = max(0.001, now - self._last_tick)
        self._last_tick = now
        w = self.width()
        for ch_idx in range(self.n_ch):
            data = self._data[ch_idx]
            if len(data) < 2:
                continue
            ch_rect = self._channel_rect(ch_idx)
            # 新規パーティクル: 1個/tick だけ spawn
            val = float(data[-1]) if np.isfinite(data[-1]) else 0.0
            y_spawn = self._val_to_y(val, ch_rect)
            self._particles[ch_idx].append([
                w - 8 + _r.uniform(-3, 3),                # x
                y_spawn + _r.uniform(-3, 3),              # y
                -(30 + _r.uniform(0, 20)),                # vx (leftward)
                _r.uniform(-10, 10),                       # vy
                0.0,                                       # life
                1.2 + _r.random() * 1.0,                  # max_life (短く)
            ])
            # 既存パーティクル更新
            alive = []
            for p in self._particles[ch_idx]:
                p[4] += dt
                p[0] += p[2] * dt
                p[1] += p[3] * dt
                if p[4] < p[5] and p[0] > -10:
                    alive.append(p)
            # キャップ
            if len(alive) > self._max_particles:
                alive = alive[-self._max_particles:]
            self._particles[ch_idx] = alive
        self.update()

    def paintEvent(self, event):
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        # 黒背景
        qp.fillRect(self.rect(), QtGui.QColor(8, 8, 14))
        w = self.width()
        h = self.height()
        left_margin = 50

        for ch_idx in range(self.n_ch):
            col = self.ch_colors[ch_idx]
            data = self._data[ch_idx]
            ch_rect = self._channel_rect(ch_idx)
            y_center = ch_rect.top() + ch_rect.height() / 2

            # チャンネル名 (左)
            font = QtGui.QFont("Consolas", 10, QtGui.QFont.Bold)
            qp.setFont(font)
            qp.setPen(col)
            qp.drawText(QtCore.QRectF(4, y_center - 9, left_margin - 8, 18),
                        QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                        self.ch_names[ch_idx])

            # 0 ライン (薄)
            zero_pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 18), 1,
                                   QtCore.Qt.DashLine)
            qp.setPen(zero_pen)
            qp.drawLine(QtCore.QPointF(left_margin, y_center),
                        QtCore.QPointF(w, y_center))

            if len(data) < 2:
                continue

            # 波形パス (300pt 程度に間引き、 1px 未満の細かさは不要)
            n = len(data)
            target_pts = min(300, max(80, int(w - left_margin)))
            step = max(1, n // target_pts)
            path = QtGui.QPainterPath()
            first = True
            for i in range(0, n, step):
                xx = left_margin + (i / (n - 1)) * (w - left_margin - 4)
                yy = self._val_to_y(data[i], ch_rect)
                if first:
                    path.moveTo(xx, yy)
                    first = False
                else:
                    path.lineTo(xx, yy)

            # グロー (2段、軽量化)
            for line_w, alpha in [(3, 40), (1.2, 220)]:
                pen = QtGui.QPen(
                    QtGui.QColor(col.red(), col.green(), col.blue(), alpha),
                    line_w)
                pen.setCapStyle(QtCore.Qt.RoundCap)
                pen.setJoinStyle(QtCore.Qt.RoundJoin)
                qp.setPen(pen)
                qp.setBrush(QtCore.Qt.NoBrush)
                qp.drawPath(path)

            # パーティクル (シンプルな円のみ、ハローなし)
            qp.setPen(QtCore.Qt.NoPen)
            for p in self._particles[ch_idx]:
                xx, yy, _vx, _vy, life, max_life = p
                if xx < left_margin:
                    continue
                t = life / max_life
                alpha = int(220 * (1.0 - t) ** 1.4)
                if alpha < 8:
                    continue
                size = 1.2 + (1.0 - t) * 2.0
                qp.setBrush(QtGui.QColor(col.red(), col.green(),
                                          col.blue(), alpha))
                qp.drawEllipse(QtCore.QPointF(xx, yy), size, size)
        qp.end()


class _EEGCurveProxy:
    """pyqtgraph curve 互換: setData(x, d) を _ParticleEEG に転送."""

    def __init__(self, particle_eeg, idx):
        self._p = particle_eeg
        self._idx = idx

    def setData(self, x, d):
        self._p.set_data(self._idx, x, d)


class _BandSphere(QtWidgets.QWidget):
    """mockup 風の neon リングゲージ. 外側カラーリング + 中央数値 + 下ラベル."""

    def __init__(self, label, color_hex, parent=None):
        super().__init__(parent)
        self.label = label
        self._color = QtGui.QColor(color_hex)
        self._value = 0.0
        self._raw = 0.0
        self._phase = 0.0
        self.setMinimumSize(110, 150)   # ラベル切れ防止のため +20
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(100)   # 軽量化: 60→100ms

    def _tick(self):
        if not self.isVisible():
            return
        import math as _m
        self._phase = (self._phase + 0.10) % (2 * _m.pi)
        self.update()

    def set_value(self, normalized, raw=None):
        v = max(0.0, min(1.0, float(normalized)))
        self._value = v
        if raw is not None:
            self._raw = raw
        self.update()

    def paintEvent(self, event):
        import math as _m
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        cx = w / 2
        # リング中心を少し上にして下にラベルスペースを確保
        cy = h * 0.40
        # 半径 (下にラベル領域 32px を残す)
        radius = min(w * 0.42, (h - 36) * 0.45)
        breathe = 1.0 + 0.05 * _m.sin(self._phase)
        radius *= breathe

        col = self._color
        cr, cg, cb = col.red(), col.green(), col.blue()

        # 外側ハロー (mountain shadow 風: 雲のような後光)
        for mult, alpha in [(2.4, 18), (1.7, 35), (1.25, 70)]:
            grad = QtGui.QRadialGradient(QtCore.QPointF(cx, cy), radius * mult)
            mul_alpha = int(alpha * (0.4 + 0.6 * self._value))
            grad.setColorAt(0.0, QtGui.QColor(cr, cg, cb, mul_alpha))
            grad.setColorAt(1.0, QtGui.QColor(cr, cg, cb, 0))
            qp.setPen(QtCore.Qt.NoPen)
            qp.setBrush(grad)
            qp.drawEllipse(QtCore.QPointF(cx, cy),
                           radius * mult, radius * mult)

        # 値による不規則 mountain (4ch シミュ): リング外側で軽い波形を描く
        n_pts = 36
        bg_path = QtGui.QPainterPath()
        for i in range(n_pts + 1):
            ang = 2 * _m.pi * i / n_pts - _m.pi / 2
            jitter = 0.05 + 0.10 * self._value * (
                0.5 + 0.5 * _m.sin(self._phase * 1.5 + i * 0.7))
            r_pt = radius * (1.15 + jitter)
            x = cx + r_pt * _m.cos(ang)
            y = cy + r_pt * _m.sin(ang)
            if i == 0:
                bg_path.moveTo(x, y)
            else:
                bg_path.lineTo(x, y)
        bg_pen = QtGui.QPen(QtGui.QColor(cr, cg, cb,
                                          int(160 * (0.3 + 0.7 * self._value))),
                             1.2)
        qp.setPen(bg_pen)
        qp.setBrush(QtCore.Qt.NoBrush)
        qp.drawPath(bg_path)

        # メインリング: track (暗) + fill (アクセント)
        ring_thickness = 6
        track_pen = QtGui.QPen(QtGui.QColor(cr, cg, cb, 50),
                                ring_thickness)
        track_pen.setCapStyle(QtCore.Qt.RoundCap)
        qp.setPen(track_pen)
        qp.setBrush(QtCore.Qt.NoBrush)
        rect = QtCore.QRectF(cx - radius, cy - radius,
                             radius * 2, radius * 2)
        qp.drawArc(rect, 90 * 16, 360 * 16)
        # fill arc
        fill_pen = QtGui.QPen(QtGui.QColor(cr, cg, cb, 240),
                               ring_thickness)
        fill_pen.setCapStyle(QtCore.Qt.RoundCap)
        qp.setPen(fill_pen)
        span = int(-360 * 16 * self._value)
        qp.drawArc(rect, 90 * 16, span)

        # 内側 panel (暗いガラス)
        inner_r = radius - ring_thickness - 4
        inner_grad = QtGui.QRadialGradient(QtCore.QPointF(cx, cy), inner_r)
        inner_grad.setColorAt(0.0, QtGui.QColor(15, 15, 22, 220))
        inner_grad.setColorAt(1.0, QtGui.QColor(5, 5, 8, 230))
        qp.setBrush(inner_grad)
        qp.setPen(QtCore.Qt.NoPen)
        qp.drawEllipse(QtCore.QPointF(cx, cy), inner_r, inner_r)

        # 値テキスト
        val_font = QtGui.QFont("Consolas")
        val_font.setPointSize(11)
        val_font.setBold(True)
        qp.setFont(val_font)
        qp.setPen(QtGui.QColor(255, 255, 255, 240))
        qp.drawText(QtCore.QRectF(cx - radius, cy - 10, radius * 2, 20),
                    QtCore.Qt.AlignCenter, f"{self._raw:.2f}")

        # ラベル (下: ギリシャ文字が切れないように余白多め)
        lbl_font = QtGui.QFont()
        lbl_font.setPointSize(15)
        lbl_font.setBold(True)
        qp.setFont(lbl_font)
        qp.setPen(col)
        # 28px の高さで描画し、4px の下余白を残す
        qp.drawText(QtCore.QRectF(0, h - 32, w, 28),
                    QtCore.Qt.AlignCenter, self.label)
        qp.end()


class _InstrumentCircle(QtWidgets.QWidget):
    """楽器ごとの円形 EQ 表示. ネオンリング + 絵文字 + dB 値.

    上半分クリック → EQ +0.5dB / 下半分クリック → -0.5dB.
    bump シグナル経由で MainWindow に通知.
    """
    bump = QtCore.pyqtSignal(str, float)   # key, delta_db

    def __init__(self, key, emoji, name, parent=None):
        super().__init__(parent)
        self.key = key
        self.emoji = emoji
        self.name = name
        self._value_db = 0.0
        self._gain_max = 6.0
        self._accent = QtGui.QColor("#1abc9c")
        self._text_dim = QtGui.QColor("#8a8a8a")
        self._phase = 0.0
        self.setMinimumSize(150, 200)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setToolTip(
            f"{name} — クリック上半分 ↑ +0.5dB / 下半分 ↓ -0.5dB")
        self._hover_half = None   # "up" / "down" / None
        self.setMouseTracking(True)
        # 楽器テクスチャ画像 (assets/instruments/{key}.png)
        self._texture = None
        tex_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets", "instruments", f"{key}.png")
        if os.path.exists(tex_path):
            img = QtGui.QImage(tex_path)
            if not img.isNull():
                self._texture = img
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(100)   # 軽量化

    def _tick(self):
        if not self.isVisible():
            return
        import math as _m
        self._phase = (self._phase + 0.13) % (2 * _m.pi)
        self.update()

    def set_accent(self, color_hex):
        self._accent = QtGui.QColor(color_hex)
        self.update()

    def set_value(self, db):
        if abs(db - self._value_db) < 0.05:
            return
        self._value_db = float(db)
        self.update()

    def paintEvent(self, event):
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        # 円は上半分、テキストは下半分にしっかり分ける
        cx = w / 2
        r = min(w * 0.42, (h - 50) * 0.5)  # 下 50px をテキスト領域として確保
        cy = r + 6

        # 強度 0..1 (絶対値 / gain_max)
        intensity = min(1.0, abs(self._value_db) / self._gain_max)

        # 値で脈動 (mockup 風)
        import math as _m
        breathe = 1.0 + 0.04 * _m.sin(self._phase) * (
            0.5 + 0.5 * intensity)
        r *= breathe

        # 外側グロー (4段、強め)
        for mult, alpha in [(3.2, 14), (2.3, 28), (1.7, 55),
                              (1.25, 130)]:
            col = QtGui.QColor(self._accent)
            col.setAlpha(int(alpha * (0.5 + 0.5 * intensity)))
            g = QtGui.QRadialGradient(QtCore.QPointF(cx, cy), r * mult)
            g.setColorAt(0.0, col)
            edge = QtGui.QColor(self._accent)
            edge.setAlpha(0)
            g.setColorAt(1.0, edge)
            qp.setPen(QtCore.Qt.NoPen)
            qp.setBrush(g)
            qp.drawEllipse(QtCore.QPointF(cx, cy), r * mult, r * mult)

        # 太いネオンリング
        ring_pen = QtGui.QPen(self._accent, 3.5 + 2.5 * intensity)
        qp.setPen(ring_pen)
        qp.setBrush(QtGui.QColor(6, 6, 10, 220))
        qp.drawEllipse(QtCore.QPointF(cx, cy), r, r)
        # 内側 2重リング
        inner_pen = QtGui.QPen(
            QtGui.QColor(255, 255, 255, int(50 + 100 * intensity)), 1.2)
        qp.setPen(inner_pen)
        qp.setBrush(QtCore.Qt.NoBrush)
        qp.drawEllipse(QtCore.QPointF(cx, cy), r - 5, r - 5)
        # 内側さらに細い
        inner2_pen = QtGui.QPen(
            QtGui.QColor(self._accent.red(), self._accent.green(),
                          self._accent.blue(),
                          int(100 + 60 * intensity)), 0.8)
        qp.setPen(inner2_pen)
        qp.drawEllipse(QtCore.QPointF(cx, cy), r - 10, r - 10)

        # 中央: テクスチャ画像があれば使う、なければ絵文字
        if self._texture is not None and not self._texture.isNull():
            # 円内 (半径 r - 10 程度) にアスペクト維持でフィット
            inner_r = r - 12
            qp.save()
            # 円形クリップ
            clip = QtGui.QPainterPath()
            clip.addEllipse(QtCore.QPointF(cx, cy), inner_r, inner_r)
            qp.setClipPath(clip)
            img = self._texture
            src_ratio = img.width() / max(1, img.height())
            box = inner_r * 2
            if src_ratio >= 1:
                dw = box
                dh = box / src_ratio
            else:
                dh = box
                dw = box * src_ratio
            qp.drawImage(
                QtCore.QRectF(cx - dw / 2, cy - dh / 2, dw, dh),
                img)
            qp.restore()
        else:
            emoji_font = QtGui.QFont()
            emoji_font.setPointSize(int(r * 0.7))
            qp.setFont(emoji_font)
            qp.setPen(QtGui.QColor(255, 255, 255, 240))
            qp.drawText(QtCore.QRectF(cx - r, cy - r, r * 2, r * 2),
                        QtCore.Qt.AlignCenter, self.emoji)

        # dB 値 (円の下)
        value_font = QtGui.QFont("Consolas")
        value_font.setPointSize(11)
        value_font.setBold(True)
        qp.setFont(value_font)
        sign = "+" if self._value_db >= 0 else ""
        qp.setPen(QtGui.QColor(255, 255, 255, 230))
        qp.drawText(QtCore.QRectF(0, h - 40, w, 18),
                    QtCore.Qt.AlignCenter,
                    f"{sign}{self._value_db:.1f} dB")
        # 名前 (さらに下)
        name_font = QtGui.QFont()
        name_font.setPointSize(9)
        qp.setFont(name_font)
        qp.setPen(self._text_dim)
        qp.drawText(QtCore.QRectF(0, h - 20, w, 14),
                    QtCore.Qt.AlignCenter, self.name)

        # ホバー時: 上下半分にうっすら ↑/↓ ヒント
        if self._hover_half is not None:
            arrow_font = QtGui.QFont("Segoe UI", 22, QtGui.QFont.Bold)
            qp.setFont(arrow_font)
            qp.setPen(QtGui.QColor(self._accent.red(),
                                   self._accent.green(),
                                   self._accent.blue(), 160))
            if self._hover_half == "up":
                qp.drawText(QtCore.QRectF(0, 4, w, 22),
                            QtCore.Qt.AlignCenter, "▲")
            else:
                qp.drawText(QtCore.QRectF(0, h - 64, w, 22),
                            QtCore.Qt.AlignCenter, "▼")
        qp.end()

    # ---- マウス操作: 上半分↑ / 下半分↓ で EQ ±0.5dB ----
    def mousePressEvent(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            return
        half = "up" if ev.pos().y() < self.height() / 2 else "down"
        delta = 0.5 if half == "up" else -0.5
        # 限界クランプ
        new_db = max(-self._gain_max,
                     min(self._gain_max, self._value_db + delta))
        delta_actual = new_db - self._value_db
        if abs(delta_actual) > 1e-3:
            self._value_db = new_db
            self.update()
            self.bump.emit(self.key, delta_actual)

    def mouseMoveEvent(self, ev):
        half = "up" if ev.pos().y() < self.height() / 2 else "down"
        if half != self._hover_half:
            self._hover_half = half
            self.update()

    def leaveEvent(self, ev):
        if self._hover_half is not None:
            self._hover_half = None
            self.update()


class _RibbonBar(QtWidgets.QWidget):
    """流れるベジエリボン. value(0..1) で長さ + 色のグラデ.
    mockup の "流れる色リボン" 用.
    """

    def __init__(self, gradient_stops, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(28)
        self.setMaximumHeight(34)
        self.setMinimumWidth(120)
        self._stops = gradient_stops
        self._value = 0.5
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(80)   # 軽量化

    def set_value(self, v):
        v = max(0.0, min(1.0, float(v)))
        if abs(v - self._value) < 0.005:
            return
        self._value = v

    def _tick(self):
        if not self.isVisible():
            return
        import math as _m
        self._phase = (self._phase + 0.18) % (2 * _m.pi * 100)
        self.update()

    def paintEvent(self, event):
        import math as _m
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        cy = h / 2

        # 背景 track (薄)
        track_h = h * 0.55
        track_rect = QtCore.QRectF(0, (h - track_h) / 2, w, track_h)
        qp.setPen(QtCore.Qt.NoPen)
        qp.setBrush(QtGui.QColor(255, 255, 255, 18))
        qp.drawRoundedRect(track_rect, track_h / 2, track_h / 2)

        # リボン塗り (値の長さで)
        fill_w = w * self._value
        if fill_w < 4:
            qp.end()
            return

        # ベジエ波形パス: 2本の波線で帯を作る
        amp = track_h * 0.45
        seg = 28   # サンプリング数
        path = QtGui.QPainterPath()
        # 上端
        for i in range(seg + 1):
            t = i / seg
            x = t * fill_w
            wave = amp * _m.sin(self._phase + t * 5.0)
            y = cy - track_h * 0.35 - wave * 0.4
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        # 下端 (折り返し)
        for i in range(seg, -1, -1):
            t = i / seg
            x = t * fill_w
            wave = amp * _m.sin(self._phase + t * 5.0 + 1.7)
            y = cy + track_h * 0.35 - wave * 0.4
            path.lineTo(x, y)
        path.closeSubpath()

        # グラデで塗る
        grad = QtGui.QLinearGradient(0, 0, w, 0)
        for pos, col in self._stops:
            grad.setColorAt(pos, col)
        qp.setBrush(grad)
        qp.setPen(QtCore.Qt.NoPen)
        qp.drawPath(path)

        # 細い highlight 線 (中央)
        hl_path = QtGui.QPainterPath()
        for i in range(seg + 1):
            t = i / seg
            x = t * fill_w
            wave = amp * _m.sin(self._phase + t * 5.0 + 0.9)
            y = cy - wave * 0.25
            if i == 0:
                hl_path.moveTo(x, y)
            else:
                hl_path.lineTo(x, y)
        hl_pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 120), 1.2)
        hl_pen.setCapStyle(QtCore.Qt.RoundCap)
        qp.setPen(hl_pen)
        qp.setBrush(QtCore.Qt.NoBrush)
        qp.drawPath(hl_path)
        qp.end()


class _HudBar(QtWidgets.QWidget):
    """HUD 用グラデーション付きバー. set_value(0..1)."""

    def __init__(self, gradient_stops, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(8)
        self.setMaximumHeight(10)
        self.setMinimumWidth(80)
        self._stops = gradient_stops  # [(pos0..1, QColor), ...]
        self._value = 0.5

    def set_value(self, v):
        v = max(0.0, min(1.0, float(v)))
        if abs(v - self._value) < 0.001:
            return
        self._value = v
        self.update()

    def paintEvent(self, event):
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing, True)
        r = self.rect().adjusted(0, 0, -1, -1)
        # 背景 (track)
        qp.setPen(QtCore.Qt.NoPen)
        qp.setBrush(QtGui.QColor(255, 255, 255, 22))
        qp.drawRoundedRect(r, 3, 3)
        # 塗り
        if self._value > 0.001:
            fill_w = int(r.width() * self._value)
            fill_rect = QtCore.QRect(r.left(), r.top(), fill_w, r.height())
            grad = QtGui.QLinearGradient(0, 0, r.width(), 0)
            for pos, col in self._stops:
                grad.setColorAt(pos, col)
            qp.setBrush(grad)
            qp.drawRoundedRect(fill_rect, 3, 3)
        qp.end()


def progress_style(color, height=18):
    return f"""
        QProgressBar {{
            background-color: #18181a; border: 1px solid #3a3a3a;
            border-radius: 4px; height: {height}px; text-align: center;
            color: #e0e0e0; font-size: 11px;
        }}
        QProgressBar::chunk {{ background-color: {color}; border-radius: 3px; }}
    """


# ======================================================================
# メインウィンドウ
# ======================================================================
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EEG Real-Time Monitor — Muse S Athena")
        self.resize(1500, 1000)

        # テーマ (ヘッダ構築より前に必要)
        self.theme = ThemeManager()
        self.theme.subscribe(self._on_theme_changed)

        # 初回スタイル適用
        self._apply_global_style()

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(self._build_header())

        self.stack = QtWidgets.QStackedWidget()
        root.addWidget(self.stack, 1)

        self.grid_page = QtWidgets.QWidget()
        self.grid_layout = QtWidgets.QGridLayout(self.grid_page)
        self.grid_layout.setSpacing(10)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.stack.addWidget(self.grid_page)

        self.focus_page = QtWidgets.QWidget()
        self.focus_layout = QtWidgets.QVBoxLayout(self.focus_page)
        self.focus_layout.setContentsMargins(0, 0, 0, 0)
        self.stack.addWidget(self.focus_page)

        # 音声エンジン (VB-CABLE → pedalboard → ヘッドホン/スピーカー)
        # カード生成より前に作る (EQ カードが self.audio を参照するため)
        self.audio = AudioEngine()

        # --- カード生成 ---
        self.cards = {}
        self.card_positions = {}

        eeg_w, self.eeg_curves = self._build_eeg_plots()
        self.cards["eeg"] = Card("Raw EEG  |  bandpass 1–40 Hz", eeg_w, "eeg",
                                 theme=self.theme)
        self.cards["eeg"].set_hover_info(
            "RAW EEG  ·  4ch",
            "TP9 / AF7 / AF8 / TP10 の生波形 (1–40 Hz バンドパス).\n"
            "粒子は最新値の位置に出生して左に流れる. 視覚化で値の急変を捉えやすい.")
        self.card_positions["eeg"] = (0, 0)

        (spec_w, self.spec_img, self.spec_data, self.spec_ch_selector,
         self.spec_mode_selector, self.spec_plot) = self._build_spectrogram()
        self._spec_mode = 0  # STFT のみ
        self.cards["spec"] = Card("Spectrogram  |  rolling STFT (log power)",
                                  spec_w, "spec", theme=self.theme)
        self.cards["spec"].set_hover_info(
            "SPECTROGRAM  ·  STFT",
            "選択チャンネルのローリング STFT (Welch 法).\n"
            "縦軸 = 周波数 (0–40 Hz), 横軸 = 時間, 色 = log power.\n"
            "α/β/γ 帯のパワー変動が時系列で見える.")
        self.card_positions["spec"] = (1, 0)

        hr_w, self.hr_curves, self.hr_bpm_label, self.hr_status = self._build_hr_panel()
        self.cards["hr"] = Card("Heart Rate & fNIRS (Athena 光学センサー)",
                                hr_w, "hr", theme=self.theme)
        self.cards["hr"].set_hover_info(
            "HEART RATE  ·  fNIRS",
            "Muse S Athena の PPG / 光学センサー (4ch).\n"
            "BPM は OSC ストリーム or PPG ピーク検出から取得.\n"
            "心拍は HR ヒステリシスで水中シーン切替にも使う.")
        self.card_positions["hr"] = (2, 0)

        band_w, self.band_bars = self._build_band_plot()
        self.cards["band"] = Card("Band Power (δ, θ, α, β, γ)", band_w, "band",
                                  theme=self.theme)
        self.cards["band"].set_hover_info(
            "BAND POWER  ·  δθαβγ",
            "5 帯域 (Delta / Theta / Alpha / Beta / Gamma) のネオンリングゲージ.\n"
            "値は 4ch 平均の log power を 0..1 に正規化.\n"
            "α/β 比 から Engagement や Arousal も算出.")
        self.card_positions["band"] = (0, 1)

        q_w, self.q_dots, self.q_texts, self.q_descs, \
            self.touching_row, self.blink_row, self.jaw_row = \
            self._build_quality_panel()
        self.cards["quality"] = Card("Signal Quality (電極接触状態)", q_w, "quality",
                                     theme=self.theme)
        self.cards["quality"].set_hover_info(
            "SIGNAL QUALITY  ·  Electrode Contact",
            "Muse horseshoe 値: 1 = Good / 2 = OK / 4 = Bad.\n"
            "Bad のチャンネルは感情推定から自動除外される.\n"
            "ドットの脈動速度は接触状態に対応 (Good = ゆっくり).")
        self.card_positions["quality"] = (1, 1)

        (e_w, self.russell_scatter, self.russell_curve,
         self.arousal_bar, self.valence_bar, self.emotion_label,
         self.emo_extras) = self._build_emotion_panel()
        self.cards["emotion"] = Card("Emotion — Russell's Circumplex Model (1980)",
                                     e_w, "emotion", theme=self.theme)
        self.cards["emotion"].set_hover_info(
            "EMOTION  ·  Russell's Circumplex",
            "Arousal × Valence の 2 次元感情マップ (Russell, 1980).\n"
            "白いドット = 現在状態 / 軌跡 = 過去履歴.\n"
            "4 象限: Excited / Stressed / Sad / Calm.")
        self.card_positions["emotion"] = (2, 1)

        eq_w = self._build_eq_card()
        self.cards["eq"] = Card("🎛  Adaptive EQ  |  VB-CABLE → Output",
                                eq_w, "eq", theme=self.theme,
                                accent_border=True)
        self.cards["eq"].set_hover_info(
            "ADAPTIVE EQ  ·  6-band + Reverb",
            "VB-CABLE → pedalboard → 出力デバイスの信号経路.\n"
            "Drums / Bass / Mid / Vocals / High / Air の各帯域 + Reverb.\n"
            "Auto モードで感情から自動制御, Manual で直接操作.")
        # EQ は 3行をまたぐ右端カラム
        self.card_positions["eq"] = (0, 2, 3, 1)  # row, col, rowspan, colspan

        for c in self.cards.values():
            c.expand_requested.connect(self.toggle_focus)
            c.swap_requested.connect(self._swap_cards)

        for cid, pos in self.card_positions.items():
            if len(pos) == 4:
                r, c, rs, cs = pos
                self.grid_layout.addWidget(self.cards[cid], r, c, rs, cs)
            else:
                r, c = pos
                self.grid_layout.addWidget(self.cards[cid], r, c)
        self.grid_layout.setColumnStretch(0, 5)
        self.grid_layout.setColumnStretch(1, 4)
        self.grid_layout.setColumnStretch(2, 4)
        self.grid_layout.setRowStretch(0, 4)
        self.grid_layout.setRowStretch(1, 3)
        self.grid_layout.setRowStretch(2, 3)

        self.focused_card = None
        self.stack.setCurrentIndex(0)

        # --- Watch page: Sea を通常レイアウトで全画面、HUD は子で手動配置 ---
        self.watch_page = QtWidgets.QWidget()
        wp_lay = QtWidgets.QVBoxLayout(self.watch_page)
        wp_lay.setContentsMargins(0, 0, 0, 0)
        wp_lay.setSpacing(0)
        self._watch_sea_holder = QtWidgets.QWidget()
        sh_lay = QtWidgets.QVBoxLayout(self._watch_sea_holder)
        sh_lay.setContentsMargins(0, 0, 0, 0)
        wp_lay.addWidget(self._watch_sea_holder)
        # Watch 装飾レイヤ (sea の上, mandala/HUD の下)
        self._watch_tron = _TronGrid(self.watch_page)
        self._watch_tron.set_accent(self.theme.accent)
        self._watch_tron.setGeometry(self.watch_page.rect())
        self._watch_matrix = _MatrixRain(self.watch_page)
        self._watch_matrix.set_accent(self.theme.accent)
        self._watch_matrix.setGeometry(self.watch_page.rect())
        # 神聖幾何学オーバーレイ (Sea の上, HUD の下)
        self._watch_mandala = _MandalaOverlay(self.watch_page)
        self._watch_mandala.set_accent(self.theme.accent)
        self._watch_mandala.setGeometry(self.watch_page.rect())
        # HUD は watch_page の子として overlay
        self._watch_hud = self._build_watch_hud()
        self._watch_hud.setParent(self.watch_page)
        self._watch_hud.setGeometry(self.watch_page.rect())
        # Photo ボタン (右上)
        self._watch_photo_btn = QtWidgets.QPushButton("📷", self.watch_page)
        self._watch_photo_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._watch_photo_btn.setFixedSize(36, 36)
        self._watch_photo_btn.setStyleSheet(
            "QPushButton { background-color: rgba(0,0,0,140); "
            "color: #ffffff; border: 1.5px solid rgba(255,255,255,80); "
            "border-radius: 18px; font-size: 16px; }"
            f"QPushButton:hover {{ border-color: {self.theme.accent}; "
            "background-color: rgba(0,0,0,200); }}"
        )
        self._watch_photo_btn.setToolTip("Save current Watch view (PNG)")
        self._watch_photo_btn.clicked.connect(self._watch_save_photo)
        # Demo モードボタン (Photo の左隣) — 模擬的に状態をループ
        self._watch_demo_btn = QtWidgets.QPushButton("▶ Demo",
                                                      self.watch_page)
        self._watch_demo_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._watch_demo_btn.setCheckable(True)
        self._watch_demo_btn.setFixedSize(76, 36)
        self._watch_demo_btn.setStyleSheet(
            "QPushButton { background-color: rgba(0,0,0,140); "
            "color: #ffffff; border: 1.5px solid rgba(255,255,255,80); "
            "border-radius: 18px; font-size: 12px; font-weight: bold; }"
            f"QPushButton:hover {{ border-color: {self.theme.accent}; "
            "background-color: rgba(0,0,0,200); }}"
            f"QPushButton:checked {{ background-color: {self.theme.accent}; "
            "color: #ffffff; border-color: #ffffff; }}"
        )
        self._watch_demo_btn.setToolTip(
            "Demo mode — simulate EEG/HR changes so reviewers "
            "without a headset can see the scenes morph")
        self._watch_demo_btn.clicked.connect(self._watch_toggle_demo)
        # Demo state
        self._watch_demo_active = False
        self._watch_demo_t0 = 0.0
        self._watch_demo_timer = QtCore.QTimer(self)
        self._watch_demo_timer.timeout.connect(self._watch_demo_tick)
        # Demo 説明パネル (右側オーバーレイ. ON 時のみ visible)
        self._watch_demo_panel = _DemoExplainOverlay(self.watch_page)
        self._watch_demo_panel.setVisible(False)
        # マウス追従パーティクル (HUD の上)
        self._watch_cursor_particles = _CursorParticles(self.watch_page)
        self._watch_cursor_particles.set_accent(self.theme.accent)
        self._watch_cursor_particles.setGeometry(self.watch_page.rect())
        # スタッキング順: tron → matrix → mandala → hud → cursor particles
        self._watch_tron.lower()
        self._watch_matrix.raise_()
        self._watch_mandala.raise_()
        self._watch_hud.raise_()
        self._watch_cursor_particles.raise_()
        self.watch_page.installEventFilter(self)
        self.stack.addWidget(self.watch_page)

        # 走査線オーバーレイは完全廃止. central の resize は eventFilter で受ける
        central.installEventFilter(self)

        # --- Listen page (没入型操作集中 UI) ---
        self.listen_page = self._build_listen_page()
        self.stack.addWidget(self.listen_page)

        # --- Footer ステータスバー (常時表示) ---
        self.footer = self._build_footer()
        root.addWidget(self.footer)

        self._mode = "studio"
        self._update_mode_btn_style()

        # 履歴
        self.emo_hist_v = deque([0.5] * EMO_HIST_LEN, maxlen=EMO_HIST_LEN)
        self.emo_hist_a = deque([0.5] * EMO_HIST_LEN, maxlen=EMO_HIST_LEN)
        self.eng_hist = deque([0.5] * EMO_HIST_LEN, maxlen=EMO_HIST_LEN)
        self.ar_hist = deque([0.5] * EMO_HIST_LEN, maxlen=EMO_HIST_LEN)

        # CSV 録画
        self.recording = False
        self.rec_file = None
        self.rec_writer = None
        self.rec_start_time = 0.0
        self.rec_row_count = 0

        # タイマー
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.update_ui)
        self.timer.start(33)
        self._last_spec_update = 0.0
        self._last_hr_update = 0.0

        self.esc_sc = QtWidgets.QShortcut(QtGui.QKeySequence("Esc"), self)
        self.esc_sc.activated.connect(self._restore_if_focused)

        # キーボードショートカット (context aware)
        QtWidgets.QShortcut(QtGui.QKeySequence("1"), self,
                            activated=lambda: self._key_num(1))
        QtWidgets.QShortcut(QtGui.QKeySequence("2"), self,
                            activated=lambda: self._key_num(2))
        QtWidgets.QShortcut(QtGui.QKeySequence("3"), self,
                            activated=lambda: self._key_num(3))
        QtWidgets.QShortcut(QtGui.QKeySequence("4"), self,
                            activated=lambda: self._key_num(4))
        QtWidgets.QShortcut(QtGui.QKeySequence("Space"), self,
                            activated=self._toggle_audio)
        QtWidgets.QShortcut(QtGui.QKeySequence("R"), self,
                            activated=self._toggle_recording)
        # モードクイックスイッチ (Watch でも使えるよう Ctrl 修飾子つき)
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+1"), self,
                            activated=lambda: self._set_mode("studio"))
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+2"), self,
                            activated=lambda: self._set_mode("listen"))
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+3"), self,
                            activated=lambda: self._set_mode("watch"))
        QtWidgets.QShortcut(QtGui.QKeySequence("F1"), self,
                            activated=self._show_help)
        QtWidgets.QShortcut(QtGui.QKeySequence("F11"), self,
                            activated=self._toggle_fullscreen)
        QtWidgets.QShortcut(QtGui.QKeySequence("F12"), self,
                            activated=self._take_screenshot)

        # REC 脈動アニメ用 phase
        self._rec_pulse_phase = 0.0
        self._rec_pulse_timer = QtCore.QTimer(self)
        self._rec_pulse_timer.timeout.connect(self._rec_pulse_tick)
        self._rec_pulse_timer.start(80)

        # Audio ON 中の呼吸アニメ
        self._audio_pulse_phase = 0.0
        self._audio_pulse_timer = QtCore.QTimer(self)
        self._audio_pulse_timer.timeout.connect(self._audio_pulse_tick)
        self._audio_pulse_timer.start(80)

        # 初回起動時のみ Welcome オーバーレイ
        QtCore.QTimer.singleShot(400, self._maybe_show_welcome)

        # アイドルスクリーンセーバー (180秒無操作で発動)
        self._last_activity_time = time.time()
        self._idle_overlay = None
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._idle_timer = QtCore.QTimer(self)
        self._idle_timer.timeout.connect(self._idle_check)
        self._idle_timer.start(1500)
        self._idle_threshold = 180.0   # 秒

    def _welcome_flag_path(self):
        from pathlib import Path
        d = Path.home() / ".muse_eq"
        d.mkdir(parents=True, exist_ok=True)
        return d / "welcome_seen.flag"

    def _idle_check(self):
        """アイドル時間チェック. 閾値超えたらスクリーンセーバー表示."""
        if self._idle_overlay is not None:
            return   # 既に表示中
        # 録画中 or リプレイ中はアイドルにしない
        if (getattr(self, "recording", False)
                or getattr(self, "_replay_state", None) is not None):
            self._last_activity_time = time.time()
            return
        elapsed = time.time() - self._last_activity_time
        if elapsed < self._idle_threshold:
            return
        try:
            ov = _IdleScreensaver(self.centralWidget(),
                                   accent=self.theme.accent)
            ov.setGeometry(self.centralWidget().rect())
            ov.show()
            ov.raise_()
            ov.closed.connect(self._on_idle_closed)
            self._idle_overlay = ov
        except Exception as e:
            print("[idle] err:", e)

    def _on_idle_closed(self):
        self._idle_overlay = None
        self._last_activity_time = time.time()

    def _maybe_show_welcome(self):
        flag = self._welcome_flag_path()
        if flag.exists():
            return
        try:
            w = _WelcomeOverlay(self.centralWidget(), accent=self.theme.accent)
            w.setGeometry(self.centralWidget().rect())
            w.show()
            w.raise_()
            w.closed.connect(lambda: flag.write_text("seen\n"))
            self._welcome_overlay = w
        except Exception as e:
            print("[welcome] error:", e)

    # --- Focus ---
    def toggle_focus(self, card_id):
        if self.focused_card == card_id:
            self._restore_grid()
        else:
            if self.focused_card is not None:
                self._restore_grid()
            self._focus_card(card_id)

    def _focus_card(self, card_id):
        card = self.cards[card_id]
        self.grid_layout.removeWidget(card)
        self.focus_layout.addWidget(card)
        card.set_expanded(True)
        self.stack.setCurrentIndex(1)
        self.focused_card = card_id

    def _restore_grid(self):
        if self.focused_card is None:
            return
        card = self.cards[self.focused_card]
        self.focus_layout.removeWidget(card)
        pos = self.card_positions[self.focused_card]
        if len(pos) == 4:
            r, c, rs, cs = pos
            self.grid_layout.addWidget(card, r, c, rs, cs)
        else:
            r, c = pos
            self.grid_layout.addWidget(card, r, c)
        card.set_expanded(False)
        self.stack.setCurrentIndex(0)
        self.focused_card = None

    def _restore_if_focused(self):
        if self.focused_card is not None:
            self._restore_grid()

    def _swap_cards(self, src_id, dst_id):
        """2 つのカードの grid 上の位置を入れ替える."""
        if src_id == dst_id:
            return
        if src_id not in self.card_positions or dst_id not in self.card_positions:
            return
        src_pos = self.card_positions[src_id]
        dst_pos = self.card_positions[dst_id]
        src_card = self.cards[src_id]
        dst_card = self.cards[dst_id]
        # 一度両方とも grid から外す
        self.grid_layout.removeWidget(src_card)
        self.grid_layout.removeWidget(dst_card)
        # ポジション入れ替え
        self.card_positions[src_id] = dst_pos
        self.card_positions[dst_id] = src_pos
        # 再配置
        def _add(card, pos):
            if len(pos) == 4:
                r, c, rs, cs = pos
                self.grid_layout.addWidget(card, r, c, rs, cs)
            else:
                r, c = pos
                self.grid_layout.addWidget(card, r, c)
        _add(src_card, dst_pos)
        _add(dst_card, src_pos)
        self._show_toast(f"⇄ Swapped: {src_id} ↔ {dst_id}")

    # --- パーツ ---
    def _build_header(self):
        from audio_engine import list_output_devices, pick_output_device
        w = QtWidgets.QFrame()
        w.setObjectName("header")
        self._header_frame = w
        # 回路パターン画像があれば背景に使う + アクセント色の neon border
        circuit_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets", "bg", "bg_circuit.png").replace("\\", "/")
        t = self.theme
        ar, ag, ab = (QtGui.QColor(t.accent).red(),
                       QtGui.QColor(t.accent).green(),
                       QtGui.QColor(t.accent).blue())
        if os.path.exists(circuit_path):
            w.setStyleSheet(f"""
                QFrame#header {{
                    background-image: url("{circuit_path}");
                    background-position: center;
                    background-repeat: no-repeat;
                    border: 2px solid rgba({ar}, {ag}, {ab}, 200);
                    border-radius: 12px;
                }}
                QLabel {{ border: none; background: transparent; }}
            """)
        else:
            w.setStyleSheet(f"""
                QFrame#header {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 #1a1a1c, stop:1 #1f1f23);
                    border: 2px solid rgba({ar}, {ag}, {ab}, 200);
                    border-radius: 12px;
                }}
                QLabel {{ border: none; }}
            """)
        # ネオングロー (アクセント色)
        shadow = QtWidgets.QGraphicsDropShadowEffect(w)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 0)
        shadow.setColor(QtGui.QColor(ar, ag, ab, 180))
        w.setGraphicsEffect(shadow)

        # 縦 2段
        outer = QtWidgets.QVBoxLayout(w)
        outer.setContentsMargins(18, 6, 18, 6)
        outer.setSpacing(2)

        # ============ 段1: ロゴ + モード + 主要操作 ============
        row1 = QtWidgets.QHBoxLayout()
        row1.setSpacing(12)

        # ロゴ + タイトル
        title = QtWidgets.QLabel("🧠")
        title.setStyleSheet("font-size: 22px;")
        row1.addWidget(title)
        title_box = QtWidgets.QVBoxLayout()
        title_box.setSpacing(0)
        title_box.setContentsMargins(0, 0, 0, 0)
        main_title = QtWidgets.QLabel("EEG Adaptive EQ")
        main_title.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {self.theme.accent}; "
            "letter-spacing: 1px;")
        # タイトルにグロー
        title_glow = QtWidgets.QGraphicsDropShadowEffect()
        title_glow.setBlurRadius(18)
        title_glow.setOffset(0, 0)
        title_glow.setColor(QtGui.QColor(ar, ag, ab, 220))
        main_title.setGraphicsEffect(title_glow)
        self._header_title_glow = title_glow
        sub = QtWidgets.QLabel("Muse S Athena  ·  Mind Monitor")
        sub.setStyleSheet(
            "font-size: 9px; color: #8a8a8a; letter-spacing: 0.3px;")
        title_box.addWidget(main_title)
        title_box.addWidget(sub)
        row1.addLayout(title_box)

        # モードタブ (mockup 風: ○/▶ プレフィックス + 下線)
        row1.addSpacing(20)
        self.mode_btns = {}
        self._mode_labels = {"studio": "STUDIO",
                             "listen": "LISTEN",
                             "watch": "WATCH"}
        for key in ("studio", "listen", "watch"):
            btn = QtWidgets.QPushButton(f"○  {self._mode_labels[key]}")
            btn.setCheckable(True)
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setFixedHeight(32)
            btn.clicked.connect(lambda _, k=key: self._set_mode(k))
            row1.addWidget(btn)
            self.mode_btns[key] = btn
        self.mode_btns["studio"].setChecked(True)

        row1.addStretch()

        # 音声 ON/OFF
        self.audio_btn = QtWidgets.QPushButton("♪ Audio OFF")
        self.audio_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.audio_btn.setFixedHeight(28)
        self.audio_btn.setStyleSheet(self._audio_btn_style(False))
        self.audio_btn.clicked.connect(self._toggle_audio)
        self.audio_btn.setToolTip(
            "VB-CABLE → pedalboard → ヘッドホン の音声処理を開始/停止")
        row1.addWidget(self.audio_btn)

        # Replay (リプレイ)
        self.replay_btn = QtWidgets.QPushButton("▶ Replay")
        self.replay_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.replay_btn.setFixedHeight(28)
        self.replay_btn.setStyleSheet(
            "QPushButton { background-color: #1e1e20; color: #b0b0b0; "
            "border: 1px solid #303033; border-radius: 13px; "
            "padding: 3px 14px; font-size: 11px; font-weight: 600; }"
            "QPushButton:hover { background-color: #2a2a2c; color: #e8e8e8; "
            "border-color: #45454a; }"
        )
        self.replay_btn.setToolTip("Replay a recorded CSV session")
        self.replay_btn.clicked.connect(self._toggle_replay)
        row1.addWidget(self.replay_btn)
        self._replay_state = None    # 再生中の dict (data, idx, start_t)

        # REC
        self.rec_btn = QtWidgets.QPushButton("● REC")
        self.rec_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.rec_btn.setFixedHeight(28)
        self.rec_btn.setStyleSheet(self._rec_btn_style(False))
        self.rec_btn.clicked.connect(self._toggle_recording)
        self.rec_btn.setToolTip("クリックで録画開始 / 停止")
        row1.addWidget(self.rec_btn)

        # 大型コントロールパネル (↕)
        self.big_panel_btn = QtWidgets.QPushButton("↕")
        self.big_panel_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.big_panel_btn.setFixedSize(28, 28)
        self.big_panel_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #c0c0c0; "
            "border: 1px solid #3a3a3a; border-radius: 14px; "
            "font-size: 14px; font-weight: bold; }"
            f"QPushButton:hover {{ background-color: {self.theme.accent}; "
            f"border-color: {self.theme.accent}; color: #ffffff; }}"
        )
        self.big_panel_btn.setToolTip("Open large control panel")
        self.big_panel_btn.clicked.connect(self._open_big_panel)
        row1.addWidget(self.big_panel_btn)

        # 設定ボタン (歯車)
        self.settings_btn = QtWidgets.QPushButton("⚙")
        self.settings_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.settings_btn.setFixedSize(28, 28)
        self.settings_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #c0c0c0; "
            "border: 1px solid #3a3a3a; border-radius: 14px; "
            "font-size: 14px; }"
            f"QPushButton:hover {{ background-color: {self.theme.accent}; "
            f"border-color: {self.theme.accent}; color: #ffffff; }}"
        )
        self.settings_btn.setToolTip(
            "⚙ Settings  (left click)\n"
            "ℹ About  (right click)")
        self.settings_btn.clicked.connect(self._open_settings)
        self.settings_btn.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.settings_btn.customContextMenuRequested.connect(
            lambda _: self._show_about())
        row1.addWidget(self.settings_btn)

        # 音量スライダー
        self.volume_label = QtWidgets.QLabel("🔊")
        self.volume_label.setStyleSheet(
            "font-size: 13px; color: #c0c0c0; background: transparent;")
        row1.addWidget(self.volume_label)
        self.volume_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.volume_slider.setRange(0, 150)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.setFixedHeight(16)
        accent_hex = self.theme.accent
        self.volume_slider.setStyleSheet(
            "QSlider::groove:horizontal { background: #2a2a2c; "
            "height: 4px; border-radius: 2px; }"
            f"QSlider::sub-page:horizontal {{ background: {accent_hex}; "
            "border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {accent_hex}; "
            "width: 14px; height: 14px; border-radius: 7px; "
            "margin-top: -5px; margin-bottom: -5px; }}"
        )
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        self.volume_slider.setToolTip("Master volume (0–150%)")
        row1.addWidget(self.volume_slider)
        self.volume_val_label = QtWidgets.QLabel("100%")
        self.volume_val_label.setStyleSheet(
            "color: #c0c0c0; font-family: 'Consolas', monospace; "
            "font-size: 10px; background: transparent;")
        self.volume_val_label.setFixedWidth(36)
        row1.addWidget(self.volume_val_label)

        # オーディオレベルメータ
        self.audio_level_meter = _StatusMeter()
        self.audio_level_meter.setFixedWidth(80)
        self.audio_level_meter.setFixedHeight(20)
        self.audio_level_meter.set_accent(self.theme.accent)
        self.audio_level_meter.setToolTip("Audio output RMS level")
        row1.addWidget(self.audio_level_meter)

        # 出力スペクトル (EQ 効果が目で見える)
        self.audio_spectrum_bar = _OutputSpectrumBar()
        self.audio_spectrum_bar.set_accent(self.theme.accent)
        row1.addWidget(self.audio_spectrum_bar)

        # ストリーミングインジケータ
        self.status_label = QtWidgets.QLabel("● Connecting...")
        self.status_label.setStyleSheet(
            "font-size: 12px; color: #f39c12; font-weight: bold;"
            "margin-left: 8px;")
        self.status_label.setFixedWidth(130)
        self.status_label.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        row1.addWidget(self.status_label)

        outer.addLayout(row1)

        # ============ 段2: 設定 + ステータス詳細 ============
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(8)

        # ヒント (左端、控えめ)
        hint = QtWidgets.QLabel("⛶ 拡大  ·  ESC 戻る")
        hint.setStyleSheet(
            "font-size: 9px; color: #5a5a5a; letter-spacing: 0.5px;")
        row2.addWidget(hint)

        row2.addSpacing(20)

        small_combo = ("QComboBox { background-color: #1f1f22; color: #c0c0c0; "
                       "border: 1px solid #2f2f33; border-radius: 4px; "
                       "padding: 2px 7px; font-size: 10px; }"
                       "QComboBox:hover { border-color: #45454a; }")

        # Theme (hover preview 対応)
        row2.addWidget(self._small_label("Theme"))
        self.theme_selector = _PreviewComboBox(
            on_preview=lambda n: self.theme.set(n),
            on_commit=lambda n: self.theme.set(n),
            get_current=lambda: self.theme.name,
        )
        self.theme_selector.addItems(list(THEMES.keys()))
        self.theme_selector.setCurrentText(self.theme.name)
        self.theme_selector.setStyleSheet(small_combo
                                          + " QComboBox { min-width: 90px; }")
        row2.addWidget(self.theme_selector)

        # カスタム色ピッカー
        self.theme_color_btn = QtWidgets.QPushButton("🎨")
        self.theme_color_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.theme_color_btn.setFixedSize(22, 22)
        self.theme_color_btn.setStyleSheet(
            "QPushButton { background-color: transparent; "
            "color: #c0c0c0; border: 1px solid #3a3a3a; "
            "border-radius: 11px; font-size: 11px; }"
            "QPushButton:hover { border-color: #c0c0c0; }"
        )
        self.theme_color_btn.setToolTip("Pick custom accent color")
        self.theme_color_btn.clicked.connect(self._pick_custom_accent)
        row2.addWidget(self.theme_color_btn)

        # BG (hover preview 対応)
        row2.addWidget(self._small_label("BG"))
        self.bg_selector = _PreviewComboBox(
            on_preview=lambda n: self.theme.set_bg(n),
            on_commit=lambda n: self.theme.set_bg(n),
            get_current=lambda: self.theme.bg_name,
        )
        self.bg_selector.addItems(list(BG_PALETTES.keys()))
        self.bg_selector.setCurrentText(self.theme.bg_name)
        self.bg_selector.setStyleSheet(small_combo
                                       + " QComboBox { min-width: 100px; }")
        row2.addWidget(self.bg_selector)

        # Out デバイス
        row2.addWidget(self._small_label("Out"))
        self.audio_out_selector = QtWidgets.QComboBox()
        self.audio_out_selector.setStyleSheet(
            small_combo + " QComboBox { min-width: 180px; max-width: 260px; }")
        self.audio_out_selector.setToolTip("音声の出力先 (CABLE 系は除外済み)")
        self._audio_out_devices = []
        auto_idx, _ = pick_output_device()
        self.audio_out_selector.addItem("Auto", None)
        for idx, name, excluded in list_output_devices():
            if excluded:
                continue
            disp = f"{name[:34]}{'…' if len(name) > 34 else ''}"
            self.audio_out_selector.addItem(disp, idx)
            self._audio_out_devices.append((idx, name))
            if idx == auto_idx:
                self.audio_out_selector.setCurrentIndex(
                    self.audio_out_selector.count() - 1)
        row2.addWidget(self.audio_out_selector)

        row2.addStretch()

        # audio/rec ステータステキスト
        self.audio_status = QtWidgets.QLabel("")
        self.audio_status.setStyleSheet(
            "font-size: 10px; color: #8a8a8a;")
        self.audio_status.setFixedWidth(230)
        self.audio_status.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        row2.addWidget(self.audio_status)

        self.rec_status = QtWidgets.QLabel("")
        self.rec_status.setStyleSheet(
            "font-size: 10px; color: #8a8a8a;")
        self.rec_status.setFixedWidth(180)
        self.rec_status.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        row2.addWidget(self.rec_status)

        self.rate_label = QtWidgets.QLabel("— Hz")
        self.rate_label.setStyleSheet(
            "font-size: 10px; color: #8a8a8a;")
        self.rate_label.setFixedWidth(60)
        self.rate_label.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight)
        row2.addWidget(self.rate_label)

        outer.addLayout(row2)
        return w

    def _build_footer(self):
        """画面下端の薄いステータスバー. 常時表示で要点を伝える."""
        w = QtWidgets.QFrame()
        w.setObjectName("footer")
        w.setFixedHeight(28)
        w.setStyleSheet(
            "QFrame#footer { background-color: rgba(10, 10, 16, 200); "
            "border: 1px solid #2a2a2c; border-radius: 8px; }"
            "QLabel { background: transparent; border: none; }"
        )
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(16, 4, 16, 4)
        h.setSpacing(20)

        def _lbl(text, color="#9a9a9a", monospace=False, fixed_w=None):
            l = QtWidgets.QLabel(text)
            font_family = ("'Consolas', 'JetBrains Mono', monospace"
                           if monospace else "")
            l.setStyleSheet(
                f"color: {color}; font-size: 10px; "
                f"letter-spacing: 0.5px; "
                + (f"font-family: {font_family};" if font_family else ""))
            if fixed_w is not None:
                l.setFixedWidth(fixed_w)
            return l

        self._footer_mode = _lbl("🧠 STUDIO", fixed_w=110)
        self._footer_eng = _lbl("ENG: --", monospace=True, fixed_w=110)
        self._footer_aro = _lbl("ARO: --", monospace=True, fixed_w=110)
        self._footer_val = _lbl("VAL: --", monospace=True, fixed_w=110)
        self._footer_hr = _lbl("♥ --- BPM", color="#e74c3c",
                                monospace=True, fixed_w=130)
        self._footer_audio = _lbl("⚡ Audio: OFF", fixed_w=160)
        self._footer_clock = _lbl("--:--:--", monospace=True, fixed_w=80)
        self._footer_uptime = _lbl("⌛ 00:00", monospace=True, fixed_w=80)
        self._footer_hint = _lbl("F1 = Help", color="#5a5a5a")
        h.addWidget(self._footer_mode)
        h.addWidget(self._footer_eng)
        h.addWidget(self._footer_aro)
        h.addWidget(self._footer_val)
        h.addWidget(self._footer_hr)
        h.addWidget(self._footer_audio)
        h.addStretch()
        h.addWidget(self._footer_uptime)
        h.addWidget(self._footer_clock)
        h.addWidget(self._footer_hint)
        # アプリ起動時刻
        self._app_start_time = time.time()
        # HR フラッシュ用前回値
        self._prev_hr_displayed = -1
        self._hr_flash_until = 0.0
        return w

    def _small_label(self, text):
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet(
            "font-size: 10px; color: #8a8a8a; letter-spacing: 0.5px;"
            "margin-right: 2px;")
        return lbl

    def _restyle_header(self, w):
        """テーマ変更時にヘッダのネオン border / glow を accent 色に追従."""
        t = self.theme
        ar2 = QtGui.QColor(t.accent).red()
        ag2 = QtGui.QColor(t.accent).green()
        ab2 = QtGui.QColor(t.accent).blue()
        circuit_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets", "bg", "bg_circuit.png").replace("\\", "/")
        if os.path.exists(circuit_path):
            w.setStyleSheet(f"""
                QFrame#header {{
                    background-image: url("{circuit_path}");
                    background-position: center;
                    background-repeat: no-repeat;
                    border: 2px solid rgba({ar2}, {ag2}, {ab2}, 200);
                    border-radius: 12px;
                }}
                QLabel {{ border: none; background: transparent; }}
            """)
        else:
            w.setStyleSheet(f"""
                QFrame#header {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 #1a1a1c, stop:1 #1f1f23);
                    border: 2px solid rgba({ar2}, {ag2}, {ab2}, 200);
                    border-radius: 12px;
                }}
                QLabel {{ border: none; }}
            """)
        eff = w.graphicsEffect()
        if isinstance(eff, QtWidgets.QGraphicsDropShadowEffect):
            eff.setColor(QtGui.QColor(ar2, ag2, ab2, 180))

    def _build_eeg_plots(self):
        """Raw EEG をパーティクル付き波形で表示 (mockup 風)."""
        container = _ParticleEEG(CH_NAMES, CH_COLORS, WINDOW_SEC, BUF_LEN)
        curves = [_EEGCurveProxy(container, i) for i in range(len(CH_NAMES))]
        return container, curves

    def _build_spectrogram(self):
        """2D Spectrogram (rolling STFT)."""
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        sel_row = QtWidgets.QHBoxLayout()
        sel_lbl = QtWidgets.QLabel("Channel:")
        sel_lbl.setStyleSheet("font-size: 11px; color: #9a9a9a;")
        sel_row.addWidget(sel_lbl)
        ch_selector = QtWidgets.QComboBox()
        ch_selector.addItems(CH_NAMES)
        ch_selector.setCurrentIndex(1)
        combo_style = (
            "QComboBox { background-color: #18181a; border: 1px solid #3a3a3a; "
            "border-radius: 4px; padding: 3px 8px; color: #e0e0e0; "
            "font-size: 11px; }")
        ch_selector.setStyleSheet(combo_style)
        sel_row.addWidget(ch_selector)
        sel_row.addStretch()
        layout.addLayout(sel_row)

        plot = pg.PlotWidget()
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        plot.setLabel("left", "Frequency (Hz)")
        plot.setLabel("bottom", "← older      (newest at right) →")
        plot.setYRange(0, SPEC_FMAX)
        plot.setXRange(0, SPEC_COLS)
        plot.getAxis("bottom").setTicks([[]])
        img = pg.ImageItem()
        plot.addItem(img)
        n_freq_bins = int(SPEC_FMAX * SPEC_NPERSEG / FS) + 1
        data = np.full((SPEC_COLS, n_freq_bins), -8.0, dtype=np.float32)
        img.setImage(data, levels=(-6.0, 3.0), autoLevels=False)
        img.setRect(pg.QtCore.QRectF(0, 0, SPEC_COLS, SPEC_FMAX))
        img.setColorMap(make_jet_like_cmap())
        layout.addWidget(plot)
        # mode_selector は廃止 (None で返す)
        return container, img, data, ch_selector, None, plot

    def _build_band_plot(self):
        """光るスフィア × 5バンド (mockup 風)."""
        labels = ["δ", "θ", "α", "β", "γ"]
        container = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(container)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(2)
        gauges = {}
        for bi, band in enumerate(BAND_NAMES):
            g = _BandSphere(labels[bi], BAND_COLORS[bi])
            row.addWidget(g, 1)
            gauges[band] = g
        v.addLayout(row, 1)
        hint = QtWidgets.QLabel("δ Delta · θ Theta · α Alpha · β Beta · γ Gamma  "
                                "(4ch 平均, log power 正規化)")
        hint.setStyleSheet("font-size: 10px; color: #707070; "
                           "letter-spacing: 0.3px;")
        hint.setAlignment(QtCore.Qt.AlignCenter)
        v.addWidget(hint)
        return container, gauges

    def _build_quality_panel(self):
        """電極接触状態。絵文字＋テキスト説明＋電極位置"""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        # 説明文 (冒頭)
        intro = QtWidgets.QLabel(
            "各電極の接触品質。値は horseshoe (1=Good, 2=OK, 4=Bad)。\n"
            "Bad の電極は信号がノイズまみれなので感情推定から除外されます。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("font-size: 11px; color: #a0a0a0; line-height: 1.5;")
        v.addWidget(intro)

        sep1 = QtWidgets.QFrame()
        sep1.setFrameShape(QtWidgets.QFrame.HLine)
        sep1.setStyleSheet("color: #3a3a3a; background-color: #3a3a3a; max-height: 1px;")
        v.addWidget(sep1)

        # 各チャンネル行: [●] TP9 (左耳後ろ)  Good ─ 良好：クリーンな信号
        dots, texts, descs = {}, {}, {}
        for name in CH_NAMES:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(8)

            dot = QtWidgets.QLabel("●")
            dot.setStyleSheet("font-size: 22px; color: #e74c3c;")
            dot.setFixedWidth(20)
            row.addWidget(dot)

            nl = QtWidgets.QLabel(f"{name}")
            nl.setStyleSheet("font-size: 13px; color: #e0e0e0; font-weight: bold;")
            nl.setFixedWidth(40)
            row.addWidget(nl)

            loc = QtWidgets.QLabel(f"({CH_LOCATIONS[name]})")
            loc.setStyleSheet("font-size: 11px; color: #8a8a8a;")
            loc.setFixedWidth(90)
            row.addWidget(loc)

            status = QtWidgets.QLabel("Bad")
            status.setStyleSheet("font-size: 12px; color: #e74c3c; font-weight: bold;")
            status.setFixedWidth(50)
            row.addWidget(status)

            desc = QtWidgets.QLabel("—")
            desc.setStyleSheet("font-size: 11px; color: #a0a0a0;")
            row.addWidget(desc)

            row.addStretch()
            v.addLayout(row)
            dots[name] = dot
            texts[name] = status
            descs[name] = desc

        sep2 = QtWidgets.QFrame()
        sep2.setFrameShape(QtWidgets.QFrame.HLine)
        sep2.setStyleSheet("color: #3a3a3a; background-color: #3a3a3a; max-height: 1px;")
        v.addSpacing(4)
        v.addWidget(sep2)
        v.addSpacing(4)

        # アーティファクト検出行
        artifact_title = QtWidgets.QLabel("アーティファクト検出")
        artifact_title.setStyleSheet(
            "font-size: 11px; color: #8a8a8a; font-weight: bold;")
        v.addWidget(artifact_title)

        def make_row(label_text, tooltip):
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(8)
            icon = QtWidgets.QLabel("⚪")
            icon.setFixedWidth(20)
            row.addWidget(icon)
            lbl = QtWidgets.QLabel(label_text)
            lbl.setStyleSheet("font-size: 12px; color: #c0c0c0;")
            lbl.setFixedWidth(120)
            row.addWidget(lbl)
            status = QtWidgets.QLabel("—")
            status.setStyleSheet("font-size: 12px; color: #8a8a8a;")
            row.addWidget(status)
            desc = QtWidgets.QLabel(tooltip)
            desc.setStyleSheet("font-size: 11px; color: #707070; margin-left: 10px;")
            row.addWidget(desc)
            row.addStretch()
            return row, icon, status

        touching_row, self.t_icon, self.t_status = make_row(
            "額の接触", "ヘッドバンドが前頭に触れているか")
        v.addLayout(touching_row)
        blink_row, self.b_icon, self.b_status = make_row(
            "まばたき検出", "EEGにまばたきアーティファクトが入った瞬間")
        v.addLayout(blink_row)
        jaw_row, self.j_icon, self.j_status = make_row(
            "食いしばり検出", "筋電による信号汚染の検出")
        v.addLayout(jaw_row)

        v.addStretch()
        return w, dots, texts, descs, touching_row, blink_row, jaw_row

    def _build_hr_panel(self):
        """心拍 (PPG/fNIRS) 可視化パネル"""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        # BPM 表示 + 説明
        top = QtWidgets.QHBoxLayout()
        bpm_lbl = QtWidgets.QLabel("♥  — BPM")
        bpm_lbl.setStyleSheet(
            "font-size: 28px; font-weight: bold; color: #ff6b6b;")
        top.addWidget(bpm_lbl)
        status = QtWidgets.QLabel("センサー待機中…")
        status.setStyleSheet("font-size: 11px; color: #8a8a8a; margin-left: 20px;")
        top.addWidget(status)
        top.addStretch()
        v.addLayout(top)

        info = QtWidgets.QLabel(
            "Muse S Athena の光学センサー (fNIRS, 4ch) と PPG から心拍波形を取得。"
            "Red/IR LED による脳血流変化 (HbO₂/HbR) も将来的に活用可能。"
        )
        info.setWordWrap(True)
        info.setStyleSheet("font-size: 11px; color: #909090;")
        v.addWidget(info)

        # 波形プロット (光学4ch)
        gl = pg.GraphicsLayoutWidget()
        curves = []
        colors_ppg = ["#ff6b6b", "#ffa07a", "#4ecdc4", "#45b7d1"]
        for i in range(4):
            p = gl.addPlot(row=i, col=0)
            # ラベル text を短く. pyqtgraph の自動 scale suffix "(x0.00)" が
            # 縦軸でクリップするのを避けるため、単に "ch1".."ch4" のみに.
            p.setLabel("left", f"ch{i+1}", **{"color": colors_ppg[i],
                                              "font-size": "9pt"})
            p.showGrid(x=False, y=True, alpha=0.15)
            p.setMouseEnabled(x=False, y=False)
            p.hideButtons()
            # 軸の自動スケール suffix "(x10⁻³)" は短くて済むよう disable
            p.getAxis("left").enableAutoSIPrefix(False)
            p.getAxis("left").setWidth(36)
            if i < 3:
                p.hideAxis("bottom")
            else:
                p.setLabel("bottom", "samples (10s window)")
            pen = pg.mkPen(color=colors_ppg[i], width=1.3)
            c = p.plot(np.zeros(PPG_BUF_LEN), pen=pen)
            curves.append(c)
        v.addWidget(gl, 1)

        return w, curves, bpm_lbl, status

    def _build_emotion_panel(self):
        """感情推定パネル - Russell (Circumplex) のみ. 軽量化."""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # Russell ビューを直接組む
        russell_page = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(russell_page)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)

        intro = QtWidgets.QLabel(
            "Russell's Circumplex Model (Russell, 1980):\n"
            "感情を2次元で表現する心理学モデル。\n"
            "  • Arousal  (縦軸): 活性度 — EEG の β/α 比で推定\n"
            "  • Valence  (横軸): 快/不快 — 前頭 α 非対称 (FAA) で推定\n"
            "4象限で基本感情を分類: 興奮・ストレス・抑うつ・平静。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("font-size: 11px; color: #a0a0a0; line-height: 1.5;")
        rv.addWidget(intro)

        def metric_row(label_text, color):
            row_w = QtWidgets.QWidget()
            rvh = QtWidgets.QHBoxLayout(row_w)
            rvh.setContentsMargins(0, 0, 0, 0)
            l = QtWidgets.QLabel(label_text)
            l.setStyleSheet("font-size: 12px; color: #d0d0d0; font-weight: bold;")
            l.setFixedWidth(80)
            rvh.addWidget(l)
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(50)
            bar.setTextVisible(True)
            bar.setStyleSheet(progress_style(color, height=14))
            rvh.addWidget(bar)
            return row_w, bar

        a_row, a_bar = metric_row("Arousal", "#e74c3c")
        rv.addWidget(a_row)
        vv_row, v_bar = metric_row("Valence", "#2ecc71")
        rv.addWidget(vv_row)

        # 新しい Russell パッド (custom paintEvent)
        russell_pad = _RussellPad(theme=self.theme)
        rv.addWidget(russell_pad, 1)
        # API 互換のため scatter/curve は同じインスタンスを返す
        scatter = russell_pad
        curve = russell_pad
        # theme 変更時の再描画
        self.theme.subscribe(lambda *_: russell_pad.update())

        emo = QtWidgets.QLabel("—")
        emo.setAlignment(QtCore.Qt.AlignCenter)
        # 絵文字 + 半角スペース x 2 + 名前 (emoji と text の被りを防ぐ)
        emo.setStyleSheet(
            "font-family: 'Segoe UI Emoji', 'Segoe UI', sans-serif; "
            "font-size: 18px; font-weight: bold; color: #e8e8e8; "
            "padding: 8px 14px; background-color: #18181a; "
            "border-radius: 6px; letter-spacing: 1px;")
        emo.setMinimumHeight(40)
        rv.addWidget(emo)

        tips = QtWidgets.QLabel(
            "💡 Russell モデルから得られる有用な出力:\n"
            "  • 感情の軌跡 (どう遷移したか) ─ 上の白い軌跡\n"
            "  • 象限の滞在時間比率 (どの感情に多くいたか)\n"
            "  • 感情の安定性 (分散) / 急変検出\n"
            "  • ストレスタイム (象限=Stressed の累積秒)\n"
            "  • 平均ベクトル方向 (全体的な感情バイアス)"
        )
        tips.setWordWrap(True)
        tips.setStyleSheet(
            "font-size: 10px; color: #8a8a8a; background-color: #18181a; "
            "padding: 8px; border-radius: 4px; line-height: 1.4;")
        rv.addWidget(tips)
        # russell_page を直接配置 (stacked 不要)
        v.addWidget(russell_page, 1)
        return w, scatter, curve, a_bar, v_bar, emo, {}

    # ==================================================================
    # EQ カード (Phase 1-C): 6-band 楽器別フェーダ + Reverb + Auto/Manual
    # ==================================================================
    # インストゥルメント別 6 バンド:
    #   Drums / Bass / Mid / Vocals / High / Air
    # Manual: ユーザーがフェーダを直接操作
    # Auto:   ReflectController が EEG 感情から 6 バンド + Reverb を駆動
    #         音飛び対策として audio へのパラメータ push はスロットル済み.
    # ==================================================================
    def _build_eq_card(self):
        from audio_engine import BANDS, GAIN_MIN_DB, GAIN_MAX_DB, BAND_KEYS
        from eq_controllers import ReflectController
        self._band_keys = BAND_KEYS
        self._eq_range = (GAIN_MIN_DB, GAIN_MAX_DB)
        self._reflect_ctrl = ReflectController(
            self.audio, gain_max_db=GAIN_MAX_DB)

        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(8)

        # --- row1: View タブ + Auto / Manual + Reset ---
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(6)
        # View タブ: Mixer のみ (Sea は Watch モードに統合済み)
        self.eq_view_mixer_btn = QtWidgets.QPushButton("🎚 Mixer")
        self.eq_view_mixer_btn.setCheckable(True)
        self.eq_view_mixer_btn.setChecked(True)
        self.eq_view_mixer_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.eq_view_mixer_btn.setFixedHeight(28)
        self.eq_view_mixer_btn.clicked.connect(lambda: self._set_eq_view("mixer"))
        top.addWidget(self.eq_view_mixer_btn)
        # 旧 Sea ボタン参照を null に (古いコードからの参照を防ぐ)
        self.eq_view_sea_btn = None
        # セパレータ
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.VLine)
        sep.setStyleSheet("color: #3a3a3a;")
        top.addWidget(sep)
        # Auto / Manual
        self.eq_auto_btn = QtWidgets.QPushButton("🧠 Auto")
        self.eq_manual_btn = QtWidgets.QPushButton("✋ Manual")
        for b in (self.eq_auto_btn, self.eq_manual_btn):
            b.setCheckable(True)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setFixedHeight(28)
        self.eq_manual_btn.setChecked(True)
        self.eq_auto_btn.clicked.connect(lambda: self._set_eq_mode("auto"))
        self.eq_manual_btn.clicked.connect(lambda: self._set_eq_mode("manual"))
        top.addWidget(self.eq_auto_btn)
        top.addWidget(self.eq_manual_btn)
        top.addStretch()
        self.eq_reset_btn = QtWidgets.QPushButton("Reset")
        self.eq_reset_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.eq_reset_btn.setFixedHeight(26)
        self.eq_reset_btn.setStyleSheet(
            "QPushButton { background-color: #2b2b2b; color: #d0d0d0; "
            "border: 1px solid #3a3a3a; border-radius: 12px; "
            "padding: 2px 14px; font-size: 11px; }"
            "QPushButton:hover { background-color: #3a3a3a; }")
        self.eq_reset_btn.clicked.connect(self._eq_reset)
        top.addWidget(self.eq_reset_btn)
        v.addLayout(top)

        # --- QStackedWidget: Mixer / Sea ---
        self.eq_stack = QtWidgets.QStackedWidget()
        v.addWidget(self.eq_stack, 1)

        # --- Mixer ページ ---
        mixer_page = QtWidgets.QWidget()
        mv = QtWidgets.QVBoxLayout(mixer_page)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(8)

        # row2: 6 本の楽器フェーダ
        bands_ui = [(k, emo, name, freq, blurb)
                    for k, emo, name, freq, kind, q, blurb in BANDS]
        self.eq_bank = InstrumentFaderBank(
            self.theme, bands_ui, gain_max=GAIN_MAX_DB)
        self.eq_bank.band_changed.connect(self._on_fader_changed)
        mv.addWidget(self.eq_bank, 5)

        # --- row3: Reverb スライダー ---
        rv_row = QtWidgets.QHBoxLayout()
        rv_row.setSpacing(8)
        rv_label = QtWidgets.QLabel("🌊 空間 Reverb")
        rv_label.setStyleSheet("font-size: 11px; color: #c0c0c0; "
                               "font-weight: 600; min-width: 120px;")
        rv_row.addWidget(rv_label)
        self.eq_reverb_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.eq_reverb_slider.setRange(0, 100)
        self.eq_reverb_slider.setValue(0)
        self.eq_reverb_slider.setFixedHeight(20)
        self.eq_reverb_slider.valueChanged.connect(self._on_reverb_changed)
        rv_row.addWidget(self.eq_reverb_slider, 1)
        self.eq_reverb_val = QtWidgets.QLabel("0%")
        self.eq_reverb_val.setStyleSheet("font-size: 11px; color: #d0d0d0; "
                                         "min-width: 36px;")
        self.eq_reverb_val.setAlignment(QtCore.Qt.AlignRight)
        rv_row.addWidget(self.eq_reverb_val)
        mv.addLayout(rv_row)

        # --- row4: プリセット (楽器別) ---
        preset_defs = [
            ("Flat",          "⚪",  {}, 0.0),
            ("ボーカル前面",    "🎤",  {"vocal": 4.5, "mid": 2.0,
                                       "bass": -1.5}, 0.25),
            ("ドラム重視",     "🥁",  {"drums": 4.5, "bass": 3.0,
                                       "high": -1.5}, 0.2),
            ("ハイ楽器輝き",    "🎺",  {"high": 4.5, "vocal": 2.5,
                                       "bass": -1.5}, 0.35),
            ("空間広く",       "🌌",  {"air": 4.5, "mid": 1.5,
                                       "drums": -1.0}, 0.75),
            ("バンド強調",     "🎸",  {"drums": 3.0, "bass": 4.0,
                                      "mid": 3.0, "vocal": 2.5}, 0.3),
        ]
        preset_row = QtWidgets.QHBoxLayout()
        preset_row.setSpacing(6)
        self.eq_preset_btns = []
        for label, emoji, band_dict, reverb in preset_defs:
            btn = QtWidgets.QPushButton(f"{emoji} {label}")
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setFixedHeight(26)
            btn.setStyleSheet(self._preset_pill_style())
            btn.clicked.connect(
                lambda _, bd=band_dict, rv=reverb: self._apply_preset(bd, rv))
            preset_row.addWidget(btn)
            self.eq_preset_btns.append(btn)
        preset_row.addStretch()
        mv.addLayout(preset_row)

        # row5: 説明ボックス
        self.eq_explain = QtWidgets.QLabel()
        self.eq_explain.setWordWrap(True)
        self.eq_explain.setStyleSheet(
            "font-size: 10px; color: #a0a0a0; background-color: #161616; "
            "padding: 8px; border-radius: 4px; line-height: 1.35;")
        mv.addWidget(self.eq_explain)

        self.eq_stack.addWidget(mixer_page)   # index 0

        # --- Sea ページ ---
        if HAS_SEA:
            self.sea_widget = SeaWidget()
            self.eq_stack.addWidget(self.sea_widget)   # index 1
        else:
            self.sea_widget = None
            placeholder = QtWidgets.QLabel(
                "🌊 Sea ビュー使用不可\n" + _SEA_IMPORT_ERR)
            placeholder.setAlignment(QtCore.Qt.AlignCenter)
            placeholder.setStyleSheet(
                "color: #888; font-size: 13px; padding: 40px;")
            self.eq_stack.addWidget(placeholder)

        self._eq_view = "mixer"
        self._eq_mode = "manual"
        self._update_eq_mode_btn_style()
        self._update_eq_view_btn_style()
        self._eq_update_explain()
        return w

    # ---- ハンドラ (Manual) ----
    def _on_fader_changed(self, key, db):
        if self._eq_mode != "manual":
            return
        self.audio.set_band(key, db)
        self._eq_update_explain()

    def _on_listen_circle_bump(self, key, delta_db):
        """Listen の楽器サークルを上下クリックで EQ ±0.5dB.
        Auto モード時は Manual に戻してから適用.
        """
        if self._eq_mode == "auto":
            self._set_eq_mode("manual")
        cur = self.audio.get_bands().get(key, 0.0)
        new_db = max(-GAIN_MAX_DB, min(GAIN_MAX_DB, cur + float(delta_db)))
        self.audio.set_band(key, new_db)
        # フェーダ UI も同期
        if hasattr(self, "eq_bank") and key in self.eq_bank.faders:
            self.eq_bank.faders[key].set_value(new_db, emit=False)
        self._eq_update_explain()
        # Toast で結果フィードバック
        if hasattr(self, "_show_toast"):
            arrow = "▲" if delta_db > 0 else "▼"
            self._show_toast(f"{arrow}  {key.upper()}  {new_db:+.1f} dB")

    def _on_reverb_changed(self, pct):
        if self._eq_mode != "manual":
            # Auto 中はコントローラが支配
            return
        wet = pct / 100.0
        self.audio.set_reverb_wet(wet)
        self.eq_reverb_val.setText(f"{pct}%")
        self._eq_update_explain()

    def _apply_preset(self, band_dict, reverb_wet):
        """プリセット適用. Auto なら Manual に戻してから適用."""
        if self._eq_mode == "auto":
            self._set_eq_mode("manual")
        vals = {k: 0.0 for k in self._band_keys}
        vals.update(band_dict)
        self.eq_bank.set_values(vals, emit=False)
        self.audio.set_bands(**vals)
        pct = int(round(reverb_wet * 100))
        self.eq_reverb_slider.blockSignals(True)
        self.eq_reverb_slider.setValue(pct)
        self.eq_reverb_slider.blockSignals(False)
        self.audio.set_reverb_wet(reverb_wet)
        self.eq_reverb_val.setText(f"{pct}%")
        self._eq_update_explain()

    def _preset_pill_style(self):
        return ("QPushButton { background-color: #242424; color: #c0c0c0; "
                f"border: 1px solid {BORDER}; border-radius: 13px; "
                "padding: 2px 12px; font-size: 11px; }"
                "QPushButton:hover { background-color: #2f2f2f; "
                "color: #ffffff; border-color: #444; }")

    def _eq_reset(self):
        self.audio.reset()
        self.eq_bank.reset(emit=False)
        self.eq_reverb_slider.blockSignals(True)
        self.eq_reverb_slider.setValue(0)
        self.eq_reverb_slider.blockSignals(False)
        self.eq_reverb_val.setText("0%")
        # Auto の内部状態もゼロに
        self._reflect_ctrl.seed_from_audio()
        self._eq_update_explain()

    # ---- Auto/Manual 切替 ----
    def _update_eq_mode_btn_style(self):
        def style(active, accent):
            if active:
                return (f"QPushButton {{ background-color: {accent}; "
                        f"color: #ffffff; border: 1px solid {accent}; "
                        f"border-radius: 14px; padding: 3px 14px; "
                        f"font-size: 12px; font-weight: bold; }}")
            return ("QPushButton { background-color: #2b2b2b; color: #9a9a9a; "
                    "border: 1px solid #3a3a3a; border-radius: 14px; "
                    "padding: 3px 14px; font-size: 12px; }"
                    "QPushButton:hover { background-color: #3a3a3a; color: #e0e0e0; }")
        self.eq_auto_btn.setStyleSheet(
            style(self.eq_auto_btn.isChecked(), self.theme.accent))
        self.eq_manual_btn.setStyleSheet(
            style(self.eq_manual_btn.isChecked(), self.theme.accent))

    def _set_eq_mode(self, mode):
        if mode == "auto":
            self.eq_auto_btn.setChecked(True)
            self.eq_manual_btn.setChecked(False)
            self._eq_mode = "auto"
            self.eq_bank.set_interactive(False)
            self.eq_reverb_slider.setEnabled(False)
            self._reflect_ctrl.seed_from_audio()
        else:
            self.eq_auto_btn.setChecked(False)
            self.eq_manual_btn.setChecked(True)
            self._eq_mode = "manual"
            self.eq_bank.set_interactive(True)
            self.eq_reverb_slider.setEnabled(True)
            self.eq_bank.clear_ghosts()
        self._update_eq_mode_btn_style()
        self._eq_update_explain()

    # ---- View (Mixer / Sea) 切替 ----
    def _update_eq_view_btn_style(self):
        accent = self.theme.accent
        self.eq_view_mixer_btn.setStyleSheet(
            f"QPushButton {{ background-color: {accent}; "
            f"color: #ffffff; border: 1px solid {accent}; "
            f"border-radius: 14px; padding: 3px 14px; "
            f"font-size: 12px; font-weight: bold; }}")

    def _set_eq_view(self, view):
        # Sea ビューは Watch モードに統合済み. Mixer のみ.
        self._eq_view = "mixer"
        self.eq_view_mixer_btn.setChecked(True)
        self.eq_stack.setCurrentIndex(0)
        self._update_eq_view_btn_style()

    # ---- Sea ビュー駆動 (Mixer / Sea どちらでも毎フレーム呼ぶ) ----
    def _update_sea_state(self, rus, eng, quality, hr_bpm_osc, last_ppg, now,
                          force=False):
        if not HAS_SEA or self.sea_widget is None:
            return
        # Sea が非表示のときは負荷軽減のため skip
        # Sea が見えてる時だけ更新: EQ Sea タブ表示中 OR Watch モード時
        in_watch = getattr(self, "_mode", "studio") == "watch"
        in_eq_sea = (self._eq_view == "sea"
                     and getattr(self, "_mode", "studio") == "studio")
        if not (in_watch or in_eq_sea or force):
            return
        # HSI: quality (1=Good, 2=OK, 4=Bad) を 0..1 に
        if quality:
            q_mean = sum(quality) / len(quality)
            hsi = max(0.0, min(1.0, (4.0 - q_mean) / 3.0))
        else:
            hsi = 1.0
        # PPG 鮮度: 3秒以内なら 1.0, それ以降減衰
        dt_ppg = now - last_ppg if last_ppg else 999
        if dt_ppg < 3.0:
            fresh = 1.0
        elif dt_ppg < 8.0:
            fresh = 1.0 - (dt_ppg - 3.0) / 5.0
        else:
            fresh = 0.0
        # HR (0 or <=20 は無効化として None を渡す)
        hr = hr_bpm_osc if hr_bpm_osc and hr_bpm_osc > 20 else None
        self.sea_widget.set_state(
            arousal=rus.get("arousal"),
            valence=rus.get("valence"),
            engagement=eng,
            hr_bpm=hr,
            hsi=hsi,
            signal_fresh=fresh,
        )

    # ---- Auto 毎フレーム駆動 ----
    def _eq_auto_tick(self, rus, eng):
        if self._eq_mode != "auto":
            return
        # ReflectController が内部でスロットルして audio に push.
        self._reflect_ctrl.tick(rus, eng)
        cur = self._reflect_ctrl.current()
        tgt = self._reflect_ctrl.target()
        # フェーダは低頻度で追従表示 (描画負荷軽減のため毎フレーム上書きはしない)
        self.eq_bank.set_values(
            {k: cur[k] for k in self._band_keys}, emit=False)
        self.eq_bank.set_ghosts(
            {k: tgt[k] for k in self._band_keys}, show=True)
        # Reverb スライダーも追従
        pct = int(round(cur["reverb"] * 100))
        self.eq_reverb_slider.blockSignals(True)
        self.eq_reverb_slider.setValue(pct)
        self.eq_reverb_slider.blockSignals(False)
        self.eq_reverb_val.setText(f"{pct}%")
        self._eq_update_explain()

    # ---- 説明 ----
    def _eq_update_explain(self):
        from audio_engine import BANDS
        bands = self.audio.get_bands()
        reverb = self.audio.get_reverb_wet()
        # ハイライトされているバンド (|dB|>=0.5) を列挙
        hot = [(k, bands[k]) for k in self._band_keys
               if abs(bands[k]) >= 0.5]
        hot.sort(key=lambda kv: -abs(kv[1]))
        name_by_key = {b[0]: f"{b[1]} {b[2]}" for b in BANDS}
        hot_txt = " · ".join(f"{name_by_key[k]} {v:+.1f}"
                             for k, v in hot[:3])
        if not hot_txt:
            hot_txt = "（全バンドフラット）"

        if self._eq_mode == "auto":
            msg = ("🧠 <b>Auto</b> — EEG 感情からバンドを自動追従 "
                   f"(EMA τ≈3s)<br>いま強調中: {hot_txt}<br>"
                   f"🌊 Reverb {int(reverb * 100)}%")
        else:
            msg = (f"✋ <b>Manual</b> — フェーダを上下ドラッグで増減, "
                   f"ダブルクリックで 0 dB, ホイールで ±0.2 dB<br>"
                   f"いま強調中: {hot_txt}<br>"
                   f"🌊 Reverb {int(reverb * 100)}%")
            if not self.audio.running:
                msg += ("<br><span style='color:#e67e22'>"
                        "※ ♪ Audio ON にすると実際に音に反映されます</span>")
        self.eq_explain.setText(msg)

    # ---- テーマ変更 ----
    def _apply_global_style(self):
        t = self.theme
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {t.bg_deep}; color: {t.text_main};
                font-family: 'Segoe UI', 'Meiryo', sans-serif; font-size: 12px;
            }}
            QLabel {{ color: {t.text_main}; }}
            QToolTip {{
                background-color: rgba(8, 8, 14, 235);
                color: {t.text_main};
                border: 1px solid {t.accent};
                border-radius: 6px;
                padding: 8px 12px; font-size: 11px;
            }}
            QComboBox {{
                background-color: {t.bg_panel}; color: {t.text_main};
                border: 1px solid {t.border}; border-radius: 6px;
                padding: 4px 10px; font-size: 11px;
            }}
            QComboBox:hover {{ border-color: {t.border_hover}; }}
            QComboBox::drop-down {{ border: none; width: 20px; }}
            QComboBox QAbstractItemView {{
                background-color: {t.bg_panel}; color: {t.text_main};
                border: 1px solid {t.border_hover};
                selection-background-color: {t.border_hover}; outline: 0;
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                background: {t.bg_deep}; border: none;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background: {t.border_hover}; border-radius: 4px; min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
                background: {t.border_hover};
            }}
            QScrollBar::add-line, QScrollBar::sub-line {{
                background: none; border: none; height: 0; width: 0;
            }}
        """)
        pg.setConfigOption("background", t.bg_deep)
        pg.setConfigOption("foreground", t.text_dim)
        pg.setConfigOption("antialias", True)

    def _on_theme_changed(self, theme):
        self._apply_global_style()
        self._update_eq_mode_btn_style()
        if hasattr(self, "_update_eq_view_btn_style"):
            self._update_eq_view_btn_style()
        if hasattr(self, "mode_btns"):
            self._update_mode_btn_style()
        if hasattr(self, "_watch_hud"):
            self._restyle_watch_hud(self._watch_hud)
        if hasattr(self, "_watch_mandala"):
            self._watch_mandala.set_accent(self.theme.accent)
        if hasattr(self, "_watch_tron"):
            self._watch_tron.set_accent(self.theme.accent)
        if hasattr(self, "_watch_matrix"):
            self._watch_matrix.set_accent(self.theme.accent)
        if hasattr(self, "_watch_cursor_particles"):
            self._watch_cursor_particles.set_accent(self.theme.accent)
        if hasattr(self, "audio_spectrum_bar"):
            self.audio_spectrum_bar.set_accent(self.theme.accent)
        # ヘッダ neon border + title glow を accent 色に
        if hasattr(self, "_header_frame"):
            self._restyle_header(self._header_frame)
        if hasattr(self, "_header_title_glow"):
            arT, agT, abT = (QtGui.QColor(self.theme.accent).red(),
                              QtGui.QColor(self.theme.accent).green(),
                              QtGui.QColor(self.theme.accent).blue())
            self._header_title_glow.setColor(
                QtGui.QColor(arT, agT, abT, 220))
        if hasattr(self, "listen_page"):
            self._restyle_listen_page(self.listen_page)
        if hasattr(self, "eq_bank"):
            for f in self.eq_bank.faders.values():
                f.update()

    MAX_TOASTS = 3

    def _show_toast(self, text, accent=None, duration_ms=2800):
        """画面右下に通知トースト表示.
        - 同じ text が既に出てる場合: 古いほうを即消して上書き (連打対応)
        - 最大 MAX_TOASTS 個まで. 超えたら一番古いのを消す.
        """
        if accent is None:
            accent = self.theme.accent
        if not hasattr(self, "_active_toasts"):
            self._active_toasts = []

        # 削除済み除去 + 重複 text を即消す
        clean = []
        for t in self._active_toasts:
            try:
                if t.parent() is None or not t.isVisible():
                    continue
                if getattr(t, "_text", "") == text:
                    # 同じ内容 → 古いものを即消す
                    t.hide()
                    t.deleteLater()
                    continue
                clean.append(t)
            except RuntimeError:
                pass
        self._active_toasts = clean

        # 最大個数を超えていたら古いほうから捨てる
        while len(self._active_toasts) >= self.MAX_TOASTS:
            old = self._active_toasts.pop(0)
            try:
                old.hide()
                old.deleteLater()
            except RuntimeError:
                pass

        toast = _Toast(self, text, accent=accent, duration_ms=duration_ms)
        toast.show()
        toast.raise_()
        toast.adjustSize()

        # 位置決定: 右下、既存トーストの上に積む
        margin = 20
        y_offset = margin
        for t in self._active_toasts:
            try:
                y_offset += t.height() + 8
            except RuntimeError:
                pass
        x = self.width() - toast.width() - margin
        y = self.height() - toast.height() - y_offset
        toast.move(x, y)
        self._active_toasts.append(toast)

    def _rec_pulse_tick(self):
        """REC ボタンを録画中に呼吸 + 経過時間表示."""
        import math as _m
        if not self.recording:
            return
        self._rec_pulse_phase = (self._rec_pulse_phase + 0.15) % (2 * _m.pi)
        pulse = 0.55 + 0.45 * (0.5 + 0.5 * _m.sin(self._rec_pulse_phase))
        r = int(231 * pulse + 80 * (1 - pulse))
        g = int(76 * pulse + 20 * (1 - pulse))
        b = int(60 * pulse + 20 * (1 - pulse))
        # 経過時間 mm:ss
        try:
            elapsed = max(0, int(time.time() - self.rec_start_time))
        except Exception:
            elapsed = 0
        mm, ss = divmod(elapsed, 60)
        self.rec_btn.setText(f"● REC  {mm:02d}:{ss:02d}")
        self.rec_btn.setStyleSheet(
            f"QPushButton {{ background-color: rgb({r},{g},{b}); "
            f"color: #ffffff; border: 1px solid rgb({r},{g},{b}); "
            "border-radius: 13px; padding: 3px 14px; "
            "font-size: 11px; font-weight: 600; "
            "font-family: 'Consolas', monospace; }}"
        )

    def _key_num(self, n):
        """1/2/3/4 キーの context-aware 処理.
        Watch モード: 1=Surface (EEG駆動) / 2=Underwater (HR駆動).
                     3/4 は廃止 (City/Forest を 2026-05 に削除).
        その他: 1=Studio / 2=Listen / 3=Watch.
        """
        if getattr(self, "_mode", "studio") == "watch":
            sea = getattr(self, "sea_widget", None)
            if sea is None:
                return
            # Watch は 2 ビュー構成. 駆動源を toast に明示する.
            sub_map = {1: ("surface", "🌊 Surface  ·  🧠 EEG-driven"),
                       2: ("underwater", "🌊 Underwater  ·  ♥ HR-driven")}
            if n in sub_map:
                key, label = sub_map[n]
                sea.set_sub_view(key)
                # ボタン UI も同期
                if key in getattr(sea, "_sub_btns", {}):
                    for k, b in sea._sub_btns.items():
                        b.setChecked(k == key)
                    if hasattr(sea, "_restyle_sub_btns"):
                        sea._restyle_sub_btns()
                # δθαβγ 弧は Surface (EEG) でのみ表示
                if hasattr(self, "_watch_mandala"):
                    self._watch_mandala.set_show_band_arc(key == "surface")
                self._show_toast(label)
            elif n in (3, 4):
                self._show_toast(
                    "Removed — Watch is now 2 views (Surface / Underwater)")
        else:
            mode_map = {1: "studio", 2: "listen", 3: "watch"}
            if n in mode_map:
                self._set_mode(mode_map[n])

    def _audio_pulse_tick(self):
        """Audio ON 中、ボタンに緩い呼吸 glow."""
        import math as _m
        if not getattr(self, "audio", None) or not self.audio.running:
            return
        self._audio_pulse_phase = (self._audio_pulse_phase + 0.08) % (2 * _m.pi)
        # DropShadow 強度を呼吸させる
        eff = self.audio_btn.graphicsEffect()
        if not isinstance(eff, QtWidgets.QGraphicsDropShadowEffect):
            eff = QtWidgets.QGraphicsDropShadowEffect(self.audio_btn)
            self.audio_btn.setGraphicsEffect(eff)
        pulse = 0.4 + 0.6 * (0.5 + 0.5 * _m.sin(self._audio_pulse_phase))
        ar = QtGui.QColor(self.theme.accent).red()
        ag = QtGui.QColor(self.theme.accent).green()
        ab = QtGui.QColor(self.theme.accent).blue()
        eff.setColor(QtGui.QColor(ar, ag, ab, int(220 * pulse)))
        eff.setBlurRadius(12 + 14 * pulse)
        eff.setOffset(0, 0)

    def _toggle_replay(self):
        """CSV リプレイの開始/停止. 再生中なら停止."""
        if self._replay_state is not None:
            self._stop_replay()
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open CSV recording", str(
                __import__("pathlib").Path.cwd() / "recordings"),
            "CSV files (*.csv)")
        if not path:
            return
        try:
            import csv as _csv
            rows = []
            with open(path, "r", encoding="utf-8") as f:
                reader = _csv.DictReader(f)
                for r in reader:
                    rows.append(r)
            if not rows:
                self._show_toast("⚠ Empty CSV", accent="#e74c3c")
                return
            self._replay_state = {
                "data": rows,
                "idx": 0,
                "start_t": time.time(),
                "first_ts": float(rows[0].get("time_s", "0") or 0),
                "path": path,
            }
            self.replay_btn.setText("■ Stop Replay")
            self.replay_btn.setStyleSheet(
                f"QPushButton {{ background-color: {self.theme.accent}; "
                f"color: #000; border: 1px solid {self.theme.accent}; "
                "border-radius: 13px; padding: 3px 14px; "
                "font-size: 11px; font-weight: 600; }}")
            # 1 つだけの再生 timer
            if not hasattr(self, "_replay_timer"):
                self._replay_timer = QtCore.QTimer(self)
                self._replay_timer.timeout.connect(self._replay_tick)
            self._replay_timer.start(33)
            import os as _os
            self._show_toast(
                f"▶ Replay: {_os.path.basename(path)} ({len(rows)} rows)")
        except Exception as e:
            self._show_toast(f"⚠ Replay failed: {e}", accent="#e74c3c")

    def _stop_replay(self):
        if hasattr(self, "_replay_timer"):
            self._replay_timer.stop()
        self._replay_state = None
        self.replay_btn.setText("▶ Replay")
        self.replay_btn.setStyleSheet(
            "QPushButton { background-color: #1e1e20; color: #b0b0b0; "
            "border: 1px solid #303033; border-radius: 13px; "
            "padding: 3px 14px; font-size: 11px; font-weight: 600; }"
            "QPushButton:hover { background-color: #2a2a2c; color: #e8e8e8; "
            "border-color: #45454a; }"
        )
        self._show_toast("■ Replay stopped")

    def _replay_tick(self):
        """毎フレーム呼ばれ, CSV の経過時刻に対応する行を state に注入."""
        rs = self._replay_state
        if rs is None:
            return
        elapsed = time.time() - rs["start_t"]
        target_ts = rs["first_ts"] + elapsed
        rows = rs["data"]
        # 現在 idx から先に進める
        i = rs["idx"]
        while i + 1 < len(rows):
            try:
                t = float(rows[i + 1].get("time_s", "0") or 0)
            except Exception:
                t = 0
            if t > target_ts:
                break
            i += 1
        rs["idx"] = i
        # 最終行に到達したらループ
        if i >= len(rows) - 1:
            rs["start_t"] = time.time()
            rs["idx"] = 0
            return
        # 現在行を state に注入
        row = rows[i]
        try:
            # EEG ch ごとの最新サンプルを補完するのは難しいので,
            # bands, quality, HR, arousal/valence/eng を inject するのみ
            with state.lock:
                for bi, band in enumerate(BAND_NAMES):
                    for ch in range(4):
                        col = f"band_{band}_{ch}"
                        if col in row:
                            state.bands[band][ch] = float(row[col])
                for ch in range(4):
                    col = f"quality_{ch}"
                    if col in row:
                        state.quality[ch] = float(row[col])
                if "hr_osc" in row:
                    try:
                        state.heart_rate = float(row["hr_osc"])
                    except Exception:
                        pass
                state.last_eeg_time = time.time()
                state.msg_count = 6
        except Exception:
            pass

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self._show_toast("⛶ Windowed")
        else:
            self.showFullScreen()
            self._show_toast("⛶ Fullscreen (F11)")

    # ============ Demo mode (review without a headset) ============
    def _watch_toggle_demo(self):
        """ON/OFF を切り替えるトグル. ON 中は EEG/HR を模擬値で動かす."""
        if not self._watch_demo_active:
            self._watch_demo_active = True
            self._watch_demo_t0 = time.monotonic()
            self._watch_demo_btn.setChecked(True)
            self._watch_demo_btn.setText("■ Demo")
            self._watch_demo_timer.start(80)   # 12.5 Hz
            if hasattr(self, "_watch_demo_panel"):
                self._watch_demo_panel.setVisible(True)
                self._watch_demo_panel.raise_()
            self._show_toast(
                "▶  Demo mode ON — EEG/HR を模擬値で循環します")
        else:
            self._watch_demo_active = False
            self._watch_demo_btn.setChecked(False)
            self._watch_demo_btn.setText("▶ Demo")
            self._watch_demo_btn.setFixedSize(76, 36)   # 元のサイズに戻す
            self._watch_demo_timer.stop()
            if hasattr(self, "_watch_demo_panel"):
                self._watch_demo_panel.setVisible(False)
            self._show_toast("■  Demo mode OFF")

    def _watch_demo_tick(self):
        """デモモード: 60 秒で 1 サイクル.

        Phase A (0-30s, Surface):
          - EEG (Arousal/Valence/Engagement) を**連続的に**滑らかに変化
          - Calm baseline → 嵐 (Storm) ピーク へ smoothstep で遷移
          - 終端: A=0.90, V=0.20, E=0.70 (高覚醒・負 valence = 嵐)
          - 値は「実際に music で到達可能なレンジ」を想定:
              通常リスニング: A=0.2-0.7 / V=0.3-0.8 / HR=55-85 BPM
              嵐状態:         A>0.85 / V<0.3 (ドラマ/激しい曲で到達)

        Phase B (30-60s, Underwater):
          - HR を**連続的に**変化 (60 → 110 → 60 BPM, sin arc)
          - LOW → MID → HIGH → MID → LOW の全 zone を巡回
          - 通常: 60-85 BPM 安定 / アップテンポ曲: 90-110 BPM 到達

        SeaWidget と Mandala に直接注入. State.* には触らない (録画汚染防止).
        """
        if not self._watch_demo_active:
            return
        import math as _m
        t = (time.monotonic() - self._watch_demo_t0) % 60.0

        if t < 30.0:
            # ============ Phase A: Surface — EEG 連続変化 ============
            target_sub = "surface"
            u = t / 30.0   # 0..1
            # 0→1 で滑らかな ease in-out (smoothstep)
            s = u * u * (3.0 - 2.0 * u)

            # 開始 (Calm baseline)  →  終端 (やや嵐寄り、抑えめ)
            #   Arousal:    0.30  →  0.72   (覚醒↑、極端は避ける)
            #   Valence:    0.62  →  0.32   (快→不快、ストレス手前)
            #   Engagement: 0.42  →  0.62   (集中↑)
            # 実音楽リスニングで到達可能なリアル寄りレンジ.
            arousal = 0.30 + 0.42 * s + 0.025 * _m.sin(t * 0.7)
            valence = 0.62 - 0.30 * s + 0.020 * _m.sin(t * 0.5 + 1.2)
            engagement = 0.42 + 0.20 * s + 0.025 * _m.sin(t * 0.4)
            # クランプ
            arousal = max(0.0, min(1.0, arousal))
            valence = max(0.0, min(1.0, valence))
            engagement = max(0.0, min(1.0, engagement))
            # HR は EEG に弱く連動 (副次的・Surface では使われない)
            hr = 65.0 + 15.0 * s + 1.5 * _m.sin(t * 0.8)
            # phase ラベル
            phase_label = ("CALM" if u < 0.30 else
                           "RISING" if u < 0.65 else
                           "INTENSE" if u < 0.90 else "STORMY")

        else:
            # ============ Phase B: Underwater — HR 連続変化 ============
            # 全 3 シーン (LOW=浅瀬 / MID=魚群 / HIGH=ジンベエザメ) を
            # 確実に表示するための台形カーブ.
            # sea_widget.py の閾値:
            #   HR_HIGH_ENTER=92 / HR_HIGH_EXIT=82
            #   HR_MID_ENTER=75  / HR_MID_EXIT=65
            #   HR_MIN_DWELL_SEC=6  / CROSSFADE_SEC=2.5
            # → HIGH を「2.5(in) + 6+(visible) + 2.5(out)」で約 13s 滞在.
            #
            #   0- 3s  LOW    (60 BPM, ベースライン)
            #   3- 9s  rise   (60 → 102, 全ゾーンを通過)
            #   9-22s  HIGH   (100 ± 3, ジンベエ シーン 13秒固定)
            #  22-28s  descend(100 → 62, HIGH→MID→LOW 逆順通過)
            #  28-30s  LOW    (60 BPM, クールダウン)
            target_sub = "underwater"
            local_t = t - 30.0   # 0..30
            u = local_t / 30.0   # 0..1

            if local_t < 3.0:
                # ベースライン LOW
                hr = 60.0 + 0.8 * _m.sin(local_t * 0.5)
            elif local_t < 9.0:
                # smoothstep で 60 → 102
                p = (local_t - 3.0) / 6.0
                s_hr = p * p * (3.0 - 2.0 * p)
                hr = 60.0 + 42.0 * s_hr + 0.6 * _m.sin(local_t * 0.6)
            elif local_t < 22.0:
                # HIGH プラトー (常に >92, 微振動だけ)
                hr = 100.0 + 2.8 * _m.sin((local_t - 9.0) * 0.55)
            elif local_t < 28.0:
                # smoothstep で 100 → 62 (HIGH→MID→LOW を順に通過)
                p = (local_t - 22.0) / 6.0
                s_hr = p * p * (3.0 - 2.0 * p)
                hr = 100.0 - 38.0 * s_hr + 0.6 * _m.sin(local_t * 0.6)
            else:
                # クールダウン LOW
                hr = 62.0 + 0.8 * _m.sin(local_t * 0.5)

            hr = max(55.0, min(110.0, hr))
            # Underwater では EEG はベースライン (心拍主導なので)
            arousal = 0.48 + 0.04 * _m.sin(local_t * 0.3)
            valence = 0.55 + 0.04 * _m.sin(local_t * 0.25)
            engagement = 0.45 + 0.04 * _m.sin(local_t * 0.4)
            # phase ラベル
            zone = ("LOW" if hr < 75 else "MID" if hr < 90 else "HIGH")
            phase_label = f"♥ {int(hr)} BPM · {zone}"

        # --- Sea widget に注入 ---
        sea = getattr(self, "sea_widget", None)
        if sea is not None:
            sea.set_state(arousal=arousal, valence=valence,
                          engagement=engagement,
                          hr_bpm=hr, hsi=1.0, signal_fresh=1.0)
            # サブビュー切替 (phase 境界で 1 回だけ)
            cur_sub = getattr(sea, "_sub_view", None)
            if cur_sub != target_sub:
                sea.set_sub_view(target_sub)
                if target_sub in getattr(sea, "_sub_btns", {}):
                    for k, b in sea._sub_btns.items():
                        b.setChecked(k == target_sub)
                    if hasattr(sea, "_restyle_sub_btns"):
                        sea._restyle_sub_btns()
                if hasattr(self, "_watch_mandala"):
                    self._watch_mandala.set_show_band_arc(
                        target_sub == "surface")
                # 切替時に toast (phase 名を出す)
                if hasattr(self, "_show_toast"):
                    title = ("🧠 EEG-driven · Surface"
                             if target_sub == "surface"
                             else "♥ HR-driven · Underwater")
                    self._show_toast(title)

        # phase ラベルを demo ボタンに表示 (常時更新)
        if hasattr(self, "_watch_demo_btn"):
            self._watch_demo_btn.setText(f"■ {phase_label}")
            # ボタン幅を内容に合わせる
            self._watch_demo_btn.adjustSize()
            min_w = self._watch_demo_btn.sizeHint().width() + 12
            self._watch_demo_btn.setFixedSize(
                max(110, min_w), 36)

        # 説明文 (phase 別)
        if t < 30.0:
            if u < 0.30:
                explanation = (
                    "Calm baseline. アコースティック・アンビエント等を聴いている状態. "
                    "Arousal/Engagement 低、Valence 中庸. 海面は穏やかな朝.")
            elif u < 0.65:
                explanation = (
                    "Rising. 曲が盛り上がり中. β/α 比が上がり Arousal ↑, "
                    "前頭 α 非対称が左寄りに動き Valence ↓. シーンは Golden へ.")
            elif u < 0.90:
                explanation = (
                    "Intense. ロック / EDM 全開. Arousal ≈ 0.7 まで上昇, "
                    "Engagement も高めで集中. 海面に波が立ち始める.")
            else:
                explanation = (
                    "Stormy. ドラマチック / アグレッシブな曲で頂点. "
                    "Storm シーン発動条件 (A>0.6 ∧ V<0.4) を満たし、海面が荒れる.")
        else:
            local_t = t - 30.0
            if local_t < 3.0:
                explanation = (
                    "ベースライン LOW (60 BPM). スロー曲 / 瞑想音楽で安静中. "
                    "光あふれる浅瀬を漂う.")
            elif local_t < 9.0:
                explanation = (
                    "心拍が上昇中 (60 → 100 BPM). アップテンポへ移行. "
                    "LOW → MID → HIGH と全ゾーンを順に通過. 視界が深くなる.")
            elif local_t < 22.0:
                explanation = (
                    "HIGH zone 維持 (≈100 BPM). 激しい曲 + 軽い身体活動. "
                    "深層: 🐋 ジンベエザメ + マンタが視界に入る最深シーン. "
                    "(6 秒以上 dwell して見せ場を作る)")
            elif local_t < 28.0:
                explanation = (
                    "クールダウン中 (100 → 62 BPM). 曲が落ち着き心拍も下降. "
                    "HIGH → MID → LOW を逆順に通過して浅瀬へ戻る.")
            else:
                explanation = (
                    "LOW zone へ着地. 1 サイクル完了. "
                    "次は Surface 側 (脳波駆動) からループ再開.")

        # 説明パネルに反映
        if hasattr(self, "_watch_demo_panel") and \
                self._watch_demo_panel.isVisible():
            self._watch_demo_panel.set_state(
                arousal=arousal, valence=valence,
                engagement=engagement, hr=hr,
                phase=phase_label, sub_view=target_sub,
                elapsed=t, total=60.0,
                explanation=explanation)

        # HUD も模擬データで書く
        rus_fake = {"arousal": arousal, "valence": valence}
        if hasattr(self, "_update_watch_hud"):
            self._update_watch_hud(
                rus_fake, engagement, hr, self.audio.get_bands(),
                quality=[1, 1, 1, 1],
                recording=getattr(self, "recording", False))

    def _watch_save_photo(self):
        """Watch ビューだけを PNG 保存 (HUD 含む現在描画)."""
        from pathlib import Path
        from datetime import datetime
        out_dir = Path.home() / ".muse_eq" / "watch_photos"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        sub = "?"
        if getattr(self, "sea_widget", None) is not None:
            sub = getattr(self.sea_widget, "_sub_view", "?")
        path = out_dir / f"watch_{sub}_{ts}.png"
        try:
            pix = self.watch_page.grab()
            pix.save(str(path), "PNG")
            self._show_toast(f"📷 {path.name}", duration_ms=3500)
        except Exception as e:
            self._show_toast(f"⚠ Photo failed: {e}", accent="#e74c3c")

    def _take_screenshot(self):
        """F12: 現在ウィンドウを ~/.muse_eq/screenshots/ に PNG 保存."""
        from pathlib import Path
        from datetime import datetime
        out_dir = Path.home() / ".muse_eq" / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"shot_{ts}.png"
        try:
            pix = self.grab()
            pix.save(str(path), "PNG")
            self._show_toast(f"📷 Saved: {path.name}", duration_ms=3500)
        except Exception as e:
            self._show_toast(f"⚠ Screenshot failed: {e}",
                              accent="#e74c3c")

    def _pick_custom_accent(self):
        """QColorDialog で任意の accent 色を選んで適用."""
        cur = QtGui.QColor(self.theme.accent)
        color = QtWidgets.QColorDialog.getColor(
            cur, self, "Choose custom accent color")
        if color.isValid():
            self.theme.set_custom_accent(color.name())
            self._show_toast(f"🎨 Accent → {color.name()}")

    def _on_volume_changed(self, val):
        try:
            self.audio.set_master_volume(val)
        except Exception:
            pass
        if hasattr(self, "volume_val_label"):
            self.volume_val_label.setText(f"{int(val)}%")
            # ミュート系のアイコン切替
            if val == 0:
                self.volume_label.setText("🔇")
            elif val < 50:
                self.volume_label.setText("🔈")
            elif val < 110:
                self.volume_label.setText("🔉")
            else:
                self.volume_label.setText("🔊")

    def _open_big_panel(self):
        dlg = _BigControlPanel(self)
        dlg.exec_()

    def _open_settings(self):
        dlg = _SettingsDialog(self, theme=self.theme)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self._show_toast("⚙ Settings applied")

    def _show_about(self):
        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("About EEG Adaptive EQ")
        msg.setTextFormat(QtCore.Qt.RichText)
        msg.setText(
            "<h2 style='color:%s'>🧠 EEG Adaptive EQ</h2>"
            "<p style='color:#c0c0c0'>"
            "Real-time EEG / PPG → adaptive EQ + immersive video.<br>"
            "Built with PyQt5 + pedalboard + Muse S Athena."
            "</p>"
            "<p><b>Version:</b> 0.9.0 (MVP)<br>"
            "<b>Author:</b> HIMEJI-HIRO<br>"
            "<b>License:</b> MIT</p>"
            "<p><a style='color:%s' "
            "href='https://github.com/HIMEJI-HIRO/muse-emotion-eq'>"
            "github.com/HIMEJI-HIRO/muse-emotion-eq</a></p>"
            "<p style='color:#8a8a8a; font-size:10px;'>"
            "Co-developed with Claude (Anthropic) in 2 weeks.<br>"
            "Vital Sensing × Affective Computing × Audio.</p>"
            % (self.theme.accent, self.theme.accent)
        )
        msg.setStandardButtons(QtWidgets.QMessageBox.Ok)
        msg.exec_()

    def _show_help(self):
        """F1 で表示するキーボードショートカット一覧."""
        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("Keyboard Shortcuts")
        msg.setTextFormat(QtCore.Qt.RichText)
        msg.setText(
            "<h3>⌨ Keyboard Shortcuts</h3>"
            "<table cellspacing=8>"
            "<tr><td colspan=2><b>— Default (Studio / Listen) —</b></td></tr>"
            "<tr><td><b>1</b></td><td>🧠 Studio mode</td></tr>"
            "<tr><td><b>2</b></td><td>🎚 Listen mode</td></tr>"
            "<tr><td><b>3</b></td><td>🌊 Watch mode</td></tr>"
            "<tr><td colspan=2><b>— In Watch mode —</b></td></tr>"
            "<tr><td><b>1</b></td><td>🌊 Surface &nbsp; 🧠 EEG-driven</td></tr>"
            "<tr><td><b>2</b></td><td>🌊 Underwater &nbsp; ♥ HR-driven</td></tr>"
            "<tr><td colspan=2><b>— Anywhere —</b></td></tr>"
            "<tr><td><b>Ctrl + 1/2/3</b></td><td>Mode switch from anywhere</td></tr>"
            "<tr><td><b>Space</b></td><td>♪ Audio ON/OFF</td></tr>"
            "<tr><td><b>R</b></td><td>● Toggle REC</td></tr>"
            "<tr><td><b>Esc</b></td><td>Restore from focused card</td></tr>"
            "<tr><td><b>F1</b></td><td>Show this help</td></tr>"
            "<tr><td><b>F11</b></td><td>⛶ Toggle fullscreen</td></tr>"
            "<tr><td><b>F12</b></td><td>📷 Save screenshot</td></tr>"
            "</table>"
        )
        msg.setStandardButtons(QtWidgets.QMessageBox.Ok)
        msg.exec_()

    # ---- Mode 切替 (Studio / Listen / Watch) ----
    def _update_mode_btn_style(self):
        t = self.theme
        for key, btn in self.mode_btns.items():
            active = btn.isChecked()
            # プレフィックス: 選択=▶ , 非選択=○
            prefix = "▶ " if active else "○  "
            btn.setText(f"{prefix}{self._mode_labels[key]}")
            if active:
                # フラット + 下線スタイル
                btn.setStyleSheet(
                    "QPushButton { background-color: transparent; "
                    f"color: {t.text_main}; "
                    f"border: none; "
                    f"border-bottom: 2px solid {t.accent}; "
                    "border-radius: 0; "
                    "padding: 6px 14px 4px 14px; "
                    "font-size: 12px; font-weight: bold; "
                    "letter-spacing: 2.5px; }"
                )
                eff = btn.graphicsEffect()
                if not isinstance(eff, QtWidgets.QGraphicsDropShadowEffect):
                    eff = QtWidgets.QGraphicsDropShadowEffect(btn)
                    btn.setGraphicsEffect(eff)
                ar, ag, ab = (QtGui.QColor(t.accent).red(),
                              QtGui.QColor(t.accent).green(),
                              QtGui.QColor(t.accent).blue())
                eff.setColor(QtGui.QColor(ar, ag, ab, 180))
                eff.setBlurRadius(16)
                eff.setOffset(0, 0)
            else:
                btn.setStyleSheet(
                    "QPushButton { background-color: transparent; "
                    f"color: {t.text_dim}; "
                    "border: none; border-bottom: 2px solid transparent; "
                    "border-radius: 0; "
                    "padding: 6px 14px 4px 14px; "
                    "font-size: 12px; letter-spacing: 2.5px; }"
                    f"QPushButton:hover {{ color: {t.text_main}; "
                    f"border-bottom: 2px solid rgba("
                    f"{QtGui.QColor(t.accent).red()}, "
                    f"{QtGui.QColor(t.accent).green()}, "
                    f"{QtGui.QColor(t.accent).blue()}, 120); }}"
                )
                btn.setGraphicsEffect(None)

    # モードタブの並び順 (スライド方向の判定に使う)
    _MODE_ORDER = ("studio", "listen", "watch")

    def _animate_mode_fade(self, widget):
        """新しいページにスライドイン + 軽い opacity フェード."""
        try:
            # 現モード vs 前モードでスライド方向を決める
            cur_idx = self._MODE_ORDER.index(getattr(self, "_mode", "studio"))
            prev_idx = getattr(self, "_prev_mode_idx", cur_idx)
            direction = 1 if cur_idx > prev_idx else -1
            self._prev_mode_idx = cur_idx

            # ウィジェット位置を一時的にオフセット → 0 へ戻す
            geom = widget.geometry()
            offset = 60 * direction
            widget.setGeometry(geom.x() + offset, geom.y(),
                                geom.width(), geom.height())
            pos_anim = QtCore.QPropertyAnimation(widget, b"geometry", widget)
            pos_anim.setDuration(260)
            pos_anim.setStartValue(QtCore.QRect(
                geom.x() + offset, geom.y(), geom.width(), geom.height()))
            pos_anim.setEndValue(geom)
            pos_anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
            pos_anim.start(QtCore.QAbstractAnimation.DeleteWhenStopped)
            self._mode_pos_anim = pos_anim

            # 同時に opacity フェード
            eff = QtWidgets.QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(eff)
            eff.setOpacity(0.0)
            op_anim = QtCore.QPropertyAnimation(eff, b"opacity", widget)
            op_anim.setDuration(240)
            op_anim.setStartValue(0.0)
            op_anim.setEndValue(1.0)
            op_anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)

            def _cleanup():
                widget.setGraphicsEffect(None)
            op_anim.finished.connect(_cleanup)
            op_anim.start(QtCore.QAbstractAnimation.DeleteWhenStopped)
            self._mode_op_anim = op_anim
        except Exception:
            pass

    LISTEN_VISIBLE = {"eq", "emotion", "band"}

    def _set_mode(self, mode):
        if mode not in ("studio", "listen", "watch"):
            return
        for k, btn in self.mode_btns.items():
            btn.setChecked(k == mode)
        self._mode = mode
        self._update_mode_btn_style()

        # Listen → 専用ページへ. Studio/Watch では Sea reparent 必要
        if mode == "listen":
            self.stack.setCurrentWidget(self.listen_page)
            self._animate_mode_fade(self.listen_page)
            self._show_toast("🎚 LISTEN mode")
            return

        # Watch → Sea ウィジェットを watch_page に reparent
        sea_widget = getattr(self, "sea_widget", None)

        if mode == "watch":
            if sea_widget is not None:
                if hasattr(self, "eq_stack"):
                    idx = self.eq_stack.indexOf(sea_widget)
                    if idx >= 0:
                        self.eq_stack.removeWidget(sea_widget)
                self._watch_sea_holder.layout().addWidget(sea_widget)
                sea_widget.show()
            self.stack.setCurrentWidget(self.watch_page)
            self._watch_hud.raise_()
            self._animate_mode_fade(self.watch_page)
            self._show_toast("🌊 WATCH mode")
        else:
            if sea_widget is not None and hasattr(self, "eq_stack"):
                if self.eq_stack.indexOf(sea_widget) < 0:
                    self.eq_stack.insertWidget(1, sea_widget)
                    sea_widget.show()
            self.stack.setCurrentWidget(self.grid_page)
            self._apply_card_visibility(mode)
            self._animate_mode_fade(self.grid_page)
            self._show_toast("🧠 STUDIO mode")

    def _apply_card_visibility(self, mode):
        if mode == "studio":
            for c in self.cards.values():
                c.setVisible(True)
        elif mode == "listen":
            for cid, c in self.cards.items():
                c.setVisible(cid in self.LISTEN_VISIBLE)

    # ---- central サイズ追従 ----
    def eventFilter(self, obj, event):
        # アクティビティ検出
        et = event.type()
        if et in (QtCore.QEvent.MouseMove, QtCore.QEvent.MouseButtonPress,
                  QtCore.QEvent.KeyPress, QtCore.QEvent.Wheel):
            self._last_activity_time = time.time()
            if self._idle_overlay is not None:
                try:
                    self._idle_overlay.deleteLater()
                except Exception:
                    pass
                self._idle_overlay = None

        if et == QtCore.QEvent.Resize:
            if hasattr(self, "watch_page") and obj is self.watch_page:
                r = self.watch_page.rect()
                for w in (getattr(self, "_watch_tron", None),
                          getattr(self, "_watch_matrix", None),
                          getattr(self, "_watch_mandala", None),
                          getattr(self, "_watch_hud", None),
                          getattr(self, "_watch_cursor_particles", None)):
                    if w is not None:
                        w.setGeometry(r)
                # 再度 stacking
                if hasattr(self, "_watch_tron"):
                    self._watch_tron.lower()
                if hasattr(self, "_watch_matrix"):
                    self._watch_matrix.raise_()
                if hasattr(self, "_watch_mandala"):
                    self._watch_mandala.raise_()
                self._watch_hud.raise_()
                if hasattr(self, "_watch_cursor_particles"):
                    self._watch_cursor_particles.raise_()
                if hasattr(self, "_watch_photo_btn"):
                    # 右上に常駐
                    pad = 14
                    self._watch_photo_btn.move(
                        self.watch_page.width()
                        - self._watch_photo_btn.width() - pad,
                        pad + 50)   # 上部 status bar 下に
                    self._watch_photo_btn.raise_()
                if hasattr(self, "_watch_demo_btn"):
                    pad = 14
                    # Photo の左隣
                    self._watch_demo_btn.move(
                        self.watch_page.width()
                        - self._watch_photo_btn.width()
                        - self._watch_demo_btn.width() - pad - 6,
                        pad + 50)
                    self._watch_demo_btn.raise_()
                if hasattr(self, "_watch_demo_panel"):
                    # 右側に固定幅 400 のパネル、上下 ⇒ ステータスバー下から
                    # ボトムまでフルに使う (上下バランス重視).
                    pw = 400
                    top_pad = 100   # 上のステータスバー + photo/demo btn 下
                    bot_pad = 28
                    ph = max(420,
                             self.watch_page.height() - top_pad - bot_pad)
                    self._watch_demo_panel.setGeometry(
                        self.watch_page.width() - pw - 14,
                        top_pad,
                        pw, ph)
                    self._watch_demo_panel.raise_()
        return super().eventFilter(obj, event)

    # ---- Watch mode の HUD ----
    def _build_listen_page(self):
        from audio_engine import BANDS, BAND_KEYS
        from collections import OrderedDict
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(48, 28, 48, 28)
        outer.setSpacing(14)

        def _section_title(text):
            lbl = QtWidgets.QLabel(text)
            lbl.setStyleSheet(
                "color: #c0c0c0; font-size: 12px; "
                "font-weight: 600; letter-spacing: 2px;"
                "margin-bottom: 2px;")
            return lbl

        # ============ Emotion セクション ============
        outer.addWidget(_section_title("EMOTION"))

        emo_box = QtWidgets.QFrame()
        emo_box.setObjectName("listen_box")
        eb_lay = QtWidgets.QGridLayout(emo_box)
        eb_lay.setContentsMargins(28, 18, 28, 18)
        eb_lay.setHorizontalSpacing(18)
        eb_lay.setVerticalSpacing(12)

        aro_stops = [(0.0, QtGui.QColor(80, 140, 220)),
                     (0.5, QtGui.QColor(255, 180, 80)),
                     (1.0, QtGui.QColor(255, 80, 80))]
        val_stops = [(0.0, QtGui.QColor(120, 60, 160)),
                     (0.5, QtGui.QColor(80, 150, 230)),
                     (1.0, QtGui.QColor(120, 230, 255))]

        self._listen_aro_bar = _RibbonBar(aro_stops)
        self._listen_aro_bar.setMinimumHeight(32)
        self._listen_aro_bar.setMaximumHeight(38)
        self._listen_val_bar = _RibbonBar(val_stops)
        self._listen_val_bar.setMinimumHeight(32)
        self._listen_val_bar.setMaximumHeight(38)
        self._listen_aro_val = QtWidgets.QLabel("0.50")
        self._listen_val_val = QtWidgets.QLabel("0.50")

        def _bar_label(txt):
            lbl = QtWidgets.QLabel(txt)
            lbl.setStyleSheet(
                "color: #c0c0c0; font-size: 13px; font-weight: 500;"
                "letter-spacing: 0.8px;")
            lbl.setFixedWidth(80)
            return lbl

        def _val_label(lbl):
            lbl.setStyleSheet(
                "color: #f0f0f0; font-family: 'Consolas', monospace; "
                "font-size: 13px; font-weight: bold;")
            lbl.setFixedWidth(48)
            lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        _val_label(self._listen_aro_val)
        _val_label(self._listen_val_val)
        eb_lay.addWidget(_bar_label("Arousal"), 0, 0)
        eb_lay.addWidget(self._listen_aro_bar, 0, 1)
        eb_lay.addWidget(self._listen_aro_val, 0, 2)
        eb_lay.addWidget(_bar_label("Valence"), 1, 0)
        eb_lay.addWidget(self._listen_val_bar, 1, 1)
        eb_lay.addWidget(self._listen_val_val, 1, 2)
        eb_lay.setColumnStretch(1, 1)

        outer.addWidget(emo_box)

        # ============ 大型感情ラベル ============
        emo_row = QtWidgets.QHBoxLayout()
        self._listen_emo_emoji = QtWidgets.QLabel("◌")
        emoji_font = QtGui.QFont()
        emoji_font.setPointSize(40)
        self._listen_emo_emoji.setFont(emoji_font)
        self._listen_emo_label = QtWidgets.QLabel("Neutral")
        label_font = QtGui.QFont()
        label_font.setPointSize(36)
        label_font.setBold(True)
        self._listen_emo_label.setFont(label_font)
        emo_row.addStretch()
        emo_row.addWidget(self._listen_emo_emoji,
                          alignment=QtCore.Qt.AlignVCenter)
        emo_row.addSpacing(10)
        emo_row.addWidget(self._listen_emo_label,
                          alignment=QtCore.Qt.AlignVCenter)
        emo_row.addStretch()
        glow = QtWidgets.QGraphicsDropShadowEffect()
        glow.setBlurRadius(36)
        glow.setOffset(0, 0)
        glow.setColor(QtGui.QColor(self.theme.accent))
        self._listen_emo_label.setGraphicsEffect(glow)
        self._listen_emo_glow = glow
        outer.addLayout(emo_row)

        # ============ Brain Power (流れる波形) ============
        outer.addWidget(_section_title("BRAIN POWER"))
        bp_row = QtWidgets.QHBoxLayout()
        bp_row.setSpacing(10)
        self._listen_band_bars = OrderedDict()
        for bi, band in enumerate(BAND_NAMES):
            wave = _BrainWave(BAND_COLORS[bi])
            bp_row.addWidget(wave, 1)
            self._listen_band_bars[band] = wave
        outer.addLayout(bp_row)

        # ============ Heart Rate (PPG ch1 + BPM 大表示) ============
        outer.addWidget(_section_title("HEART RATE"))
        hr_row = QtWidgets.QHBoxLayout()
        hr_row.setSpacing(14)
        # BPM 大表示
        self._listen_hr_label = QtWidgets.QLabel("♥  ---")
        self._listen_hr_label.setStyleSheet(
            "font-family: 'Consolas'; font-size: 32px; font-weight: bold; "
            "color: #ff6b6b; padding-right: 12px;")
        self._listen_hr_label.setFixedWidth(170)
        hr_row.addWidget(self._listen_hr_label,
                          alignment=QtCore.Qt.AlignVCenter)
        # PPG ch1 のミニ波形プロット
        self._listen_hr_plot = pg.PlotWidget()
        self._listen_hr_plot.setBackground("#0d0d10")
        self._listen_hr_plot.showGrid(x=False, y=True, alpha=0.12)
        self._listen_hr_plot.setMouseEnabled(x=False, y=False)
        self._listen_hr_plot.hideButtons()
        self._listen_hr_plot.hideAxis("bottom")
        self._listen_hr_plot.hideAxis("left")
        self._listen_hr_plot.setFixedHeight(70)
        self._listen_hr_curve = self._listen_hr_plot.plot(
            np.zeros(PPG_BUF_LEN),
            pen=pg.mkPen(color="#ff6b6b", width=1.8))
        hr_row.addWidget(self._listen_hr_plot, 1)
        outer.addLayout(hr_row)

        # ============ Large Adaptive EQ (2x3 グリッド) ============
        outer.addWidget(_section_title("LARGE ADAPTIVE EQ"))
        grid_w = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(grid_w)
        grid.setContentsMargins(0, 4, 0, 4)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(14)
        self._listen_circles = {}
        for i, (k, emo, name, freq, kind, q, blurb) in enumerate(BANDS):
            circle = _InstrumentCircle(k, emo, name)
            circle.bump.connect(self._on_listen_circle_bump)
            circle.set_accent(self.theme.accent)
            r, c = divmod(i, 3)
            grid.addWidget(circle, r, c)
            self._listen_circles[k] = circle
        outer.addWidget(grid_w)

        # ============ Presets ============
        preset_title = _section_title("PRESETS")
        preset_title.setAlignment(QtCore.Qt.AlignCenter)
        outer.addWidget(preset_title)
        preset_defs = [
            ("Flat",       "⚪", {}, 0.0),
            ("Vocal",      "🎤", {"vocal": 4.5, "mid": 2.0,
                                  "bass": -1.5}, 0.25),
            ("Drums",      "🥁", {"drums": 4.5, "bass": 3.0,
                                  "high": -1.5}, 0.2),
            ("High",       "🎺", {"high": 4.5, "vocal": 2.5,
                                  "bass": -1.5}, 0.35),
            ("Spatial",    "🌌", {"air": 4.5, "mid": 1.5,
                                  "drums": -1.0}, 0.75),
            ("Band",       "🎸", {"drums": 3.0, "bass": 4.0,
                                  "mid": 3.0, "vocal": 2.5}, 0.3),
        ]
        preset_row = QtWidgets.QHBoxLayout()
        preset_row.setSpacing(8)
        preset_row.addStretch()
        for label, emo_p, band_dict, reverb in preset_defs:
            btn = QtWidgets.QPushButton(f"{emo_p} {label}")
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setFixedHeight(28)
            btn.setObjectName("listen_preset")
            btn.clicked.connect(
                lambda _, bd=band_dict, rv=reverb: self._apply_preset(bd, rv))
            preset_row.addWidget(btn)
        preset_row.addStretch()
        outer.addLayout(preset_row)

        # ============ Reverb ============
        outer.addWidget(_section_title("REVERB"))
        rv_row = QtWidgets.QHBoxLayout()
        rv_row.setSpacing(14)
        rv_stops = [(0.0, QtGui.QColor(120, 60, 160)),
                    (1.0, QtGui.QColor(80, 200, 220))]
        self._listen_reverb_bar = _RibbonBar(rv_stops)
        self._listen_reverb_bar.setMinimumHeight(30)
        self._listen_reverb_bar.setMaximumHeight(34)
        self._listen_reverb_val = QtWidgets.QLabel("0%")
        self._listen_reverb_val.setStyleSheet(
            "color: #f0f0f0; font-family: 'Consolas', monospace; "
            "font-size: 13px; font-weight: bold;")
        self._listen_reverb_val.setFixedWidth(48)
        self._listen_reverb_val.setAlignment(
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        rv_row.addWidget(self._listen_reverb_bar, 1)
        rv_row.addWidget(self._listen_reverb_val)
        outer.addLayout(rv_row)

        outer.addStretch()

        self._restyle_listen_page(page)
        return page

    def _restyle_listen_page(self, page):
        t = self.theme
        ar, ag, ab = (QtGui.QColor(t.accent).red(),
                      QtGui.QColor(t.accent).green(),
                      QtGui.QColor(t.accent).blue())
        page.setStyleSheet(
            f"QFrame#listen_box {{ background-color: {t.bg_panel}; "
            f"border: 1px solid rgba({ar},{ag},{ab},90); "
            f"border-radius: 12px; }}"
            f"QPushButton#listen_preset {{ background-color: {t.bg_panel}; "
            f"color: {t.text_dim}; "
            f"border: 1px solid {t.border}; border-radius: 14px; "
            "padding: 4px 16px; font-size: 11px; "
            "letter-spacing: 0.5px; }"
            f"QPushButton#listen_preset:hover {{ "
            f"border-color: {t.accent}; color: {t.text_main}; "
            f"background-color: rgba({ar},{ag},{ab},25); }}"
        )
        if hasattr(self, "_listen_emo_glow"):
            self._listen_emo_glow.setColor(QtGui.QColor(t.accent))
        if hasattr(self, "_listen_circles"):
            for c in self._listen_circles.values():
                c.set_accent(t.accent)

    def _build_watch_hud(self):
        w = QtWidgets.QWidget()
        w.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        # ★ 重要: 背景を透明にしないと Sea が隠れる
        w.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        w.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        w.setStyleSheet("QWidget { background: transparent; }")
        lay = QtWidgets.QGridLayout(w)
        lay.setContentsMargins(20, 20, 20, 20)

        # 左下: NEURAL STATE
        nstate = QtWidgets.QFrame()
        nstate.setObjectName("hud")
        nlay = QtWidgets.QVBoxLayout(nstate)
        nlay.setContentsMargins(16, 12, 16, 12)
        nlay.setSpacing(8)
        nstate_title = QtWidgets.QLabel("〔 NEURAL STATE 〕")
        nstate_title.setObjectName("hud_title")
        nlay.addWidget(nstate_title)

        # ARO: 青(低覚醒) → 赤(高覚醒)
        aro_stops = [(0.0, QtGui.QColor(80, 140, 220)),
                     (0.5, QtGui.QColor(255, 180, 80)),
                     (1.0, QtGui.QColor(255, 80, 80))]
        # VAL: 暗紫(不快) → 薄水色(快)
        val_stops = [(0.0, QtGui.QColor(120, 60, 160)),
                     (0.5, QtGui.QColor(80, 150, 230)),
                     (1.0, QtGui.QColor(120, 230, 255))]
        # ENG: 灰(散漫) → 緑(集中) → 黄(過集中)
        eng_stops = [(0.0, QtGui.QColor(120, 120, 130)),
                     (0.5, QtGui.QColor(80, 220, 130)),
                     (1.0, QtGui.QColor(255, 220, 90))]

        self._hud_aro_bar = _HudBar(aro_stops)
        self._hud_val_bar = _HudBar(val_stops)
        self._hud_eng_bar = _HudBar(eng_stops)
        self._hud_aro_val = QtWidgets.QLabel("0.50")
        self._hud_val_val = QtWidgets.QLabel("0.50")
        self._hud_eng_val = QtWidgets.QLabel("0.50")

        def _row(name_txt, bar, val_lbl):
            row = QtWidgets.QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            name = QtWidgets.QLabel(name_txt)
            name.setObjectName("hud_name")
            name.setFixedWidth(34)
            row.addWidget(name)
            row.addWidget(bar, 1)
            val_lbl.setObjectName("hud_val")
            val_lbl.setFixedWidth(36)
            val_lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            row.addWidget(val_lbl)
            cont = QtWidgets.QWidget()
            cont.setLayout(row)
            return cont

        nlay.addWidget(_row("ARO", self._hud_aro_bar, self._hud_aro_val))
        nlay.addWidget(_row("VAL", self._hud_val_bar, self._hud_val_val))
        nlay.addWidget(_row("ENG", self._hud_eng_bar, self._hud_eng_val))

        # HR — 大型表示 (mockup 風: BPM 巨大数字 + 小さい "BPM" 単位)
        hr_row = QtWidgets.QHBoxLayout()
        hr_row.setContentsMargins(0, 4, 0, 0)
        hr_row.setSpacing(4)
        hr_icon = QtWidgets.QLabel("♥")
        hr_icon.setObjectName("hud_hr_icon")
        hr_row.addWidget(hr_icon)
        self._hud_hr_big = QtWidgets.QLabel("---")
        self._hud_hr_big.setObjectName("hud_hr_big")
        hr_row.addWidget(self._hud_hr_big)
        hr_unit = QtWidgets.QLabel("BPM")
        hr_unit.setObjectName("hud_hr_unit")
        hr_row.addWidget(hr_unit, alignment=QtCore.Qt.AlignBottom)
        hr_row.addStretch()
        nlay.addLayout(hr_row)
        # 互換用: 旧 _hud_hr ラベルも作って動的色変化を維持
        self._hud_hr = self._hud_hr_big   # 同じ参照

        # 右下: EQ STATE
        eqstate = QtWidgets.QFrame()
        eqstate.setObjectName("hud")
        elay = QtWidgets.QVBoxLayout(eqstate)
        elay.setContentsMargins(14, 10, 14, 10)
        elay.setSpacing(4)
        eqstate_title = QtWidgets.QLabel("〔 EQ STATE 〕")
        eqstate_title.setObjectName("hud_title")
        elay.addWidget(eqstate_title)
        self._hud_eq = {}
        for k, emo, name in [("drums", "🥁", "DRM"),
                              ("bass", "🎸", "BAS"),
                              ("mid", "🎹", "MID"),
                              ("vocal", "🎤", "VOC"),
                              ("high", "🎺", "HI "),
                              ("air", "🌟", "AIR")]:
            lbl = QtWidgets.QLabel(f"{emo} {name}  +0.0 dB")
            lbl.setObjectName("hud_val")
            self._hud_eq[k] = lbl
            elay.addWidget(lbl)

        # === 上部中央: ステータスバー強化 ===
        # [REC] [▸ STATUS: CALM] [●●●○○ 強度ドット] [▮▮▮▮ 音声メータ] [品質ドット 4ch]
        top_center = QtWidgets.QFrame()
        top_center.setObjectName("hud")
        tc_lay = QtWidgets.QHBoxLayout(top_center)
        tc_lay.setContentsMargins(16, 6, 16, 6)
        tc_lay.setSpacing(12)
        # REC バッジ
        self._hud_rec_badge = QtWidgets.QLabel("● REC")
        self._hud_rec_badge.setObjectName("hud_rec")
        self._hud_rec_badge.setVisible(False)
        tc_lay.addWidget(self._hud_rec_badge)
        # 開きブラケット
        self._hud_scene_left = QtWidgets.QLabel("[")
        self._hud_scene_left.setObjectName("hud_scene_b")
        tc_lay.addWidget(self._hud_scene_left)
        # シーンラベル
        self._hud_scene_lbl = QtWidgets.QLabel("STATUS: CALM")
        self._hud_scene_lbl.setObjectName("hud_scene")
        tc_lay.addWidget(self._hud_scene_lbl)
        # 強度ドット (5個固定の○/●)
        self._hud_scene_dots = QtWidgets.QLabel("●●○○○")
        self._hud_scene_dots.setObjectName("hud_scene_dots")
        tc_lay.addWidget(self._hud_scene_dots)
        # オーディオ風メータ (バー化、Arousal で長さ変動)
        self._hud_audio_meter = _StatusMeter()
        self._hud_audio_meter.setFixedWidth(120)
        self._hud_audio_meter.setFixedHeight(16)
        tc_lay.addWidget(self._hud_audio_meter)
        # 閉じブラケット
        self._hud_scene_right = QtWidgets.QLabel("]")
        self._hud_scene_right.setObjectName("hud_scene_b")
        tc_lay.addWidget(self._hud_scene_right)
        # 品質ドット (4ch)
        self._hud_q_dots = []
        for _ in range(4):
            dot = QtWidgets.QLabel("●")
            dot.setObjectName("hud_qdot")
            dot.setStyleSheet("color: #555; font-size: 16px;"
                              "background: transparent;")
            tc_lay.addWidget(dot)
            self._hud_q_dots.append(dot)

        # 配置: 上部中央 + 左下 + 右下
        lay.addWidget(top_center, 0, 0, 1, 2,
                      alignment=QtCore.Qt.AlignTop | QtCore.Qt.AlignHCenter)
        lay.addWidget(nstate, 1, 0, alignment=QtCore.Qt.AlignBottom | QtCore.Qt.AlignLeft)
        lay.addWidget(eqstate, 1, 1, alignment=QtCore.Qt.AlignBottom | QtCore.Qt.AlignRight)
        lay.setRowStretch(0, 0)
        lay.setRowStretch(1, 1)
        lay.setColumnStretch(0, 1)
        lay.setColumnStretch(1, 1)

        # 心拍パルス用タイマ
        self._hud_pulse_phase = 0.0
        self._hud_pulse_last = time.time()
        self._hud_pulse_bpm = 0.0
        self._hud_pulse_timer = QtCore.QTimer(self)
        self._hud_pulse_timer.timeout.connect(self._hud_pulse_tick)
        self._hud_pulse_timer.start(50)

        self._restyle_watch_hud(w)
        return w

    def _hud_pulse_tick(self):
        if getattr(self, "_mode", "studio") != "watch":
            return
        import math as _m
        now = time.time()
        dt = now - self._hud_pulse_last
        self._hud_pulse_last = now
        bpm = self._hud_pulse_bpm
        if bpm and bpm > 20:
            self._hud_pulse_phase += 2 * _m.pi * (bpm / 60.0) * dt
            # 拍動: 鼓動の瞬間に明るく、すぐ減衰
            x = self._hud_pulse_phase % (2 * _m.pi)
            # 1周期内に 1パルス (短いピーク)
            pulse = _m.exp(-((x - 0.5) ** 2) * 8.0)
            intensity = 0.35 + 0.65 * pulse
            t = self.theme
            r, g, b = t.accent_glow
            col = QtGui.QColor(int(r * intensity + 30 * (1 - intensity)),
                               int(g * intensity + 30 * (1 - intensity)),
                               int(b * intensity + 30 * (1 - intensity)))
            self._hud_hr_big.setStyleSheet(
                f"color: {col.name()}; "
                "font-family: 'Consolas', 'JetBrains Mono', monospace; "
                "font-size: 30px; font-weight: bold; "
                "letter-spacing: 1px; background: transparent; "
                "padding-left: 4px;")

    def _restyle_watch_hud(self, w):
        t = self.theme
        ss = (
            f"QFrame#hud {{ background-color: rgba(0,0,0,160); "
            f"border: 1px solid {t.accent}; border-radius: 10px; }}"
            f"QLabel#hud_title {{ color: {t.accent}; font-size: 11px; "
            "font-weight: bold; letter-spacing: 2px; "
            "background: transparent; }"
            f"QLabel#hud_name {{ color: {t.text_dim}; "
            "font-family: 'Consolas', 'JetBrains Mono', monospace; "
            "font-size: 11px; font-weight: bold; letter-spacing: 1px; "
            "background: transparent; }"
            f"QLabel#hud_val {{ color: {t.text_main}; "
            "font-family: 'Consolas', 'JetBrains Mono', monospace; "
            "font-size: 12px; background: transparent; }"
            f"QLabel#hud_hr {{ color: {t.accent}; "
            "font-family: 'Consolas', 'JetBrains Mono', monospace; "
            "font-size: 18px; font-weight: bold; "
            "letter-spacing: 1px; background: transparent; }"
            f"QLabel#hud_hr_icon {{ color: {t.accent}; "
            "font-size: 22px; background: transparent; }"
            f"QLabel#hud_hr_big {{ color: {t.accent}; "
            "font-family: 'Consolas', 'JetBrains Mono', monospace; "
            "font-size: 30px; font-weight: bold; "
            "letter-spacing: 1px; background: transparent; "
            "padding-left: 4px; }"
            f"QLabel#hud_hr_unit {{ color: {t.text_dim}; "
            "font-family: 'Consolas', monospace; "
            "font-size: 11px; letter-spacing: 1.5px; "
            "background: transparent; padding-bottom: 4px; }"
            f"QLabel#hud_scene {{ color: {t.accent}; font-size: 14px; "
            "font-weight: bold; letter-spacing: 2.5px; "
            "background: transparent; "
            "font-family: 'Consolas', 'JetBrains Mono', monospace; }"
            f"QLabel#hud_scene_b {{ color: {t.text_dim}; font-size: 16px; "
            "background: transparent; }"
            f"QLabel#hud_scene_dots {{ color: {t.accent}; font-size: 12px; "
            "letter-spacing: 2px; background: transparent; }"
            "QLabel#hud_rec { color: #e74c3c; font-size: 12px; "
            "font-weight: bold; letter-spacing: 1.5px; "
            "background: transparent; }"
        )
        w.setStyleSheet(ss)

    EMO_LABELS = [
        # (条件 lambda(arousal, valence), emoji, label, color)
        (lambda a, v: v > 0.65 and a > 0.55, "😄", "Excited", "#f1c40f"),
        (lambda a, v: v > 0.6 and a <= 0.55, "😊", "Happy",   "#2ecc71"),
        (lambda a, v: v < 0.4 and a > 0.55,  "😠", "Tense",   "#e74c3c"),
        (lambda a, v: v < 0.4 and a <= 0.45, "😔", "Sad",     "#3498db"),
        (lambda a, v: a > 0.65,              "⚡", "Alert",   "#e67e22"),
        (lambda a, v: a < 0.35,              "😌", "Calm",    "#3498db"),
        (lambda a, v: v > 0.55,              "🙂", "Pleasant","#1abc9c"),
        (lambda a, v: v < 0.45,              "🙁", "Unease",  "#95a5a6"),
    ]

    def _emotion_label(self, a, v):
        for cond, emo, name, color in self.EMO_LABELS:
            try:
                if cond(a, v):
                    return emo, name, color
            except Exception:
                continue
        return "◌", "Neutral", "#cccccc"

    def _update_listen_ui(self, rus, eng, eq_vals, reverb_wet,
                          bands=None, hr_bpm=None, ppg_ch=None):
        a = rus.get("arousal", 0.5)
        v = rus.get("valence", 0.5)
        emoji, name, color = self._emotion_label(a, v)
        self._listen_emo_emoji.setText(emoji)
        self._listen_emo_label.setText(name)
        self._listen_emo_label.setStyleSheet(f"color: {color};")
        if hasattr(self, "_listen_emo_glow"):
            self._listen_emo_glow.setColor(QtGui.QColor(color))
        self._listen_aro_bar.set_value(a)
        self._listen_val_bar.set_value(v)
        self._listen_aro_val.setText(f"{a:.2f}")
        self._listen_val_val.setText(f"{v:.2f}")
        for k, circle in self._listen_circles.items():
            circle.set_value(eq_vals.get(k, 0.0))
        self._listen_reverb_bar.set_value(reverb_wet)
        self._listen_reverb_val.setText(f"{int(round(reverb_wet * 100))}%")
        # Band Power mini bars (4ch 平均 → 0..1)
        if bands and hasattr(self, "_listen_band_bars"):
            for band, bar in self._listen_band_bars.items():
                vals = bands.get(band, [0])
                valid = [vv for vv in vals if np.isfinite(vv) and vv > 0]
                avg = float(np.mean(valid)) if valid else 0.0
                norm = max(0.0, min(1.0, (avg + 1.0) / 3.5))
                bar.set_value(norm)
        # 心拍: BPM 大表示 + ミニ波形
        if hasattr(self, "_listen_hr_label"):
            if hr_bpm and hr_bpm > 20:
                self._listen_hr_label.setText(f"♥ {int(hr_bpm):3d}")
            else:
                self._listen_hr_label.setText("♥  ---")
        if hasattr(self, "_listen_hr_curve") and ppg_ch is not None:
            d = ppg_ch
            if d.size > 0 and d.std() > 0 and np.all(np.isfinite(d)):
                d_show = d - d.mean()
            else:
                d_show = d
            self._listen_hr_curve.setData(d_show)

    def _update_watch_hud(self, rus, eng, hr, eq_vals,
                          quality=None, recording=False):
        a = rus.get("arousal", 0.5)
        v = rus.get("valence", 0.5)
        self._hud_aro_bar.set_value(a)
        self._hud_val_bar.set_value(v)
        self._hud_eng_bar.set_value(eng)
        self._hud_aro_val.setText(f"{a:.2f}")
        self._hud_val_val.setText(f"{v:.2f}")
        self._hud_eng_val.setText(f"{eng:.2f}")
        if hr and hr > 20:
            self._hud_hr_big.setText(f"{int(hr):3d}")
            self._hud_pulse_bpm = float(hr)
            if hasattr(self, "_watch_mandala"):
                self._watch_mandala.set_bpm(hr)
        else:
            self._hud_hr_big.setText("---")
            self._hud_pulse_bpm = 0.0
            if hasattr(self, "_watch_mandala"):
                self._watch_mandala.set_bpm(0)
        names = {"drums": ("🥁", "DRM"), "bass": ("🎸", "BAS"),
                 "mid": ("🎹", "MID"), "vocal": ("🎤", "VOC"),
                 "high": ("🎺", "HI "), "air": ("🌟", "AIR")}
        for k, lbl in self._hud_eq.items():
            v_db = eq_vals.get(k, 0.0)
            emo, nm = names[k]
            lbl.setText(f"{emo} {nm}  {v_db:+.1f} dB")
        # シーンラベル (sea_widget の判定と整合)
        # 強度 1-5 ドット: そのシーン条件の強さ
        if a > 0.6 and v < 0.4:
            scene_name = "STORMY"
            intensity = max(1, min(5, int((a - 0.6) / 0.08) + 1))
        elif v > 0.55:
            scene_name = "GOLDEN"
            intensity = max(1, min(5, int((v - 0.55) / 0.09) + 1))
        else:
            scene_name = "CALM"
            intensity = max(1, min(5, int((1.0 - abs(a - 0.5) * 2)
                                          / 0.20) + 1))
        dots = "●" * intensity + "○" * (5 - intensity)
        self._hud_scene_lbl.setText(f"STATUS: {scene_name}")
        if hasattr(self, "_hud_scene_dots"):
            self._hud_scene_dots.setText(dots)
        if hasattr(self, "_hud_audio_meter"):
            # オーディオメータは Arousal で長さ変化
            self._hud_audio_meter.set_value(a)
            self._hud_audio_meter.set_accent(self.theme.accent)
        # 品質ドット (1=Good, 2=OK, 4=Bad)
        if quality and len(quality) >= 4:
            q_colors = {1: "#2ecc71", 2: "#f39c12", 4: "#e74c3c"}
            for i, dot in enumerate(self._hud_q_dots):
                col = q_colors.get(int(quality[i]), "#555")
                dot.setStyleSheet(f"color: {col}; font-size: 16px;"
                                  "background: transparent;")
        # REC バッジ
        self._hud_rec_badge.setVisible(bool(recording))
        # マンダラに EQ 値も渡す (放射状ライン用)
        if hasattr(self, "_watch_mandala"):
            self._watch_mandala.set_eq_values(eq_vals)
            # サブビュー連動: Underwater (HR) では δθαβγ 弧を隠す
            sea = getattr(self, "sea_widget", None)
            if sea is not None:
                cur_sub = getattr(sea, "_sub_view", "surface")
                self._watch_mandala.set_show_band_arc(cur_sub == "surface")

    # ---- 録画 ----
    def _audio_btn_style(self, running):
        if running:
            accent = self.theme.accent
            return (f"QPushButton {{ background-color: {accent}; "
                    f"color: #ffffff; border: 1px solid {accent}; "
                    f"border-radius: 13px; padding: 3px 14px; "
                    f"font-size: 11px; font-weight: 600; }}"
                    f"QPushButton:hover {{ background-color: {self.theme.accent_dark}; }}")
        return ("QPushButton { background-color: #1e1e20; color: #b0b0b0; "
                "border: 1px solid #303033; border-radius: 13px; "
                "padding: 3px 14px; font-size: 11px; font-weight: 600; }"
                "QPushButton:hover { background-color: #2a2a2c; color: #e8e8e8; "
                "border-color: #45454a; }")

    def _toggle_audio(self):
        if not HAS_AUDIO:
            self.audio_status.setText(
                "⚠ sounddevice / pedalboard 未インストール")
            self.audio_status.setStyleSheet(
                "font-size: 11px; color: #e74c3c; margin-left: 8px;")
            return

        if self.audio.running:
            self.audio.stop()
            self.audio_btn.setText("♪ Audio OFF")
            self.audio_btn.setStyleSheet(self._audio_btn_style(False))
            self.audio_btn.setGraphicsEffect(None)
            self.audio_status.setText("停止")
            self.audio_status.setStyleSheet(
                "font-size: 11px; color: #8a8a8a; margin-left: 8px;")
            print("[Audio] stopped")
            self._show_toast("♪ Audio OFF", accent="#8a8a8a")
        else:
            out_idx = self.audio_out_selector.currentData()
            ok = self.audio.start(output_index=out_idx)
            if ok:
                self.audio_btn.setText("♪ Audio ON")
                self.audio_btn.setStyleSheet(self._audio_btn_style(True))
                short_out = (self.audio.output_name or "?")[:24]
                self.audio_status.setText(f"→ {short_out}")
                self.audio_status.setStyleSheet(
                    "font-size: 11px; color: #1abc9c; margin-left: 8px;")
                print(f"[Audio] started: {self.audio.input_name}"
                      f"  →  {self.audio.output_name}")
                self._show_toast(f"♪ Audio ON  →  {short_out}")
            else:
                self.audio_status.setText(f"⚠ {self.audio.last_error}")
                self.audio_status.setStyleSheet(
                    "font-size: 11px; color: #e74c3c; margin-left: 8px;")
                print(f"[Audio] start failed: {self.audio.last_error}")
                self._show_toast(
                    f"⚠ Audio error: {self.audio.last_error[:40]}",
                    accent="#e74c3c", duration_ms=4000)

    def _rec_btn_style(self, recording):
        if recording:
            return ("QPushButton { background-color: #c0392b; color: #ffffff; "
                    "border: 1px solid #e74c3c; border-radius: 13px; "
                    "padding: 3px 14px; font-size: 11px; font-weight: 600; }"
                    "QPushButton:hover { background-color: #e74c3c; }")
        return ("QPushButton { background-color: #1e1e20; color: #e74c3c; "
                "border: 1px solid #3a2a2a; border-radius: 13px; "
                "padding: 3px 14px; font-size: 11px; font-weight: 600; }"
                "QPushButton:hover { background-color: #2a1f1f; "
                "border-color: #e74c3c; }")

    def _toggle_recording(self):
        if not self.recording:
            self._start_recording()
        else:
            self._stop_recording()

    def _csv_header(self):
        h = ["time_sec", "timestamp"]
        h += [f"eeg_{c}" for c in CH_NAMES]
        for band in BAND_NAMES:
            h += [f"{band}_{c}" for c in CH_NAMES]
        h += [f"quality_{c}" for c in CH_NAMES]
        h += ["touching", "blink", "jaw"]
        h += ["arousal", "valence", "emotion_label",
              "engagement", "arousal_index", "hr_bpm"]
        return h

    def _start_recording(self):
        try:
            os.makedirs(REC_DIR, exist_ok=True)
            fname = datetime.now().strftime("session_%Y%m%d_%H%M%S.csv")
            path = os.path.join(REC_DIR, fname)
            self.rec_file = open(path, "w", newline="", encoding="utf-8")
            self.rec_writer = csv.writer(self.rec_file)
            self.rec_writer.writerow(self._csv_header())
            self.rec_start_time = time.time()
            self.rec_row_count = 0
            self.recording = True
            self.rec_btn.setText("■ STOP")
            self.rec_btn.setStyleSheet(self._rec_btn_style(True))
            self.rec_path = path
            self.rec_status.setText(f"録画中: {fname}")
            self.rec_status.setStyleSheet(
                "font-size: 11px; color: #e74c3c; margin-left: 10px; "
                "margin-right: 10px; font-weight: bold;")
            self._show_toast(f"● Recording started: {fname}",
                              accent="#e74c3c")
        except Exception as e:
            self.rec_status.setText(f"録画開始失敗: {e}")
            self._show_toast(f"⚠ REC failed: {e}", accent="#e74c3c")

    def _stop_recording(self):
        self.recording = False
        try:
            if self.rec_file:
                self.rec_file.flush()
                self.rec_file.close()
        except Exception:
            pass
        self.rec_file = None
        self.rec_writer = None
        self.rec_btn.setText("● REC")
        self.rec_btn.setStyleSheet(self._rec_btn_style(False))
        dur = time.time() - self.rec_start_time
        self.rec_status.setText(
            f"保存済 ({self.rec_row_count} 行, {dur:.1f}s): {os.path.basename(self.rec_path)}"
        )
        self.rec_status.setStyleSheet(
            "font-size: 11px; color: #2ecc71; margin-left: 10px; margin-right: 10px;")
        self._show_toast(
            f"✓ Saved {self.rec_row_count} rows  ({dur:.1f}s)",
            accent="#2ecc71")

    def _write_csv_row(self, eeg_data, bands, quality, touching, blink, jaw,
                       rus, eng, ar, hr_osc):
        if not self.recording or self.rec_writer is None:
            return
        try:
            t = time.time() - self.rec_start_time
            ts = datetime.now().isoformat(timespec="milliseconds")
            row = [f"{t:.3f}", ts]
            # 各EEGチャンネルの最新サンプル値
            row += [f"{float(eeg_data[i][-1]):.4f}" for i in range(4)]
            for band in BAND_NAMES:
                row += [f"{bands[band][i]:.4f}" for i in range(4)]
            row += [f"{quality[i]:.0f}" for i in range(4)]
            row += [touching, blink, jaw]
            row += [f"{rus['arousal']:.4f}", f"{rus['valence']:.4f}",
                    rus["label"], f"{eng:.4f}", f"{ar:.4f}",
                    f"{hr_osc:.1f}"]
            self.rec_writer.writerow(row)
            self.rec_row_count += 1
        except Exception:
            pass

    # ---- update ----
    def update_ui(self):
        with state.lock:
            eeg_data = [np.array(buf, dtype=np.float32) for buf in state.eeg_buf]
            bands = {k: list(v) for k, v in state.bands.items()}
            quality = list(state.quality)
            touching = state.touching
            blink = state.blink
            jaw = state.jaw
            msg_count = state.msg_count
            last_time = state.last_eeg_time
            optics_data = [np.array(buf, dtype=np.float32)
                           for buf in state.optics_buf]
            ppg_data = [np.array(buf, dtype=np.float32) for buf in state.ppg_buf]
            last_optics = state.last_optics_time
            last_ppg = state.last_ppg_time
            hr_osc = state.heart_rate
            state.msg_count = 0

        now = time.time()

        # EEG — Studio の eeg カードが見えている時だけ描画 (重い filter を節約)
        studio_visible = (getattr(self, "_mode", "studio") == "studio")
        if studio_visible and self.cards.get("eeg") and \
                self.cards["eeg"].isVisible():
            x = np.linspace(0, WINDOW_SEC, BUF_LEN)
            for i, curve in enumerate(self.eeg_curves):
                d = eeg_data[i].copy()
                if not np.all(np.isfinite(d)):
                    d = np.nan_to_num(d)
                d -= d.mean()
                try:
                    d = sosfiltfilt(_SOS_BP, d)
                except Exception:
                    pass
                curve.setData(x, d)

        # Band power → 球体ゲージ (band カード可視時のみ描画)
        if (studio_visible and self.cards.get("band")
                and self.cards["band"].isVisible()):
            for band in BAND_NAMES:
                vals = bands[band]
                valid = [v for v in vals if np.isfinite(v) and v > 0]
                if not valid:
                    avg = 0.0
                else:
                    avg = float(np.mean(valid))
                # log 値 (-1..2.5) を 0..1 に
                norm = max(0.0, min(1.0, (avg + 1.0) / 3.5))
                self.band_bars[band].set_value(norm, raw=avg)

        # Signal Quality (呼吸アニメ) — quality カード可視時のみ.
        # setStyleSheet は非常に重い → アルファ値を 16段階バケットで丸めて
        # 前回と同じバケットなら再適用しない.
        if (studio_visible and self.cards.get("quality")
                and self.cards["quality"].isVisible()):
            import math as _math_ui
            if not hasattr(self, "_q_dot_cache"):
                self._q_dot_cache = {}     # name -> (color, alpha_bucket)
                self._q_text_cache = {}    # name -> (txt, color)
            for i, name in enumerate(CH_NAMES):
                q = quality[i]
                color = QUALITY_COLORS.get(q, "#e74c3c")
                rate = {1: 0.6, 2: 1.4, 4: 2.5}.get(int(q), 2.5)
                pulse = 0.55 + 0.45 * (0.5 + 0.5 * _math_ui.sin(now * rate))
                alpha_bucket = int(pulse * 16)  # 0..16
                cache_key = (color, alpha_bucket)
                if self._q_dot_cache.get(name) != cache_key:
                    self._q_dot_cache[name] = cache_key
                    qc = QtGui.QColor(color)
                    alpha = int(alpha_bucket / 16 * 255)
                    self.q_dots[name].setStyleSheet(
                        f"font-size: 22px; "
                        f"color: rgba({qc.red()},{qc.green()},"
                        f"{qc.blue()},{alpha});")
                txt = QUALITY_TEXT.get(q, "—")
                text_key = (txt, color)
                if self._q_text_cache.get(name) != text_key:
                    self._q_text_cache[name] = text_key
                    self.q_texts[name].setText(txt)
                    self.q_texts[name].setStyleSheet(
                        f"font-size: 12px; color: {color}; "
                        f"font-weight: bold;")
                    self.q_descs[name].setText(QUALITY_DESC.get(q, "—"))
            # アーティファクト (状態変化時のみ stylesheet を書き換える)
            if not hasattr(self, "_artifact_cache"):
                self._artifact_cache = {"t": None, "b": None, "j": None}
            ac = self._artifact_cache
            if ac["t"] != touching:
                ac["t"] = touching
                if touching:
                    self.t_icon.setText("🟢"); self.t_status.setText("接触中")
                    self.t_status.setStyleSheet(
                        "font-size: 12px; color: #2ecc71;")
                else:
                    self.t_icon.setText("⚫"); self.t_status.setText("離れている")
                    self.t_status.setStyleSheet(
                        "font-size: 12px; color: #e74c3c;")
            if ac["b"] != blink:
                ac["b"] = blink
                if blink:
                    self.b_icon.setText("👁"); self.b_status.setText("検出")
                    self.b_status.setStyleSheet(
                        "font-size: 12px; color: #f39c12;")
                else:
                    self.b_icon.setText("⚪"); self.b_status.setText("なし")
                    self.b_status.setStyleSheet(
                        "font-size: 12px; color: #8a8a8a;")
            if ac["j"] != jaw:
                ac["j"] = jaw
                if jaw:
                    self.j_icon.setText("😬"); self.j_status.setText("検出")
                    self.j_status.setStyleSheet(
                        "font-size: 12px; color: #e74c3c;")
                else:
                    self.j_icon.setText("⚪"); self.j_status.setText("なし")
                    self.j_status.setStyleSheet(
                        "font-size: 12px; color: #8a8a8a;")

        # Emotion metrics (Russell + Engagement + Arousal-only)
        rus = compute_russell(bands, quality)
        eng = compute_engagement(bands, quality)
        ar = compute_arousal_only(bands, quality)

        # Russell view
        self.arousal_bar.setValue(int(rus["arousal"] * 100))
        self.valence_bar.setValue(int(rus["valence"] * 100))
        self.emotion_label.setText(rus["label"])
        self.emo_hist_v.append(rus["valence"])
        self.emo_hist_a.append(rus["arousal"])
        self.russell_scatter.set_position(rus["valence"], rus["arousal"])
        trail_v = list(self.emo_hist_v)[-RUSSELL_TRAIL_LEN:]
        trail_a = list(self.emo_hist_a)[-RUSSELL_TRAIL_LEN:]
        # ペアにする (v, a)
        self.russell_curve.set_trail(list(zip(trail_v, trail_a)))

        # EEG → EQ Auto 制御 (Manual 時は no-op)
        self._eq_auto_tick(rus, eng)

        # Sea ビュー (表示中のみ) に生体信号を流す
        self._update_sea_state(rus, eng, quality, hr_osc, last_ppg, now)

        # Listen モード UI 更新
        if getattr(self, "_mode", "studio") == "listen":
            # PPG ch1 を Listen のミニ波形に渡す
            ppg_ch1 = ppg_data[0] if ppg_data and len(ppg_data) > 0 else None
            self._update_listen_ui(rus, eng, self.audio.get_bands(),
                                   self.audio.get_reverb_wet(),
                                   bands=bands,
                                   hr_bpm=hr_osc, ppg_ch=ppg_ch1)

        # Watch モード HUD 更新
        if getattr(self, "_mode", "studio") == "watch":
            self._update_watch_hud(
                rus, eng, hr_osc, self.audio.get_bands(),
                quality=quality, recording=getattr(self, "recording", False))
            # Watch では sea_widget を常時駆動 (表示判定不要)
            if hasattr(self, "sea_widget") and self.sea_widget is not None:
                if quality:
                    q_mean = sum(quality) / len(quality)
                    hsi = max(0.0, min(1.0, (4.0 - q_mean) / 3.0))
                else:
                    hsi = 1.0
                dt_ppg = now - last_ppg if last_ppg else 999
                fresh = 1.0 if dt_ppg < 3.0 else (
                    max(0.0, 1.0 - (dt_ppg - 3.0) / 5.0))
                hr = hr_osc if hr_osc and hr_osc > 20 else None
                self.sea_widget.set_state(
                    arousal=rus.get("arousal"),
                    valence=rus.get("valence"),
                    engagement=eng,
                    hr_bpm=hr, hsi=hsi, signal_fresh=fresh)

        # CSV 録画
        if self.recording:
            self._write_csv_row(eeg_data, bands, quality, touching, blink, jaw,
                                rus, eng, ar, hr_osc)

        # HR & fNIRS — HR カード可視時のみ波形を更新
        if studio_visible and self.cards.get("hr") and \
                self.cards["hr"].isVisible():
            for i, curve in enumerate(self.hr_curves):
                d = optics_data[i]
                if d.std() > 0 and np.all(np.isfinite(d)):
                    d_show = d - d.mean()
                else:
                    d_show = d
                curve.setData(d_show)

        # HR 計算 (250msごと, optics[0] から)
        if now - self._last_hr_update >= 0.25:
            self._last_hr_update = now
            bpm, status_txt = self._estimate_hr(
                optics_data[0], ppg_data, hr_osc, last_optics, last_ppg, now)
            self.hr_bpm_label.setText(f"♥  {bpm}")
            self.hr_status.setText(status_txt)

        # 2D Spectrogram (STFT rolling) — spec カード可視時のみ welch を回す
        if (studio_visible and self.cards.get("spec")
                and self.cards["spec"].isVisible()
                and now - self._last_spec_update >= SPEC_UPDATE_INTERVAL):
            self._last_spec_update = now
            ch_idx = self.spec_ch_selector.currentIndex()
            seg = eeg_data[ch_idx][-FS:]
            seg = seg - seg.mean()
            if np.all(np.isfinite(seg)) and seg.std() > 0:
                freqs, psd = welch(seg, fs=FS, nperseg=SPEC_NPERSEG)
                mask = freqs <= SPEC_FMAX
                psd_log = np.log(psd[mask] + 1e-12)
                self.spec_data[:-1] = self.spec_data[1:]
                n = min(len(psd_log), self.spec_data.shape[1])
                self.spec_data[-1, :n] = psd_log[:n].astype(np.float32)
                self.spec_img.setImage(self.spec_data, levels=(-6.0, 3.0),
                                       autoLevels=False)

        # ヘッダー状態 — 状態が変わった時だけ stylesheet を書く
        streaming = (now - last_time < 1.0)
        prev = getattr(self, "_streaming_state", None)
        if prev != streaming:
            self._streaming_state = streaming
            if streaming:
                self.status_label.setText("● Streaming")
                self.status_label.setStyleSheet(
                    "font-size: 14px; color: #2ecc71; font-weight: bold;")
            else:
                self.status_label.setText("● No Signal")
                self.status_label.setStyleSheet(
                    "font-size: 14px; color: #e74c3c; font-weight: bold;")
        self.rate_label.setText(f"{msg_count * 30} Hz")

        # オーディオレベルメータ更新
        if hasattr(self, "audio_level_meter"):
            try:
                lvl = float(self.audio.get_output_level())
                self.audio_level_meter.set_value(min(1.0, lvl * 3.0))
            except Exception:
                pass

        # 出力スペクトル更新 — 表示中だけ計算する (FFT が重いので)
        if (hasattr(self, "audio_spectrum_bar")
                and self.audio_spectrum_bar.isVisible()):
            try:
                spec = self.audio.get_output_spectrum(n_bins=22)
                self.audio_spectrum_bar.set_values(list(spec))
            except Exception:
                pass

        # ウィンドウタイトルにモード + サブビュー
        try:
            mode = getattr(self, "_mode", "studio")
            mode_lbl = {"studio": "STUDIO", "listen": "LISTEN",
                        "watch": "WATCH"}.get(mode, "?")
            extra = ""
            if mode == "watch" and getattr(self, "sea_widget", None) is not None:
                sub = getattr(self.sea_widget, "_sub_view", "")
                if sub:
                    extra = f" · {sub.upper()}"
            self.setWindowTitle(
                f"[{mode_lbl}{extra}]  EEG Adaptive EQ — Muse S Athena")
        except Exception:
            pass

        # フッターステータスバー更新
        if hasattr(self, "_footer_mode"):
            mode_labels = {"studio": "🧠 STUDIO",
                           "listen": "🎚 LISTEN",
                           "watch": "🌊 WATCH"}
            self._footer_mode.setText(
                mode_labels.get(getattr(self, "_mode", "studio"), "?"))
            a_v = rus.get("arousal", 0.5)
            v_v = rus.get("valence", 0.5)
            self._footer_aro.setText(f"ARO: {a_v:.2f}")
            self._footer_val.setText(f"VAL: {v_v:.2f}")
            self._footer_eng.setText(f"ENG: {eng:.2f}")
            cur_hr_int = int(hr_osc) if hr_osc and hr_osc > 20 else -1
            if cur_hr_int >= 0:
                self._footer_hr.setText(f"♥ {cur_hr_int:3d} BPM")
            else:
                self._footer_hr.setText("♥ --- BPM")
            # HR 値変化フラッシュ
            if cur_hr_int >= 0 and cur_hr_int != self._prev_hr_displayed:
                self._prev_hr_displayed = cur_hr_int
                self._hr_flash_until = now + 0.4
            if now < self._hr_flash_until:
                self._footer_hr.setStyleSheet(
                    f"color: {self.theme.accent}; "
                    "font-family: 'Consolas', monospace; font-size: 11px; "
                    "font-weight: bold; letter-spacing: 0.5px;")
            else:
                self._footer_hr.setStyleSheet(
                    "color: #e74c3c; "
                    "font-family: 'Consolas', monospace; font-size: 10px; "
                    "letter-spacing: 0.5px;")
            audio_txt = (f"⚡ Audio: ON" if self.audio.running
                         else "⚡ Audio: OFF")
            self._footer_audio.setText(audio_txt)
            self._footer_clock.setText(time.strftime("%H:%M:%S"))
            # Uptime
            elapsed = int(now - self._app_start_time)
            mm, ss = divmod(elapsed, 60)
            hh, mm = divmod(mm, 60)
            if hh > 0:
                self._footer_uptime.setText(f"⌛ {hh:d}:{mm:02d}:{ss:02d}")
            else:
                self._footer_uptime.setText(f"⌛ {mm:02d}:{ss:02d}")

    def closeEvent(self, event):
        if self.recording:
            self._stop_recording()
        try:
            if self.audio.running:
                self.audio.stop()
        except Exception:
            pass
        super().closeEvent(event)

    def _estimate_hr(self, optics_sig, ppg_list, hr_osc, last_opt, last_ppg, now):
        """HR を複数ソースから推定."""
        # 1. Mind Monitor が直接 HR を送ってきたら使う
        if hr_osc > 20 and now - max(last_opt, last_ppg) < 3.0:
            return f"{int(hr_osc)} BPM", "Mind Monitor 計算値"

        # 2. PPG 信号があれば使う
        for ppg in ppg_list:
            if ppg.std() > 0.01:
                try:
                    filt = sosfiltfilt(_SOS_PPG, ppg - ppg.mean())
                    peaks, _ = find_peaks(filt, distance=PPG_FS * 0.4,
                                          prominence=filt.std() * 0.3)
                    if len(peaks) > 3:
                        intervals = np.diff(peaks) / PPG_FS
                        bpm = 60.0 / np.median(intervals)
                        if 40 <= bpm <= 200:
                            return f"{int(bpm)} BPM", "PPG からピーク検出"
                except Exception:
                    pass

        # 3. optics からピーク検出 (AC 成分)
        if optics_sig.std() > 0.001 and now - last_opt < 3.0:
            try:
                filt = sosfiltfilt(_SOS_PPG, optics_sig - optics_sig.mean())
                peaks, _ = find_peaks(filt, distance=PPG_FS * 0.4,
                                      prominence=filt.std() * 0.3)
                if len(peaks) > 3:
                    intervals = np.diff(peaks) / PPG_FS
                    bpm = 60.0 / np.median(intervals)
                    if 40 <= bpm <= 200:
                        return f"{int(bpm)} BPM", "Optics からピーク検出 (実験的)"
            except Exception:
                pass

        if now - max(last_opt, last_ppg) > 3.0:
            return "— BPM", "センサー信号なし"
        return "— BPM", "ピーク検出失敗 (信号が弱い)"


# ======================================================================
def main():
    import traceback

    def excepthook(exc_type, exc_value, exc_tb):
        traceback.print_exception(exc_type, exc_value, exc_tb)
    sys.excepthook = excepthook

    print(f"OSC server listening on {LISTEN_IP}:{LISTEN_PORT}")
    print("Mind Monitor で Streaming を ON にしてください")
    server = start_osc_thread()
    app = QtWidgets.QApplication(sys.argv)

    # スプラッシュ表示
    splash = SplashScreen()
    splash.show()
    app.processEvents()
    splash.set_progress(0.15, "Loading audio engine...")
    app.processEvents()

    try:
        win = MainWindow()
        splash.set_progress(0.75, "Initializing UI...")
        app.processEvents()
        # 短い余韻
        QtCore.QTimer.singleShot(400, lambda: (
            splash.set_progress(1.0, "Ready"),
            win.show(),
            splash.close(),
        ))
        # singleShot 実行までイベントループ稼働
        # ただ main loop は app.exec_() なので OK
        print("Window will show. Size:",
              win.size().width(), "x", win.size().height())
    except Exception:
        traceback.print_exc()
        splash.close()
        server.shutdown()
        sys.exit(1)
    exit_code = app.exec_()
    server.shutdown()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
