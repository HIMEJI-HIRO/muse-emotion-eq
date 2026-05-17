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

try:
    import pywt
    HAS_PYWT = True
except ImportError:
    HAS_PYWT = False

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
    pos = np.array([0.0, 0.15, 0.35, 0.55, 0.75, 1.0])
    colors = np.array([
        [0, 0, 0, 255], [0, 0, 160, 255], [0, 200, 120, 255],
        [255, 255, 0, 255], [255, 120, 0, 255], [220, 0, 0, 255],
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
class Card(QtWidgets.QFrame):
    expand_requested = QtCore.pyqtSignal(str)

    def __init__(self, title, content_widget, card_id, parent=None, theme=None):
        super().__init__(parent)
        self.card_id = card_id
        self.is_expanded = False
        self._theme = theme
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
        # フォールバック色 (theme=None のとき)
        bg_panel = t.bg_panel if t else "#1f1f21"
        border = t.border if t else "#2a2a2c"
        border_hover = t.border_hover if t else "#353538"
        text_dim = t.text_dim if t else "#b0b0b0"

        self.setStyleSheet(
            f"QFrame#card {{ background-color: {bg_panel}; "
            f"border: 1px solid {border}; border-radius: 12px; }}"
            f"QFrame#card:hover {{ border-color: {border_hover}; }}"
            "QLabel { border: none; }"
        )
        self.title_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {text_dim}; "
            "letter-spacing: 0.8px;")
        self.expand_btn.setStyleSheet(
            "QPushButton {"
            f" background-color: transparent; color: {text_dim};"
            f" border: 1px solid {border}; border-radius: 6px; font-size: 12px;"
            "}"
            f"QPushButton:hover {{ background-color: {border}; "
            f"color: #ffffff; border-color: {border_hover}; }}"
        )

    def set_expanded(self, expanded):
        self.is_expanded = expanded


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


class _ScanlineOverlay(QtWidgets.QWidget):
    """画面全体を覆う薄い走査線オーバーレイ (CRT 風)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self._phase = 0

    def paintEvent(self, event):
        qp = QtGui.QPainter(self)
        qp.setPen(QtCore.Qt.NoPen)
        line_color = QtGui.QColor(255, 255, 255, 8)
        qp.setBrush(line_color)
        h = self.height()
        for y in range(0, h, 3):
            qp.drawRect(0, y, self.width(), 1)
        # 1本のブライト走査線が下に流れる
        bright_y = (self._phase % max(1, h))
        bright = QtGui.QColor(255, 255, 255, 25)
        qp.setBrush(bright)
        qp.drawRect(0, bright_y, self.width(), 2)
        qp.end()

    def advance(self):
        self._phase = (self._phase + 4) % max(1, self.height())
        self.update()


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

        # 音声エンジン (VB-CABLE → pedalboard → WF-XM5)
        # カード生成より前に作る (EQ カードが self.audio を参照するため)
        self.audio = AudioEngine()

        # --- カード生成 ---
        self.cards = {}
        self.card_positions = {}

        eeg_w, self.eeg_curves = self._build_eeg_plots()
        self.cards["eeg"] = Card("Raw EEG  |  bandpass 1–40 Hz", eeg_w, "eeg",
                                 theme=self.theme)
        self.card_positions["eeg"] = (0, 0)

        (spec_w, self.spec_img, self.spec_data, self.spec_ch_selector,
         self.spec_mode_selector, self.spec_plot) = self._build_spectrogram()
        self.spec_mode_selector.currentIndexChanged.connect(self._on_spec_mode_changed)
        self._spec_mode = 0  # 0=STFT, 1=Wavelet
        self.cards["spec"] = Card("Spectrogram  |  rolling STFT (log power)",
                                  spec_w, "spec", theme=self.theme)
        self.card_positions["spec"] = (1, 0)

        hr_w, self.hr_curves, self.hr_bpm_label, self.hr_status = self._build_hr_panel()
        self.cards["hr"] = Card("Heart Rate & fNIRS (Athena 光学センサー)",
                                hr_w, "hr", theme=self.theme)
        self.card_positions["hr"] = (2, 0)

        band_w, self.band_bars = self._build_band_plot()
        self.cards["band"] = Card("Band Power (δ, θ, α, β, γ)", band_w, "band",
                                  theme=self.theme)
        self.card_positions["band"] = (0, 1)

        q_w, self.q_dots, self.q_texts, self.q_descs, \
            self.touching_row, self.blink_row, self.jaw_row = \
            self._build_quality_panel()
        self.cards["quality"] = Card("Signal Quality (電極接触状態)", q_w, "quality",
                                     theme=self.theme)
        self.card_positions["quality"] = (1, 1)

        (e_w, self.russell_scatter, self.russell_curve,
         self.arousal_bar, self.valence_bar, self.emotion_label,
         self.emo_extras) = self._build_emotion_panel()
        self.cards["emotion"] = Card("Emotion — Russell's Circumplex Model (1980)",
                                     e_w, "emotion", theme=self.theme)
        self.card_positions["emotion"] = (2, 1)

        eq_w = self._build_eq_card()
        self.cards["eq"] = Card("🎛  Adaptive EQ  |  VB-CABLE → WF-1000XM5",
                                eq_w, "eq", theme=self.theme)
        # EQ は 3行をまたぐ右端カラム
        self.card_positions["eq"] = (0, 2, 3, 1)  # row, col, rowspan, colspan

        for c in self.cards.values():
            c.expand_requested.connect(self.toggle_focus)

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
        # HUD は watch_page の子として overlay (resizeEvent で全画面追従)
        self._watch_hud = self._build_watch_hud()
        self._watch_hud.setParent(self.watch_page)
        self._watch_hud.setGeometry(self.watch_page.rect())
        self._watch_hud.raise_()
        self.watch_page.installEventFilter(self)
        self.stack.addWidget(self.watch_page)

        # 走査線オーバーレイは廃止 (chk 削除済). resize 連携だけ残しても無害だが
        # 念のためダミー Widget を作って既存参照を維持.
        self._scanline = QtWidgets.QWidget(central)
        self._scanline.setVisible(False)
        central.installEventFilter(self)

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

    # --- パーツ ---
    def _build_header(self):
        w = QtWidgets.QFrame()
        w.setObjectName("header")
        w.setStyleSheet("""
            QFrame#header {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #1e1e20, stop:1 #242427);
                border: 1px solid #2a2a2c;
                border-radius: 12px;
            }
            QLabel { border: none; }
        """)
        shadow = QtWidgets.QGraphicsDropShadowEffect(w)
        shadow.setBlurRadius(16)
        shadow.setOffset(0, 2)
        shadow.setColor(QtGui.QColor(0, 0, 0, 100))
        w.setGraphicsEffect(shadow)

        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(20, 10, 20, 10)
        h.setSpacing(12)

        # ロゴ + タイトル
        title = QtWidgets.QLabel("🧠")
        title.setStyleSheet("font-size: 22px;")
        h.addWidget(title)
        title_box = QtWidgets.QVBoxLayout()
        title_box.setSpacing(0)
        title_box.setContentsMargins(0, 0, 0, 0)
        main_title = QtWidgets.QLabel("EEG Adaptive EQ")
        main_title.setStyleSheet(
            "font-size: 15px; font-weight: 600; color: #f0f0f0; "
            "letter-spacing: 0.5px;")
        sub = QtWidgets.QLabel("Muse S Athena  ·  Mind Monitor")
        sub.setStyleSheet(
            "font-size: 10px; color: #6a6a6a; letter-spacing: 0.3px;")
        title_box.addWidget(main_title)
        title_box.addWidget(sub)
        h.addLayout(title_box)

        # --- モードタブ (Studio / Listen / Watch) ---
        h.addSpacing(20)
        self.mode_btns = {}
        for key, label in [("studio", "🧠 STUDIO"),
                           ("listen", "🎚 LISTEN"),
                           ("watch", "🌊 WATCH")]:
            btn = QtWidgets.QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setFixedHeight(30)
            btn.clicked.connect(lambda _, k=key: self._set_mode(k))
            h.addWidget(btn)
            self.mode_btns[key] = btn
        self.mode_btns["studio"].setChecked(True)

        h.addStretch()

        hint = QtWidgets.QLabel("⛶  拡大  /  ESC  戻る")
        hint.setStyleSheet(
            "font-size: 10px; color: #5a5a5a; letter-spacing: 0.5px;")
        h.addWidget(hint)
        sep = QtWidgets.QLabel("│")
        sep.setStyleSheet("color: #2a2a2c; margin: 0 8px;")
        h.addWidget(sep)

        # テーマセレクタ
        theme_label = QtWidgets.QLabel("Theme")
        theme_label.setStyleSheet("font-size: 11px; color: #8a8a8a;")
        h.addWidget(theme_label)
        self.theme_selector = QtWidgets.QComboBox()
        self.theme_selector.addItems(list(THEMES.keys()))
        self.theme_selector.setCurrentText(self.theme.name)
        self.theme_selector.setStyleSheet(
            "QComboBox { background-color: #2b2b2b; color: #e0e0e0; "
            "border: 1px solid #3a3a3a; border-radius: 4px; padding: 3px 8px; "
            "font-size: 11px; min-width: 80px; margin-right: 10px; }")
        self.theme_selector.currentTextChanged.connect(self.theme.set)
        h.addWidget(self.theme_selector)

        # Background セレクタ
        bg_label = QtWidgets.QLabel("BG")
        bg_label.setStyleSheet("font-size: 11px; color: #8a8a8a;")
        h.addWidget(bg_label)
        self.bg_selector = QtWidgets.QComboBox()
        self.bg_selector.addItems(list(BG_PALETTES.keys()))
        self.bg_selector.setCurrentText(self.theme.bg_name)
        self.bg_selector.setStyleSheet(
            "QComboBox { background-color: #2b2b2b; color: #e0e0e0; "
            "border: 1px solid #3a3a3a; border-radius: 4px; padding: 3px 8px; "
            "font-size: 11px; min-width: 90px; margin-right: 10px; }")
        self.bg_selector.currentTextChanged.connect(self.theme.set_bg)
        h.addWidget(self.bg_selector)

        # 出力デバイス選択 (CABLE に誤って流れる事故を防ぐ安全弁)
        from audio_engine import list_output_devices, pick_output_device
        out_lbl = QtWidgets.QLabel("Out:")
        out_lbl.setStyleSheet("font-size: 11px; color: #8a8a8a; "
                              "margin-left: 6px;")
        h.addWidget(out_lbl)
        self.audio_out_selector = QtWidgets.QComboBox()
        self.audio_out_selector.setStyleSheet(
            "QComboBox { background-color: #2b2b2b; color: #e0e0e0; "
            "border: 1px solid #3a3a3a; border-radius: 4px; padding: 3px 8px; "
            "font-size: 11px; min-width: 160px; max-width: 260px; "
            "margin-right: 6px; }")
        self.audio_out_selector.setToolTip(
            "音声の出力先 (CABLE 系は除外済み)")
        self._audio_out_devices = []   # list of (idx, name)
        auto_idx, _ = pick_output_device()
        self.audio_out_selector.addItem("Auto", None)
        for idx, name, excluded in list_output_devices():
            if excluded:
                continue
            disp = f"{name[:36]}{'…' if len(name) > 36 else ''}"
            self.audio_out_selector.addItem(disp, idx)
            self._audio_out_devices.append((idx, name))
            if idx == auto_idx:
                self.audio_out_selector.setCurrentIndex(
                    self.audio_out_selector.count() - 1)
        h.addWidget(self.audio_out_selector)

        # 音声 ON/OFF
        self.audio_btn = QtWidgets.QPushButton("♪ Audio OFF")
        self.audio_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.audio_btn.setFixedHeight(28)
        self.audio_btn.setStyleSheet(self._audio_btn_style(False))
        self.audio_btn.clicked.connect(self._toggle_audio)
        self.audio_btn.setToolTip(
            "VB-CABLE → pedalboard → ヘッドホン の音声処理を開始/停止")
        h.addWidget(self.audio_btn)

        self.audio_status = QtWidgets.QLabel("")
        self.audio_status.setStyleSheet(
            "font-size: 11px; color: #8a8a8a; margin-left: 8px; margin-right: 10px;")
        self.audio_status.setFixedWidth(230)
        self.audio_status.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        self.audio_status.setTextInteractionFlags(QtCore.Qt.NoTextInteraction)
        # 長いデバイス名は省略表示 (レイアウトシフト防止)
        h.addWidget(self.audio_status)

        # 録画ボタン
        self.rec_btn = QtWidgets.QPushButton("● REC")
        self.rec_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.rec_btn.setFixedHeight(28)
        self.rec_btn.setStyleSheet(self._rec_btn_style(False))
        self.rec_btn.clicked.connect(self._toggle_recording)
        self.rec_btn.setToolTip("クリックで録画開始 / 停止")
        h.addWidget(self.rec_btn)

        self.rec_status = QtWidgets.QLabel("")
        self.rec_status.setStyleSheet(
            "font-size: 11px; color: #8a8a8a; margin-left: 10px; margin-right: 10px;")
        self.rec_status.setFixedWidth(180)
        self.rec_status.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        h.addWidget(self.rec_status)

        self.status_label = QtWidgets.QLabel("● Connecting...")
        self.status_label.setStyleSheet(
            "font-size: 14px; color: #f39c12; font-weight: bold;")
        self.status_label.setFixedWidth(130)
        self.status_label.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        h.addWidget(self.status_label)
        self.rate_label = QtWidgets.QLabel("— Hz")
        self.rate_label.setStyleSheet(
            "font-size: 12px; color: #8a8a8a; margin-left: 20px;")
        self.rate_label.setFixedWidth(70)
        self.rate_label.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight)
        h.addWidget(self.rate_label)
        return w

    def _build_eeg_plots(self):
        container = pg.GraphicsLayoutWidget()
        curves = []
        for i, (name, color) in enumerate(zip(CH_NAMES, CH_COLORS)):
            p = container.addPlot(row=i, col=0)
            p.setLabel("left", name, **{"color": color, "font-size": "11pt",
                                        "font-weight": "bold"})
            p.showGrid(x=False, y=True, alpha=0.15)
            p.setMouseEnabled(x=False, y=False)
            p.hideButtons()
            p.setYRange(-80, 80, padding=0)
            p.getAxis("left").setWidth(50)
            if i < 3:
                p.hideAxis("bottom")
            else:
                p.setLabel("bottom", "time (s)")
            p.setXRange(0, WINDOW_SEC, padding=0)
            pen = pg.mkPen(color=color, width=1.4)
            x = np.linspace(0, WINDOW_SEC, BUF_LEN)
            c = p.plot(x, np.zeros(BUF_LEN), pen=pen)
            curves.append(c)
        return container, curves

    def _build_spectrogram(self):
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
        combo_style = """
            QComboBox {
                background-color: #18181a; border: 1px solid #3a3a3a;
                border-radius: 4px; padding: 3px 8px;
                color: #e0e0e0; font-size: 11px;
            }
        """
        ch_selector.setStyleSheet(combo_style)
        sel_row.addWidget(ch_selector)

        sel_row.addSpacing(16)
        mode_lbl = QtWidgets.QLabel("Mode:")
        mode_lbl.setStyleSheet("font-size: 11px; color: #9a9a9a;")
        sel_row.addWidget(mode_lbl)
        mode_selector = QtWidgets.QComboBox()
        mode_selector.addItems(["STFT (rolling)", "Wavelet Scalogram (CWT, Morlet)"])
        if not HAS_PYWT:
            mode_selector.model().item(1).setEnabled(False)
            mode_selector.setToolTip("pywt 未インストール: pip install PyWavelets")
        mode_selector.setStyleSheet(combo_style)
        sel_row.addWidget(mode_selector)
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
        return container, img, data, ch_selector, mode_selector, plot

    def _build_band_plot(self):
        plot = pg.PlotWidget()
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        plot.showGrid(x=False, y=True, alpha=0.15)
        plot.setLabel("left", "Power (log)")
        plot.setYRange(-1, 2.5)
        bars = {}
        for bi, band in enumerate(BAND_NAMES):
            x_pos = np.arange(4) * 0.22 + bi * 1.3
            b = pg.BarGraphItem(x=x_pos, height=[0] * 4, width=0.18,
                                brush=BAND_COLORS[bi])
            plot.addItem(b)
            bars[band] = b
        ticks = [(bi * 1.3 + 0.33, f"{band[0].upper()}")
                 for bi, band in enumerate(BAND_NAMES)]
        plot.getAxis("bottom").setTicks([ticks])
        plot.setLabel("bottom", "δ   θ   α   β   γ   (bars = TP9 AF7 AF8 TP10)")
        return plot, bars

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
            p.setLabel("left", f"ch{i+1}", **{"color": colors_ppg[i],
                                              "font-size": "10pt"})
            p.showGrid(x=False, y=True, alpha=0.15)
            p.setMouseEnabled(x=False, y=False)
            p.hideButtons()
            p.getAxis("left").setWidth(40)
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
        """感情推定パネル - Russell / Engagement / Arousal の3ビュー切替"""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # モデル切替
        sel_row = QtWidgets.QHBoxLayout()
        model_lbl = QtWidgets.QLabel("Model:")
        model_lbl.setStyleSheet("font-size: 11px; color: #9a9a9a;")
        sel_row.addWidget(model_lbl)
        model_selector = QtWidgets.QComboBox()
        model_selector.addItems([
            "1. Russell's Circumplex (Arousal × Valence)",
            "2. Engagement Index (Pope et al. 1995)",
            "3. Arousal Index (Ramirez & Vamvakousis 2012)",
        ])
        model_selector.setStyleSheet("""
            QComboBox {
                background-color: #18181a; border: 1px solid #3a3a3a;
                border-radius: 4px; padding: 4px 10px;
                color: #e0e0e0; font-size: 12px;
            }
        """)
        sel_row.addWidget(model_selector, 1)
        v.addLayout(sel_row)

        stacked = QtWidgets.QStackedWidget()
        v.addWidget(stacked, 1)
        self.emo_stacked = stacked
        self.emo_model_selector = model_selector

        # ========== Page 0: Russell ==========
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

        plot = pg.PlotWidget()
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        plot.setLabel("left", "Arousal →")
        plot.setLabel("bottom", "Valence →")
        plot.setXRange(0, 1, padding=0)
        plot.setYRange(0, 1, padding=0)
        plot.setLimits(xMin=0, xMax=1, yMin=0, yMax=1)
        plot.getViewBox().setDefaultPadding(0)
        plot.showGrid(x=True, y=True, alpha=0.12)
        plot.getAxis("bottom").setTicks(
            [[(0, "Negative"), (0.5, "0.5"), (1, "Positive")]])
        plot.getAxis("left").setTicks(
            [[(0, "Low"), (0.5, "0.5"), (1, "High")]])

        plot.addItem(pg.InfiniteLine(pos=0.5, angle=90,
                                     pen=pg.mkPen("#555", style=QtCore.Qt.DashLine)))
        plot.addItem(pg.InfiniteLine(pos=0.5, angle=0,
                                     pen=pg.mkPen("#555", style=QtCore.Qt.DashLine)))

        def add_text(x, y, text, color):
            item = pg.TextItem(text, color=color, anchor=(0.5, 0.5))
            item.setPos(x, y)
            plot.addItem(item)

        add_text(0.80, 0.85, "😊 Happy\nExcited", "#2ecc71")
        add_text(0.20, 0.85, "😠 Angry\nStressed", "#e74c3c")
        add_text(0.20, 0.15, "😔 Sad\nDepressed", "#3498db")
        add_text(0.80, 0.15, "😌 Calm\nRelaxed", "#f39c12")

        trail_pen = pg.mkPen(color=(255, 255, 255, 110), width=1.4)
        curve = plot.plot([0.5], [0.5], pen=trail_pen)

        scatter = pg.ScatterPlotItem(
            [0.5], [0.5], size=20,
            brush=pg.mkBrush(255, 255, 255, 230),
            pen=pg.mkPen("#ffffff", width=2)
        )
        plot.addItem(scatter)
        rv.addWidget(plot, 1)

        emo = QtWidgets.QLabel("—")
        emo.setAlignment(QtCore.Qt.AlignCenter)
        emo.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #e8e8e8; "
            "padding: 6px; background-color: #18181a; border-radius: 6px;")
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
        stacked.addWidget(russell_page)

        # ========== Page 1: Engagement ==========
        eng_page = QtWidgets.QWidget()
        ev = QtWidgets.QVBoxLayout(eng_page)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.setSpacing(8)

        eng_intro = QtWidgets.QLabel(
            "Engagement Index (Pope, Bogart & Bartolome, 1995):\n"
            "  Engagement = β / (α + θ)\n"
            "航空機パイロットの注意散漫検出に開発された指標。\n"
            "値が高いほど 認知的関与・タスクへの集中 が強いと解釈される。\n"
            "教育・運転・VRコンテンツ体験評価など幅広く応用されている。"
        )
        eng_intro.setWordWrap(True)
        eng_intro.setStyleSheet("font-size: 11px; color: #a0a0a0; line-height: 1.5;")
        ev.addWidget(eng_intro)

        eng_row, eng_bar = metric_row("Engagement", "#f39c12")
        ev.addWidget(eng_row)

        eng_label = QtWidgets.QLabel("—")
        eng_label.setAlignment(QtCore.Qt.AlignCenter)
        eng_label.setStyleSheet(
            "font-size: 22px; font-weight: bold; color: #f39c12; "
            "padding: 10px; background-color: #18181a; border-radius: 6px;")
        ev.addWidget(eng_label)

        # 時系列プロット
        eng_plot = pg.PlotWidget()
        eng_plot.setMouseEnabled(x=False, y=False)
        eng_plot.hideButtons()
        eng_plot.setLabel("left", "Engagement")
        eng_plot.setLabel("bottom", "← 過去10秒       最新 →")
        eng_plot.setYRange(0, 1, padding=0)
        eng_plot.setXRange(0, EMO_HIST_LEN, padding=0)
        eng_plot.setLimits(yMin=0, yMax=1, xMin=0, xMax=EMO_HIST_LEN)
        eng_plot.getViewBox().setDefaultPadding(0)
        eng_plot.showGrid(x=False, y=True, alpha=0.15)
        eng_plot.getAxis("left").setTicks(
            [[(0, "0"), (0.3, "0.3"), (0.5, "0.5"), (0.7, "0.7"), (1, "1")]])
        eng_plot.getAxis("bottom").setTicks([[]])
        eng_plot.addItem(pg.InfiniteLine(pos=0.3, angle=0,
                         pen=pg.mkPen("#555", style=QtCore.Qt.DashLine)))
        eng_plot.addItem(pg.InfiniteLine(pos=0.7, angle=0,
                         pen=pg.mkPen("#555", style=QtCore.Qt.DashLine)))
        eng_low = pg.TextItem("Low", color="#8a8a8a", anchor=(0, 1))
        eng_low.setPos(0, 0.3)
        eng_plot.addItem(eng_low)
        eng_high = pg.TextItem("High", color="#8a8a8a", anchor=(0, 1))
        eng_high.setPos(0, 0.7)
        eng_plot.addItem(eng_high)
        eng_curve = eng_plot.plot(np.zeros(EMO_HIST_LEN),
                                  pen=pg.mkPen("#f39c12", width=1.8))
        ev.addWidget(eng_plot, 1)

        eng_tips = QtWidgets.QLabel(
            "💡 Engagement から得られる有用な出力:\n"
            "  • 集中時間 (Engagement > 0.7 の累積)\n"
            "  • 注意散漫の検出 (Engagement < 0.3 の持続)\n"
            "  • タスク中の平均集中度スコア\n"
            "  • 時間帯別の集中度プロファイル"
        )
        eng_tips.setWordWrap(True)
        eng_tips.setStyleSheet(
            "font-size: 10px; color: #8a8a8a; background-color: #18181a; "
            "padding: 8px; border-radius: 4px; line-height: 1.4;")
        ev.addWidget(eng_tips)
        stacked.addWidget(eng_page)

        # ========== Page 2: Arousal Index ==========
        ar_page = QtWidgets.QWidget()
        av = QtWidgets.QVBoxLayout(ar_page)
        av.setContentsMargins(0, 0, 0, 0)
        av.setSpacing(8)

        ar_intro = QtWidgets.QLabel(
            "Arousal Index (Ramirez & Vamvakousis, 2012):\n"
            "  Arousal = β / α  (全電極平均)\n"
            "音楽を聴いたときの覚醒度 (sleepy ↔ excited) を1次元で表現。\n"
            "ベータ波 (13–30Hz) は覚醒・警戒、アルファ波 (8–13Hz) はリラックス。\n"
            "Russell の縦軸だけを単独で使うシンプルな覚醒メーター。"
        )
        ar_intro.setWordWrap(True)
        ar_intro.setStyleSheet("font-size: 11px; color: #a0a0a0; line-height: 1.5;")
        av.addWidget(ar_intro)

        ar_row, ar_bar = metric_row("Arousal", "#e74c3c")
        av.addWidget(ar_row)

        ar_label = QtWidgets.QLabel("—")
        ar_label.setAlignment(QtCore.Qt.AlignCenter)
        ar_label.setStyleSheet(
            "font-size: 22px; font-weight: bold; color: #e74c3c; "
            "padding: 10px; background-color: #18181a; border-radius: 6px;")
        av.addWidget(ar_label)

        ar_plot = pg.PlotWidget()
        ar_plot.setMouseEnabled(x=False, y=False)
        ar_plot.hideButtons()
        ar_plot.setLabel("left", "Arousal")
        ar_plot.setLabel("bottom", "← 過去10秒       最新 →")
        ar_plot.setYRange(0, 1, padding=0)
        ar_plot.setXRange(0, EMO_HIST_LEN, padding=0)
        ar_plot.setLimits(yMin=0, yMax=1, xMin=0, xMax=EMO_HIST_LEN)
        ar_plot.getViewBox().setDefaultPadding(0)
        ar_plot.showGrid(x=False, y=True, alpha=0.15)
        ar_plot.getAxis("left").setTicks(
            [[(0, "0"), (0.3, "0.3"), (0.5, "0.5"), (0.7, "0.7"), (1, "1")]])
        ar_plot.getAxis("bottom").setTicks([[]])
        ar_plot.addItem(pg.InfiniteLine(pos=0.3, angle=0,
                        pen=pg.mkPen("#555", style=QtCore.Qt.DashLine)))
        ar_plot.addItem(pg.InfiniteLine(pos=0.7, angle=0,
                        pen=pg.mkPen("#555", style=QtCore.Qt.DashLine)))
        ar_sleepy = pg.TextItem("Sleepy", color="#8a8a8a", anchor=(0, 1))
        ar_sleepy.setPos(0, 0.3)
        ar_plot.addItem(ar_sleepy)
        ar_excited = pg.TextItem("Excited", color="#8a8a8a", anchor=(0, 1))
        ar_excited.setPos(0, 0.7)
        ar_plot.addItem(ar_excited)
        ar_curve = ar_plot.plot(np.zeros(EMO_HIST_LEN),
                                pen=pg.mkPen("#e74c3c", width=1.8))
        av.addWidget(ar_plot, 1)

        ar_tips = QtWidgets.QLabel(
            "💡 Arousal Index から得られる有用な出力:\n"
            "  • 眠気/居眠り検出 (Arousal 低下の持続)\n"
            "  • 刺激への反応 (音楽/映像に対する覚醒変化)\n"
            "  • 覚醒の揺らぎ (分散) / 急変点\n"
            "  • 音楽の EQ パラメータへの直接マッピング\n"
            "    (例: 高Arousal → 低音増強, 低Arousal → 中音強調)"
        )
        ar_tips.setWordWrap(True)
        ar_tips.setStyleSheet(
            "font-size: 10px; color: #8a8a8a; background-color: #18181a; "
            "padding: 8px; border-radius: 4px; line-height: 1.4;")
        av.addWidget(ar_tips)
        stacked.addWidget(ar_page)

        model_selector.currentIndexChanged.connect(stacked.setCurrentIndex)

        # 返り値:既存UI要素 + 新しい Engagement/Arousal 要素を dict で
        extras = {
            "eng_bar": eng_bar, "eng_label": eng_label, "eng_curve": eng_curve,
            "ar_bar": ar_bar, "ar_label": ar_label, "ar_curve": ar_curve,
        }
        return w, scatter, curve, a_bar, v_bar, emo, extras

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
        # View タブ (Mixer / Sea)
        self.eq_view_mixer_btn = QtWidgets.QPushButton("🎚 Mixer")
        self.eq_view_sea_btn = QtWidgets.QPushButton("🌊 Sea")
        for b in (self.eq_view_mixer_btn, self.eq_view_sea_btn):
            b.setCheckable(True)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setFixedHeight(28)
        self.eq_view_mixer_btn.setChecked(True)
        self.eq_view_mixer_btn.clicked.connect(lambda: self._set_eq_view("mixer"))
        self.eq_view_sea_btn.clicked.connect(lambda: self._set_eq_view("sea"))
        if not HAS_SEA:
            self.eq_view_sea_btn.setEnabled(False)
            self.eq_view_sea_btn.setToolTip("SeaWidget 未利用: " + _SEA_IMPORT_ERR)
        top.addWidget(self.eq_view_mixer_btn)
        top.addWidget(self.eq_view_sea_btn)
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
            ("ボーカル前面",    "🎤",  {"vocal": 2.5, "mid": 1.0, "bass": -0.5},
                                 0.2),
            ("ドラム重視",     "🥁",  {"drums": 2.5, "bass": 1.5}, 0.2),
            ("ハイ楽器輝き",    "🎺",  {"high": 2.8, "vocal": 1.2}, 0.3),
            ("空間広く",       "🌌",  {"air": 2.0, "mid": 0.5}, 0.7),
            ("バンド強調",     "🎸",  {"drums": 1.5, "bass": 2.0, "mid": 1.5,
                                      "vocal": 1.2}, 0.3),
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
        self.eq_view_mixer_btn.setStyleSheet(
            style(self.eq_view_mixer_btn.isChecked(), self.theme.accent))
        self.eq_view_sea_btn.setStyleSheet(
            style(self.eq_view_sea_btn.isChecked(), self.theme.accent))

    def _set_eq_view(self, view):
        if view == "sea" and not HAS_SEA:
            # フォールバック: Mixer
            view = "mixer"
        self._eq_view = view
        self.eq_view_mixer_btn.setChecked(view == "mixer")
        self.eq_view_sea_btn.setChecked(view == "sea")
        self.eq_stack.setCurrentIndex(1 if view == "sea" else 0)
        self._update_eq_view_btn_style()

    # ---- Sea ビュー駆動 (Mixer / Sea どちらでも毎フレーム呼ぶ) ----
    def _update_sea_state(self, rus, eng, quality, hr_bpm_osc, last_ppg, now):
        if not HAS_SEA or self.sea_widget is None:
            return
        # Sea が非表示のときは負荷軽減のため skip
        if self._eq_view != "sea":
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
                background-color: {t.bg_panel}; color: {t.text_main};
                border: 1px solid {t.border_hover}; border-radius: 4px;
                padding: 5px 8px; font-size: 11px;
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
        if hasattr(self, "eq_bank"):
            for f in self.eq_bank.faders.values():
                f.update()

    # ---- Mode 切替 (Studio / Listen / Watch) ----
    def _update_mode_btn_style(self):
        t = self.theme
        for key, btn in self.mode_btns.items():
            active = btn.isChecked()
            if active:
                btn.setStyleSheet(
                    f"QPushButton {{ background-color: {t.accent}; "
                    f"color: {t.bg_deep}; border: 1px solid {t.accent}; "
                    f"border-radius: 14px; padding: 4px 14px; "
                    f"font-size: 11px; font-weight: bold; "
                    f"letter-spacing: 1.2px; }}"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton { background-color: transparent; "
                    f"color: {t.text_dim}; border: 1px solid {t.border}; "
                    f"border-radius: 14px; padding: 4px 14px; "
                    f"font-size: 11px; letter-spacing: 1.2px; }}"
                    f"QPushButton:hover {{ border-color: {t.accent}; "
                    f"color: {t.text_main}; }}"
                )

    LISTEN_VISIBLE = {"eq", "emotion", "band"}

    def _set_mode(self, mode):
        if mode not in ("studio", "listen", "watch"):
            return
        for k, btn in self.mode_btns.items():
            btn.setChecked(k == mode)
        self._mode = mode
        self._update_mode_btn_style()

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
        else:
            if sea_widget is not None and hasattr(self, "eq_stack"):
                if self.eq_stack.indexOf(sea_widget) < 0:
                    self.eq_stack.insertWidget(1, sea_widget)
                    sea_widget.show()
            self.stack.setCurrentWidget(self.grid_page)
            self._apply_card_visibility(mode)

    def _apply_card_visibility(self, mode):
        if mode == "studio":
            for c in self.cards.values():
                c.setVisible(True)
        elif mode == "listen":
            for cid, c in self.cards.items():
                c.setVisible(cid in self.LISTEN_VISIBLE)

    # ---- 走査線 ----
    def _on_scanline_toggled(self, checked):
        self._scanline.setVisible(checked)
        if checked:
            if not hasattr(self, "_scanline_timer"):
                self._scanline_timer = QtCore.QTimer(self)
                self._scanline_timer.timeout.connect(self._scanline.advance)
            self._scanline_timer.start(33)
            self._scanline.raise_()
        else:
            if hasattr(self, "_scanline_timer"):
                self._scanline_timer.stop()

    # ---- central サイズ追従 ----
    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Resize:
            if obj is self.centralWidget():
                self._scanline.setGeometry(obj.rect())
            if hasattr(self, "watch_page") and obj is self.watch_page:
                self._watch_hud.setGeometry(self.watch_page.rect())
                self._watch_hud.raise_()
        return super().eventFilter(obj, event)

    # ---- Watch mode の HUD ----
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

        # HR は別行 (バー無し、大きく)
        self._hud_hr = QtWidgets.QLabel("♥   ---  BPM")
        self._hud_hr.setObjectName("hud_hr")
        nlay.addWidget(self._hud_hr)

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

        # 配置: 左下 + 右下
        lay.addWidget(nstate, 1, 0, alignment=QtCore.Qt.AlignBottom | QtCore.Qt.AlignLeft)
        lay.addWidget(eqstate, 1, 1, alignment=QtCore.Qt.AlignBottom | QtCore.Qt.AlignRight)
        lay.setRowStretch(0, 1)
        lay.setColumnStretch(0, 1)
        lay.setColumnStretch(1, 1)
        self._restyle_watch_hud(w)
        return w

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
        )
        w.setStyleSheet(ss)

    def _update_watch_hud(self, rus, eng, hr, eq_vals):
        a = rus.get("arousal", 0.5)
        v = rus.get("valence", 0.5)
        self._hud_aro_bar.set_value(a)
        self._hud_val_bar.set_value(v)
        self._hud_eng_bar.set_value(eng)
        self._hud_aro_val.setText(f"{a:.2f}")
        self._hud_val_val.setText(f"{v:.2f}")
        self._hud_eng_val.setText(f"{eng:.2f}")
        if hr and hr > 20:
            self._hud_hr.setText(f"♥  {int(hr):3d} BPM")
        else:
            self._hud_hr.setText("♥  ---  BPM")
        names = {"drums": ("🥁", "DRM"), "bass": ("🎸", "BAS"),
                 "mid": ("🎹", "MID"), "vocal": ("🎤", "VOC"),
                 "high": ("🎺", "HI "), "air": ("🌟", "AIR")}
        for k, lbl in self._hud_eq.items():
            v_db = eq_vals.get(k, 0.0)
            emo, nm = names[k]
            lbl.setText(f"{emo} {nm}  {v_db:+.1f} dB")

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
            self.audio_status.setText("停止")
            self.audio_status.setStyleSheet(
                "font-size: 11px; color: #8a8a8a; margin-left: 8px;")
            print("[Audio] stopped")
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
            else:
                self.audio_status.setText(f"⚠ {self.audio.last_error}")
                self.audio_status.setStyleSheet(
                    "font-size: 11px; color: #e74c3c; margin-left: 8px;")
                print(f"[Audio] start failed: {self.audio.last_error}")

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
        except Exception as e:
            self.rec_status.setText(f"録画開始失敗: {e}")

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

    def _on_spec_mode_changed(self, idx):
        self._spec_mode = idx
        if idx == 0:
            # STFT rolling モード
            n_freq_bins = int(SPEC_FMAX * SPEC_NPERSEG / FS) + 1
            self.spec_data = np.full((SPEC_COLS, n_freq_bins), -8.0, dtype=np.float32)
            self.spec_img.setImage(self.spec_data, levels=(-6.0, 3.0),
                                   autoLevels=False)
            self.spec_img.setRect(pg.QtCore.QRectF(0, 0, SPEC_COLS, SPEC_FMAX))
            self.spec_plot.setXRange(0, SPEC_COLS)
            self.spec_plot.setLabel("bottom", "← older      (newest at right) →")
            self.spec_plot.getAxis("bottom").setTicks([[]])
        else:
            # Wavelet モード: 時間軸 = 過去 WINDOW_SEC 秒, 周波数軸 = 線形
            self.spec_data = np.full((BUF_LEN, CWT_NFREQS), -8.0, dtype=np.float32)
            self.spec_img.setImage(self.spec_data, levels=(-6.0, 3.0),
                                   autoLevels=False)
            self.spec_img.setRect(
                pg.QtCore.QRectF(0, CWT_FREQS[0], WINDOW_SEC,
                                 CWT_FREQS[-1] - CWT_FREQS[0]))
            self.spec_plot.setXRange(0, WINDOW_SEC)
            self.spec_plot.setLabel("bottom", "time (s) — 過去5秒")
            self.spec_plot.getAxis("bottom").setTicks(None)

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

        # EEG
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

        # Band power
        for bi, band in enumerate(BAND_NAMES):
            x_pos = np.arange(4) * 0.22 + bi * 1.3
            self.band_bars[band].setOpts(x=x_pos, height=bands[band])

        # Signal Quality
        for i, name in enumerate(CH_NAMES):
            q = quality[i]
            color = QUALITY_COLORS.get(q, "#e74c3c")
            self.q_dots[name].setStyleSheet(f"font-size: 22px; color: {color};")
            txt = QUALITY_TEXT.get(q, "—")
            self.q_texts[name].setText(txt)
            self.q_texts[name].setStyleSheet(
                f"font-size: 12px; color: {color}; font-weight: bold;")
            self.q_descs[name].setText(QUALITY_DESC.get(q, "—"))
        # アーティファクト
        if touching:
            self.t_icon.setText("🟢"); self.t_status.setText("接触中")
            self.t_status.setStyleSheet("font-size: 12px; color: #2ecc71;")
        else:
            self.t_icon.setText("⚫"); self.t_status.setText("離れている")
            self.t_status.setStyleSheet("font-size: 12px; color: #e74c3c;")
        if blink:
            self.b_icon.setText("👁"); self.b_status.setText("検出")
            self.b_status.setStyleSheet("font-size: 12px; color: #f39c12;")
        else:
            self.b_icon.setText("⚪"); self.b_status.setText("なし")
            self.b_status.setStyleSheet("font-size: 12px; color: #8a8a8a;")
        if jaw:
            self.j_icon.setText("😬"); self.j_status.setText("検出")
            self.j_status.setStyleSheet("font-size: 12px; color: #e74c3c;")
        else:
            self.j_icon.setText("⚪"); self.j_status.setText("なし")
            self.j_status.setStyleSheet("font-size: 12px; color: #8a8a8a;")

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
        self.russell_scatter.setData([rus["valence"]], [rus["arousal"]])
        trail_v = list(self.emo_hist_v)[-RUSSELL_TRAIL_LEN:]
        trail_a = list(self.emo_hist_a)[-RUSSELL_TRAIL_LEN:]
        self.russell_curve.setData(trail_v, trail_a)

        # Engagement view
        self.eng_hist.append(eng)
        self.emo_extras["eng_bar"].setValue(int(eng * 100))
        if eng > 0.7:
            eng_txt, eng_col = "High Engagement (集中)", "#2ecc71"
        elif eng < 0.3:
            eng_txt, eng_col = "Low Engagement (散漫)", "#e74c3c"
        else:
            eng_txt, eng_col = "Moderate Engagement (通常)", "#f39c12"
        self.emo_extras["eng_label"].setText(eng_txt)
        self.emo_extras["eng_label"].setStyleSheet(
            f"font-size: 22px; font-weight: bold; color: {eng_col}; "
            "padding: 10px; background-color: #18181a; border-radius: 6px;")
        self.emo_extras["eng_curve"].setData(list(self.eng_hist))

        # Arousal-only view
        self.ar_hist.append(ar)
        self.emo_extras["ar_bar"].setValue(int(ar * 100))
        if ar > 0.7:
            ar_txt, ar_col = "Excited / 覚醒", "#e74c3c"
        elif ar < 0.3:
            ar_txt, ar_col = "Sleepy / 低覚醒", "#3498db"
        else:
            ar_txt, ar_col = "Neutral / 中程度", "#f39c12"
        self.emo_extras["ar_label"].setText(ar_txt)
        self.emo_extras["ar_label"].setStyleSheet(
            f"font-size: 22px; font-weight: bold; color: {ar_col}; "
            "padding: 10px; background-color: #18181a; border-radius: 6px;")
        self.emo_extras["ar_curve"].setData(list(self.ar_hist))

        # EEG → EQ Auto 制御 (Manual 時は no-op)
        self._eq_auto_tick(rus, eng)

        # Sea ビュー (表示中のみ) に生体信号を流す
        self._update_sea_state(rus, eng, quality, hr_osc, last_ppg, now)

        # Watch モード HUD 更新
        if getattr(self, "_mode", "studio") == "watch":
            self._update_watch_hud(rus, eng, hr_osc, self.audio.get_bands())
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

        # HR & fNIRS
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

        # Spectrogram / Scalogram
        if self._spec_mode == 0:
            # STFT rolling
            if now - self._last_spec_update >= SPEC_UPDATE_INTERVAL:
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
        else:
            # Wavelet Scalogram (Morlet CWT)
            if HAS_PYWT and now - self._last_spec_update >= CWT_UPDATE_INTERVAL:
                self._last_spec_update = now
                ch_idx = self.spec_ch_selector.currentIndex()
                seg = eeg_data[ch_idx].astype(np.float64)
                seg = seg - seg.mean()
                if np.all(np.isfinite(seg)) and seg.std() > 0:
                    try:
                        fc = pywt.central_frequency("morl")
                        scales = fc * FS / CWT_FREQS
                        coefs, _ = pywt.cwt(seg, scales, "morl",
                                            sampling_period=1.0 / FS)
                        power = np.log(np.abs(coefs) ** 2 + 1e-12)
                        # coefs shape: (n_freqs, n_times) -> 転置して (time, freq)
                        self.spec_data = power.T.astype(np.float32)
                        # 自動レベル調整(中央値±スケール)
                        med = float(np.median(self.spec_data))
                        self.spec_img.setImage(self.spec_data,
                                               levels=(med - 3.0, med + 5.0),
                                               autoLevels=False)
                    except Exception:
                        pass

        # ヘッダー状態
        if now - last_time < 1.0:
            self.status_label.setText("● Streaming")
            self.status_label.setStyleSheet(
                "font-size: 14px; color: #2ecc71; font-weight: bold;")
        else:
            self.status_label.setText("● No Signal")
            self.status_label.setStyleSheet(
                "font-size: 14px; color: #e74c3c; font-weight: bold;")
        self.rate_label.setText(f"{msg_count * 30} Hz")

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
    try:
        win = MainWindow()
        win.show()
        print("Window shown. Size:", win.size().width(), "x", win.size().height())
    except Exception:
        traceback.print_exc()
        server.shutdown()
        sys.exit(1)
    exit_code = app.exec_()
    server.shutdown()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
