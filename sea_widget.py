"""
sea_widget.py
=============
Emotional Seascape — OpenCV 動画ループ + 生体信号オーバーレイ.

Qt の QMediaPlayer は Windows DirectShow 経由で H.264 に失敗するので
cv2.VideoCapture でフレームを読み、QImage に変換して QPainter で描画.
副産物として 2本の動画を alpha blend で真のクロスフェード可能.

構成:
  SeaWidget (QWidget)
    ├ _VideoSource x3   (cv2 VideoCapture + ループ)
    └ paintEvent で:
        1. 現シーン / 遷移中は両シーンを alpha blend
        2. オーバーレイ: カラーティント, 霧, HR リング, グリッター, 泡, ビネット
"""
import math
import os
import random
import time

from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import (QColor, QImage, QPainter, QPen, QPixmap,
                         QRadialGradient)
from PyQt5.QtWidgets import QWidget

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


# ---- アセット ------------------------------------------------------------
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "assets", "sea")
VIDEO_CALM = os.path.join(ASSETS_DIR, "sea_calm.mp4")
VIDEO_GOLDEN = os.path.join(ASSETS_DIR, "sea_golden.mp4")
VIDEO_STORM = os.path.join(ASSETS_DIR, "sea_storm.mp4")


# ---- EMA (33ms/frame) ----------------------------------------------------
ALPHA_SKY = 0.004
ALPHA_ENG = 0.02
ALPHA_WAVE = 0.015
ALPHA_HR = 0.05
ALPHA_HSI = 0.03


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp_color(c1, c2, t):
    return QColor(
        int(_lerp(c1.red(), c2.red(), t)),
        int(_lerp(c1.green(), c2.green(), t)),
        int(_lerp(c1.blue(), c2.blue(), t)),
        int(_lerp(c1.alpha(), c2.alpha(), t)),
    )


# ==========================================================================
class _VideoSource:
    """cv2.VideoCapture を抱え、次フレームの QImage を供給するだけのクラス."""

    def __init__(self, path):
        self.path = path
        self.available = HAS_CV2 and os.path.exists(path)
        self._cap = None
        self._fps = 30.0
        self._frame_interval = 1.0 / 30.0
        self._last_grab = 0.0
        self._cur_image = None    # QImage (最後に読んだフレーム)
        self._rate = 1.0
        if self.available:
            self._open()

    def _open(self):
        self._cap = cv2.VideoCapture(self.path)
        fps = self._cap.get(cv2.CAP_PROP_FPS)
        if fps and fps > 1.0:
            self._fps = float(fps)
        self._frame_interval = 1.0 / self._fps

    def set_rate(self, rate):
        self._rate = max(0.1, min(3.0, float(rate)))

    def update(self, now):
        """時刻 now に応じて次フレームを読み込む (必要なら). cur_image を更新."""
        if not self.available or self._cap is None:
            return
        # 速度倍率込みのフレーム間隔
        interval = self._frame_interval / self._rate
        if (now - self._last_grab) < interval:
            return
        ok, frame = self._cap.read()
        if not ok:
            # ループ
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
            if not ok:
                return
        # BGR → RGB、連続 numpy → QImage
        h, w, _ = frame.shape
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # QImage は bytes データの生存期間が必要 → copy()
        self._cur_image = QImage(rgb.data, w, h, 3 * w,
                                 QImage.Format_RGB888).copy()
        self._last_grab = now

    def image(self):
        return self._cur_image

    def release(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None


# ==========================================================================
SCENES = ["calm", "golden", "storm"]
SCENE_PATHS = {
    "calm": VIDEO_CALM,
    "golden": VIDEO_GOLDEN,
    "storm": VIDEO_STORM,
}

# シーン選択用パラメータ (EEG ノイズ対策)
SCENE_EMA_ALPHA = 0.004         # 30fps, τ≈8s (判定用のスロー平滑)
SCENE_MIN_DWELL_SEC = 6.0       # 一度切り替わったら 6秒は固定

# ヒステリシス閾値 (入り/出が別)
# GOLDEN: 高 Valence
GOLDEN_ENTER_V = 0.57
GOLDEN_EXIT_V = 0.46
# STORM: 低 Valence × 高 Arousal
STORM_ENTER_V = 0.40
STORM_ENTER_A = 0.60
STORM_EXIT_V = 0.46
STORM_EXIT_A = 0.45


def _pick_scene_hysteresis(current, arousal, valence):
    """現在のシーンを考慮した遷移判定.
    - storm中: V<STORM_EXIT_V かつ A>STORM_EXIT_A を満たす限り storm
    - golden中: V>GOLDEN_EXIT_V を満たす限り golden
    - calm中: 明確に条件を満たしたときだけ遷移
    """
    if current == "storm":
        if valence < STORM_EXIT_V and arousal > STORM_EXIT_A:
            return "storm"
        # 出る: golden 条件満たせば golden へ、でなければ calm
        if valence > GOLDEN_ENTER_V:
            return "golden"
        return "calm"
    if current == "golden":
        if valence > GOLDEN_EXIT_V:
            return "golden"
        if valence < STORM_ENTER_V and arousal > STORM_ENTER_A:
            return "storm"
        return "calm"
    # current == calm
    if valence < STORM_ENTER_V and arousal > STORM_ENTER_A:
        return "storm"
    if valence > GOLDEN_ENTER_V:
        return "golden"
    return "calm"


# ==========================================================================
class SeaWidget(QWidget):
    """動画背景 (cv2) + オーバーレイ描画. 感情で3本をクロスフェード."""

    CROSSFADE_SEC = 2.5
    TARGET_FPS = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 360)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        # 動画ソース
        self._sources = {n: _VideoSource(SCENE_PATHS[n]) for n in SCENES}
        self._available = [n for n, s in self._sources.items() if s.available]
        if not self._available:
            print("[SeaWidget] 動画なし or cv2 未インストール")
            self._available = ["calm"]

        # シーン遷移
        self._current = self._available[0]
        self._target = self._current
        self._fade_start = 0.0
        self._fading = False
        # シーン選択専用の slow EMA (描画用とは別系統)
        self._scene_a = 0.5
        self._scene_v = 0.5
        # 最後にシーン切替した時刻 (dwell ロック用)
        self._last_switch_time = 0.0

        # 生体信号 (目標 / 現在)
        self._t = dict(arousal=0.5, valence=0.5, engagement=0.5,
                       hr=60.0, hsi=1.0, fresh=1.0)
        self._c = dict(self._t)

        # アニメ状態
        self._t0 = time.monotonic()
        self._last_tick = self._t0
        self._wave_phase = 0.0
        self._pulse_phase = 0.0
        self._rings = []
        self._bubbles = []

        # メインループ
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(int(1000 / self.TARGET_FPS))

    # --- 外部 API --------------------------------------------------------
    def set_state(self, arousal=None, valence=None, engagement=None,
                  hr_bpm=None, hsi=None, signal_fresh=None):
        if arousal is not None:
            self._t["arousal"] = max(0.0, min(1.0, float(arousal)))
        if valence is not None:
            self._t["valence"] = max(0.0, min(1.0, float(valence)))
        if engagement is not None:
            self._t["engagement"] = max(0.0, min(1.0, float(engagement)))
        if hr_bpm is not None and hr_bpm > 20:
            self._t["hr"] = float(hr_bpm)
        if hsi is not None:
            self._t["hsi"] = max(0.0, min(1.0, float(hsi)))
        if signal_fresh is not None:
            self._t["fresh"] = max(0.0, min(1.0, float(signal_fresh)))
        # シーン選択: slow EMA + ヒステリシス + 最小滞留時間
        if arousal is not None and valence is not None:
            self._scene_a += SCENE_EMA_ALPHA * (float(arousal) - self._scene_a)
            self._scene_v += SCENE_EMA_ALPHA * (float(valence) - self._scene_v)
            now = time.monotonic()
            # 最後の切替から SCENE_MIN_DWELL_SEC 未満は判定しない
            if (now - self._last_switch_time) >= SCENE_MIN_DWELL_SEC:
                want = _pick_scene_hysteresis(
                    self._target, self._scene_a, self._scene_v)
                if want in self._available and want != self._target:
                    self._request_scene(want)
                    self._last_switch_time = now

    def trigger_pulse(self, strength=1.0):
        self._rings.append((time.monotonic(), float(strength)))

    # --- シーン切替 ------------------------------------------------------
    def _request_scene(self, name):
        if name == self._target:
            return
        self._target = name
        self._fade_start = time.monotonic()
        self._fading = True

    def _fade_progress(self, now):
        if not self._fading:
            return 1.0
        t = (now - self._fade_start) / self.CROSSFADE_SEC
        if t >= 1.0:
            self._current = self._target
            self._fading = False
            return 1.0
        return t

    # --- メイン tick -----------------------------------------------------
    def _tick(self):
        if not self.isVisible():
            return
        now = time.monotonic()
        dt = max(0.001, now - self._last_tick)
        self._last_tick = now

        # EMA
        self._c["arousal"] += ALPHA_WAVE * (self._t["arousal"] - self._c["arousal"])
        self._c["valence"] += ALPHA_SKY * (self._t["valence"] - self._c["valence"])
        self._c["engagement"] += ALPHA_ENG * (self._t["engagement"] - self._c["engagement"])
        self._c["hr"] += ALPHA_HR * (self._t["hr"] - self._c["hr"])
        self._c["hsi"] += ALPHA_HSI * (self._t["hsi"] - self._c["hsi"])
        self._c["fresh"] += ALPHA_HSI * (self._t["fresh"] - self._c["fresh"])

        wave_rate = 0.5 + self._c["arousal"] * 1.5
        self._wave_phase += 2.0 * math.pi * wave_rate * dt

        hr_hz = self._c["hr"] / 60.0
        self._pulse_phase += 2.0 * math.pi * hr_hz * dt
        if self._pulse_phase > 2.0 * math.pi:
            self._pulse_phase -= 2.0 * math.pi
            self._rings.append((now, 1.0))
        self._rings = [(t, s) for (t, s) in self._rings if now - t < 2.5]

        # 泡
        spawn_rate = 0.4 + self._c["arousal"] * 7.0
        if random.random() < spawn_rate * dt:
            w = self.width() or 600
            h = self.height() or 400
            self._bubbles.append([
                random.uniform(0, w),
                h * (0.65 + random.random() * 0.3),
                -(10 + random.random() * 25),
                1.0 + random.random() * 2.2,
                0.0,
                2.2 + random.random() * 2.0,
            ])
        alive = []
        for b in self._bubbles:
            b[4] += dt
            b[1] += b[2] * dt
            if b[4] < b[5] and b[1] > 0:
                alive.append(b)
        self._bubbles = alive[:180]

        # 動画フレーム更新 (再生速度は Arousal 連動)
        rate = 0.7 + self._c["arousal"] * 0.7
        for src in self._sources.values():
            src.set_rate(rate)
        # アクティブな (current, target) だけ進めれば十分だが全部回しても軽い
        active_scenes = {self._current, self._target}
        for name in active_scenes:
            self._sources[name].update(now)

        self.update()

    # --- 描画 -------------------------------------------------------------
    def paintEvent(self, event):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing, True)
        qp.setRenderHint(QPainter.SmoothPixmapTransform, True)
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            qp.end()
            return

        now = time.monotonic()
        progress = self._fade_progress(now)

        # --- 動画フレーム描画 ---
        qp.fillRect(self.rect(), QColor(0, 0, 0))

        # 現シーン (opacity = 1 - progress  during fade, else 1)
        def draw_frame(name, opacity):
            if opacity <= 0.01:
                return
            img = self._sources[name].image()
            if img is None:
                return
            qp.setOpacity(opacity)
            # 画面サイズにフィットさせる (KeepAspectRatioByExpanding)
            tgt = self.rect()
            src_ratio = img.width() / max(1, img.height())
            dst_ratio = w / max(1, h)
            if src_ratio > dst_ratio:
                # ソースが横長 → 縦を合わせて横はみ出し
                draw_h = h
                draw_w = int(draw_h * src_ratio)
                x = (w - draw_w) // 2
                y = 0
            else:
                draw_w = w
                draw_h = int(draw_w / src_ratio)
                x = 0
                y = (h - draw_h) // 2
            qp.drawImage(QRectF(x, y, draw_w, draw_h), img)

        if self._fading and self._target != self._current:
            draw_frame(self._current, 1.0 - progress)
            draw_frame(self._target, progress)
        else:
            scene = self._target if self._fading else self._current
            draw_frame(scene, 1.0)

        qp.setOpacity(1.0)

        # --- オーバーレイ ---
        v = self._c["valence"]
        a = self._c["arousal"]
        e = self._c["engagement"]
        hsi = self._c["hsi"]
        fresh = self._c["fresh"]

        # カラーティント
        cold = QColor(20, 50, 110)
        warm = QColor(255, 150, 70)
        tint_col = _lerp_color(cold, warm, v)
        tint_strength = 0.12 + abs(v - 0.5) * 0.22
        tint_col.setAlpha(int(255 * tint_strength))
        qp.fillRect(self.rect(), tint_col)

        # Engagement 低で暗く
        if e < 0.5:
            dim = int((0.5 - e) * 2.0 * 70)
            qp.fillRect(self.rect(), QColor(0, 0, 20, dim))

        # HR リング
        self._paint_rings(qp, w, h, now)

        # グリッター
        self._paint_glitter(qp, w, h, a, v)

        # 泡
        self._paint_bubbles(qp, w, h)

        # 霧
        fog = (1.0 - hsi) * 0.45 + (1.0 - fresh) * 0.25
        if fog > 0.02:
            fog_col = QColor(230, 235, 245, int(min(0.8, fog) * 255))
            qp.fillRect(self.rect(), fog_col)

        # ビネット
        vg = QRadialGradient(QPointF(w * 0.5, h * 0.5), max(w, h) * 0.75)
        vg.setColorAt(0.55, QColor(0, 0, 0, 0))
        vg.setColorAt(1.0, QColor(0, 0, 0, 110))
        qp.fillRect(QRectF(0, 0, w, h), vg)

        qp.end()

    def _paint_rings(self, qp, w, h, now):
        if not self._rings:
            return
        cx = w * 0.5
        cy = h * 0.62
        qp.save()
        qp.setBrush(Qt.NoBrush)
        for (birth, strength) in self._rings:
            age = now - birth
            if age < 0 or age > 2.5:
                continue
            t = age / 2.5
            radius = (45 + 280 * t) * (0.7 + 0.3 * strength)
            alpha = int(230 * (1.0 - t) ** 1.3 * strength)
            if alpha < 4:
                continue
            col = QColor(255, 235, 215, alpha)
            pen = QPen(col, 2.0 + 2.8 * (1.0 - t))
            qp.setPen(pen)
            qp.drawEllipse(QPointF(cx, cy), radius, radius * 0.32)
        qp.restore()

    def _paint_glitter(self, qp, w, h, a, v):
        qp.save()
        qp.setPen(Qt.NoPen)
        n_points = int(30 + a * 130)
        rng = random.Random(int(self._wave_phase * 2.0) & 0xfff)
        horizon = h * 0.45
        base_col = _lerp_color(QColor(180, 210, 255), QColor(255, 220, 170), v)
        for _ in range(n_points):
            t = rng.random()
            y = horizon + (h - horizon) * (t ** 1.3)
            spread = 30 + t * w * 0.35
            cx = w * 0.5 + (rng.random() - 0.5) * 2 * spread
            flick = 0.3 + 0.7 * rng.random()
            flick *= 0.5 + 0.5 * math.sin(
                self._wave_phase * 2.2 + cx * 0.05 + t * 5.0)
            if flick < 0.15:
                continue
            alpha = int(200 * flick * (0.3 + 0.7 * t))
            if alpha < 6:
                continue
            col = QColor(base_col.red(), base_col.green(),
                         base_col.blue(), alpha)
            qp.setBrush(col)
            r = 0.8 + t * 2.0 + rng.random() * 0.6
            qp.drawEllipse(QPointF(cx, y), r, r * 0.5)
        qp.restore()

    def _paint_bubbles(self, qp, w, h):
        if not self._bubbles:
            return
        qp.save()
        qp.setPen(Qt.NoPen)
        for b in self._bubbles:
            x, y, _vy, r, life, max_life = b
            life_t = life / max_life
            alpha = int(170 * (1.0 - life_t))
            if alpha < 4:
                continue
            qp.setBrush(QColor(255, 255, 255, alpha))
            qp.drawEllipse(QPointF(x, y), r, r)
        qp.restore()

    # --- lifecycle -------------------------------------------------------
    def stop(self):
        for src in self._sources.values():
            src.release()

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)


# ==========================================================================
if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)
    w = SeaWidget()
    w.resize(960, 600)
    w.setWindowTitle("Sea (demo)")
    w.show()

    demo_t0 = time.monotonic()

    def demo_tick():
        t = time.monotonic() - demo_t0
        # 20s ごとに calm → golden → storm を巡回
        phase = (t % 60) / 20
        if phase < 1:
            w.set_state(arousal=0.3, valence=0.5, engagement=0.5, hr_bpm=65)
        elif phase < 2:
            w.set_state(arousal=0.45, valence=0.8, engagement=0.6, hr_bpm=70)
        else:
            w.set_state(arousal=0.85, valence=0.2, engagement=0.7, hr_bpm=95)

    demo_timer = QTimer()
    demo_timer.timeout.connect(demo_tick)
    demo_timer.start(200)

    sys.exit(app.exec_())
