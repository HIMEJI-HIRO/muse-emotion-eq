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
import contextlib
import math
import os
import random
import sys
import time

# cv2/ffmpeg の冗長な警告 (h264 mmco unref 等) を抑制
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
os.environ.setdefault("OPENCV_LOG_LEVEL", "OFF")

from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import (QColor, QFont, QImage, QPainter, QPen, QPixmap,
                         QRadialGradient)
from PyQt5.QtWidgets import QWidget

try:
    import cv2
    HAS_CV2 = True
    try:
        cv2.setLogLevel(0)   # SILENT
    except Exception:
        pass
except ImportError:
    HAS_CV2 = False


# --- stderr 抑制 (FFmpeg の native ログを潰す) ---
@contextlib.contextmanager
def _suppress_stderr():
    """ファイルディスクリプタレベルで stderr を /dev/null へ."""
    try:
        old_fd = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 2)
            yield
        finally:
            os.dup2(old_fd, 2)
            os.close(devnull)
            os.close(old_fd)
    except Exception:
        yield  # 失敗しても処理は続ける


# ---- アセット ------------------------------------------------------------
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "assets", "sea")
VIDEO_CALM = os.path.join(ASSETS_DIR, "sea_calm.mp4")
VIDEO_GOLDEN = os.path.join(ASSETS_DIR, "sea_golden.mp4")
VIDEO_STORM = os.path.join(ASSETS_DIR, "sea_storm.mp4")
# 海面 morph 動画 (8秒で 0→calm, 3→golden, 6→stormy へ連続変化)
VIDEO_SURFACE_MORPH = os.path.join(ASSETS_DIR, "sea_surface_morph.mp4")
# 水中動画 (HR で切替)
VIDEO_UNDERWATER_LOW = os.path.join(ASSETS_DIR, "sea_underwater_low.mp4")
VIDEO_UNDERWATER_MID = os.path.join(ASSETS_DIR, "sea_underwater_mid.mp4")
VIDEO_UNDERWATER_HIGH = os.path.join(ASSETS_DIR, "sea_underwater_high.mp4")

# 注: City / Forest サブビューは 2026-05 に廃止.
# Watch は Surface (🧠 EEG 駆動) / Underwater (♥ HR 駆動) の 2 ビュー構成に統一.
# 旧アセット (sea_city.mp4 / sea_forest.mp4 / bg_city.png) は LFS 履歴のため残置.

# 水中シーン HR 閾値 (2 ゾーンに簡略化)
# LOW (サンゴ礁 + 魚) ↔ HIGH (ジンベエザメ) の 2 段階. ヒステリシス幅 10 BPM.
HR_HIGH_ENTER = 82.0   # ≥ ここで LOW → HIGH (ジンベエ登場)
HR_HIGH_EXIT  = 72.0   # ≤ ここで HIGH → LOW (サンゴ礁に戻る)
HR_MIN_DWELL_SEC = 6.0
HR_EMA_ALPHA = 0.02   # ~3秒 τ

# morph 動画内のシーン代表時刻 (秒)
MORPH_TIME_CALM = 1.5
MORPH_TIME_GOLDEN = 4.0
MORPH_TIME_STORM = 6.5

# scrub の追従速度 (位置を目標へ寄せる強さ, 1/秒)
SCRUB_PULL = 0.6
# 通常の前進速度倍率 (Arousal で 0.5〜1.4)
SCRUB_BASE_RATE = 1.0


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
    """cv2.VideoCapture を抱え、次フレームの QImage を供給するクラス.

    通常: update(now) で時間経過に応じて自動前進.
    Morph mode: seek(t_sec) で任意の時刻にジャンプして 1 フレーム取得.
    """

    # ループ境界のクロスフェード時間 (秒). 動画末尾の最後 X 秒で
    # 先頭フレーム (seam image) を上に重ねて jump cut を隠す.
    SEAM_FADE_SEC = 0.6

    def __init__(self, path, loop_start=None, loop_end=None):
        """loop_start / loop_end (秒) を指定すると、その区間内だけループ.
        Veo 生成動画でイントロやアウトロに空フレームがある場合に有効."""
        self.path = path
        self.available = HAS_CV2 and os.path.exists(path)
        self._cap = None
        self._fps = 30.0
        self._frame_interval = 1.0 / 30.0
        self._duration = 0.0
        self._last_grab = 0.0
        self._cur_image = None
        self._rate = 1.0
        # ループ範囲 (None なら動画全体)
        self._loop_start = loop_start
        self._loop_end = loop_end
        # ループ境界 jump cut 隠蔽用: ループ start のフレームをキャッシュ
        self._seam_image = None
        # seam blend を使うか (デフォルト False に: 空フレームと重なる事故防止)
        self._seam_blend_enabled = False
        if self.available:
            self._open()

    def _open(self):
        self._cap = cv2.VideoCapture(self.path)
        fps = self._cap.get(cv2.CAP_PROP_FPS)
        if fps and fps > 1.0:
            self._fps = float(fps)
        self._frame_interval = 1.0 / self._fps
        n_frames = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._duration = n_frames / self._fps if self._fps > 0 else 0.0
        self._cur_video_time = 0.0   # 直近に読んだフレームの動画内時刻

    @property
    def duration(self):
        return self._duration

    def set_rate(self, rate):
        self._rate = max(0.1, min(3.0, float(rate)))

    def _loop_wrap_seconds(self):
        """ループ範囲 (start, end). 未指定なら動画全体."""
        start = self._loop_start if self._loop_start is not None else 0.0
        end = (self._loop_end if self._loop_end is not None
               else self._duration)
        return start, end

    def update(self, now):
        """通常再生モード: 経過時間で次フレーム読み込み.
        loop_start / loop_end を指定していると、その区間内でだけループ."""
        if not self.available or self._cap is None:
            return
        interval = self._frame_interval / self._rate
        if (now - self._last_grab) < interval:
            return
        loop_start, loop_end = self._loop_wrap_seconds()
        try:
            with _suppress_stderr():
                # loop_end を超えていたら手動で loop_start に seek
                if (loop_end > 0
                        and self._cur_video_time >= loop_end - self._frame_interval):
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES,
                                  int(loop_start * self._fps))
                    self._cur_video_time = loop_start
                ok, frame = self._cap.read()
                if not ok:
                    # 物理的な末尾に当たった場合も loop_start に
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES,
                                  int(loop_start * self._fps))
                    self._cur_video_time = loop_start
                    ok, frame = self._cap.read()
        except Exception:
            ok, frame = False, None
        if not ok:
            return
        self._set_image(frame)
        self._cur_video_time += self._frame_interval
        self._last_grab = now

    def seek_to(self, t_sec):
        """指定秒数のフレームにジャンプ + 1 フレーム読み込み.
        起動時に空フレーム回避用に呼ぶ."""
        if not self.available or self._cap is None:
            return
        if self._duration <= 0:
            return
        t = max(0.0, min(self._duration - 0.1, float(t_sec)))
        target_frame = int(t * self._fps)
        try:
            with _suppress_stderr():
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                ok, frame = self._cap.read()
            if ok:
                self._set_image(frame)
                # seam image は frame 0 を取りたいので、別途 frame 0 を読む
                if self._seam_image is None:
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok2, frame0 = self._cap.read()
                    if ok2:
                        # 一旦 frame 0 を seam に保存し、すぐ target に戻す
                        self._set_image(frame0)
                        self._seam_image = QImage(self._cur_image)
                    # target_frame に戻す
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                    ok3, frame_again = self._cap.read()
                    if ok3:
                        self._set_image(frame_again)
                self._cur_video_time = t + self._frame_interval
        except Exception:
            pass

    # --- ループ継ぎ目隠し用 API ----------------------------------------
    def seam_image(self):
        """先頭フレーム (loop 開始点) の QImage."""
        return self._seam_image

    def seam_blend_factor(self):
        """seam blend が無効なら 0.0 を返す (デフォルト).
        有効時は loop_end までの残り時間で 0..1 の不透明度を smoothstep."""
        if not self._seam_blend_enabled:
            return 0.0
        if self._duration <= 0 or self._seam_image is None:
            return 0.0
        _, loop_end = self._loop_wrap_seconds()
        if loop_end <= 0:
            return 0.0
        remaining = loop_end - self._cur_video_time
        if remaining >= self.SEAM_FADE_SEC:
            return 0.0
        u = max(0.0, min(1.0, 1.0 - (remaining / self.SEAM_FADE_SEC)))
        return u * u * (3.0 - 2.0 * u)

    # スクラブ用 — seek はキーフレーム境界 (1秒単位) に丸めて頻度を激減
    SEEK_QUANTIZE_SEC = 1.0    # この秒数の倍数にしか seek しない
    SEEK_THRESHOLD = 0.6       # ここを超えてズレた時だけ seek 検討
    def scrub_read(self, t_sec, now):
        """目標時刻 t_sec に近づくよう次フレームを読む.
        通常は前進 cap.read(). ズレ大なら GOP 境界に seek."""
        if not self.available or self._cap is None:
            return
        interval = self._frame_interval / max(0.5, self._rate)
        if (now - self._last_grab) < interval:
            return

        if self._duration > 0:
            t_sec = t_sec % self._duration

        delta = t_sec - self._cur_video_time
        if self._duration > 0:
            if delta > self._duration / 2:
                delta -= self._duration
            elif delta < -self._duration / 2:
                delta += self._duration

        need_seek = abs(delta) > self.SEEK_THRESHOLD or delta < -0.1

        if need_seek:
            # 1 秒境界に丸めて GOP のキーフレームに乗せる
            seek_t = round(t_sec / self.SEEK_QUANTIZE_SEC) * self.SEEK_QUANTIZE_SEC
            if self._duration > 0:
                seek_t = seek_t % self._duration
            target_frame = int(seek_t * self._fps)
            try:
                with _suppress_stderr():
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            except Exception:
                pass
            self._cur_video_time = seek_t

        try:
            with _suppress_stderr():
                ok, frame = self._cap.read()
        except Exception:
            ok, frame = False, None
        if not ok:
            try:
                with _suppress_stderr():
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = self._cap.read()
            except Exception:
                ok, frame = False, None
            if not ok:
                return
            self._cur_video_time = 0.0
        self._set_image(frame)
        if not need_seek:
            self._cur_video_time += self._frame_interval
            if self._duration > 0 and self._cur_video_time >= self._duration:
                self._cur_video_time = 0.0
        self._last_grab = now

    def seek_read(self, t_sec):
        self.scrub_read(t_sec, time.monotonic())

    def _set_image(self, frame):
        h, w, _ = frame.shape
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        self._cur_image = QImage(rgb.data, w, h, 3 * w,
                                 QImage.Format_RGB888).copy()

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


def _scene_to_morph_time(arousal, valence, duration):
    """感情 (slow EMA 後) → morph 動画の目標タイムスタンプ (秒).
    Calm (低A中V) → MORPH_TIME_CALM
    Golden (高V)   → MORPH_TIME_GOLDEN
    Storm (高A低V) → MORPH_TIME_STORM
    その間は重み付き平均.
    """
    # 各シーンへの "近さ" を 0-1 で評価
    # calm: 低 arousal & 中庸 valence
    w_calm = max(0.0, 1.0 - 2.0 * arousal) * (1.0 - abs(valence - 0.5))
    # golden: 高 valence
    w_golden = max(0.0, (valence - 0.4) / 0.6)
    # storm: 高 arousal & 低 valence
    w_storm = max(0.0, (arousal - 0.4) / 0.6) * max(0.0, (0.5 - valence) / 0.5)
    # ベースに少しの calm を底上げ (全部 0 にならないように)
    w_calm += 0.1
    total = w_calm + w_golden + w_storm
    t = (w_calm * MORPH_TIME_CALM
         + w_golden * MORPH_TIME_GOLDEN
         + w_storm * MORPH_TIME_STORM) / total
    return max(0.0, min(duration - 0.1, t))


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

        # --- Morph モード判定 ---
        # sea_surface_morph.mp4 があれば 1 本動画スクラブ方式.
        # 無ければ従来 3 シーンクロスフェード方式.
        self._morph_source = None
        self._use_morph = False
        if HAS_CV2 and os.path.exists(VIDEO_SURFACE_MORPH):
            ms = _VideoSource(VIDEO_SURFACE_MORPH)
            if ms.available and ms.duration > 0.5:
                self._morph_source = ms
                self._use_morph = True

        if self._use_morph:
            self._sources = {}
            self._available = []
            # scrub state (秒)
            self._scrub_pos = MORPH_TIME_CALM
            self._scrub_target = MORPH_TIME_CALM
        else:
            self._sources = {n: _VideoSource(SCENE_PATHS[n]) for n in SCENES}
            self._available = [n for n, s in self._sources.items() if s.available]
            if not self._available:
                print("[SeaWidget] 動画なし or cv2 未インストール")
                self._available = ["calm"]

        # シーン遷移 (3 シーンモード用)
        self._current = self._available[0] if self._available else "calm"
        self._target = self._current
        self._fade_start = 0.0
        self._fading = False

        # ===== Underwater モード =====
        # サブビュー切替 ("surface" / "underwater")
        # surface = 🧠 EEG (Arousal/Valence/Engagement) で動く
        # underwater = ♥ HR (心拍) で動く
        self._sub_view = "surface"
        self._uw_sources = {}
        # 2 ゾーン構成:
        #   "low"  -> sea_underwater_mid.mp4  (サンゴ礁 + 魚群 = 平常)
        #   "high" -> sea_underwater_high.mp4 (ジンベエザメ = 興奮)
        # ※ sea_underwater_low.mp4 (空っぽの水中) は今後使わない.
        # 各動画の「subject (魚/ジンベエ) が映ってる区間」を loop window で
        # 指定. Veo 8 秒生成は冒頭/末尾に空フレームが入りがちなので
        # その範囲を回避する.
        uw_config = {
            # name -> (file_path, loop_start_sec, loop_end_sec, warmup_sec)
            "low":  (VIDEO_UNDERWATER_MID,  0.5, 7.5, 2.0),
            "high": (VIDEO_UNDERWATER_HIGH, 1.5, 7.0, 3.0),
        }
        for name, (path, ls, le, warm) in uw_config.items():
            src = _VideoSource(path, loop_start=ls, loop_end=le)
            if src.available:
                src.seek_to(warm)
                self._uw_sources[name] = src
        self._uw_available = bool(self._uw_sources)
        self._uw_current = "low"
        self._uw_target = "low"
        self._uw_fade_start = 0.0
        self._uw_fading = False
        self._hr_ema = 60.0
        self._uw_last_switch = 0.0
        self._flash_start = 0.0
        self._flash_zone = ""

        # サブビュー切替ボタン (top-left, 半透明オーバーレイ)
        self._sub_btns_widget = QWidget(self)
        sb_lay = self._sub_btns_widget.children()  # placeholder
        from PyQt5.QtWidgets import QHBoxLayout, QPushButton
        h = QHBoxLayout(self._sub_btns_widget)
        h.setContentsMargins(8, 8, 8, 8)
        h.setSpacing(4)
        self._sub_btns = {}
        # 駆動源をボタン文言に明記 ("見た人がすぐ何で動くか分かる" 設計)
        for key, label in [("surface", "🌊 Surface  ·  🧠 EEG"),
                            ("underwater", "🌊 Underwater  ·  ♥ HR")]:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(28)
            b.clicked.connect(lambda _, k=key: self.set_sub_view(k))
            h.addWidget(b)
            self._sub_btns[key] = b
        self._sub_btns["surface"].setChecked(True)
        h.addStretch()
        self._sub_btns_widget.move(8, 8)
        self._sub_btns_widget.resize(420, 44)
        self._restyle_sub_btns()
        if not self._uw_available:
            self._sub_btns["underwater"].setEnabled(False)
            self._sub_btns["underwater"].setToolTip(
                "水中動画 (sea_underwater_*.mp4) が見つかりません")
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
            # HR EMA (Underwater 切替判定用、別系統)
            self._hr_ema += HR_EMA_ALPHA * (float(hr_bpm) - self._hr_ema)
            self._update_underwater_scene()
        if hsi is not None:
            self._t["hsi"] = max(0.0, min(1.0, float(hsi)))
        if signal_fresh is not None:
            self._t["fresh"] = max(0.0, min(1.0, float(signal_fresh)))
        # シーン選択 / scrub 目標更新
        if arousal is not None and valence is not None:
            self._scene_a += SCENE_EMA_ALPHA * (float(arousal) - self._scene_a)
            self._scene_v += SCENE_EMA_ALPHA * (float(valence) - self._scene_v)
            if self._use_morph:
                # 感情 → morph 動画内の目標タイムスタンプを補間
                self._scrub_target = _scene_to_morph_time(
                    self._scene_a, self._scene_v,
                    self._morph_source.duration if self._morph_source else 8.0)
            else:
                now = time.monotonic()
                if (now - self._last_switch_time) >= SCENE_MIN_DWELL_SEC:
                    want = _pick_scene_hysteresis(
                        self._target, self._scene_a, self._scene_v)
                    if want in self._available and want != self._target:
                        self._request_scene(want)
                        self._last_switch_time = now

    def trigger_pulse(self, strength=1.0):
        self._rings.append((time.monotonic(), float(strength)))

    def set_sub_view(self, name):
        if name not in ("surface", "underwater"):
            return
        if name == "underwater" and not self._uw_available:
            return
        if name == self._sub_view:
            return
        self._sub_view = name
        for k, b in self._sub_btns.items():
            b.setChecked(k == name)
        # 選択状態に応じてスタイル再適用 (これしないと active 色が反映されない)
        self._restyle_sub_btns()

    def _restyle_sub_btns(self):
        """サブビューボタンのスタイル (半透明 + 選択時に強い accent 発光)."""
        for key, btn in self._sub_btns.items():
            active = btn.isChecked()
            if active:
                btn.setStyleSheet(
                    "QPushButton { background-color: rgba(26,188,156,230); "
                    "color: #ffffff; border: 2px solid #1abc9c; "
                    "border-radius: 13px; padding: 2px 14px; "
                    "font-size: 11px; font-weight: bold; "
                    "letter-spacing: 1px; }")
                # 発光エフェクト (DropShadow)
                from PyQt5.QtWidgets import QGraphicsDropShadowEffect
                eff = btn.graphicsEffect()
                if not isinstance(eff, QGraphicsDropShadowEffect):
                    eff = QGraphicsDropShadowEffect(btn)
                    btn.setGraphicsEffect(eff)
                eff.setColor(QColor(26, 188, 156, 220))
                eff.setBlurRadius(20)
                eff.setOffset(0, 0)
            else:
                btn.setStyleSheet(
                    "QPushButton { background-color: rgba(0,0,0,160); "
                    "color: #c0c0c0; "
                    "border: 1px solid rgba(255,255,255,50); "
                    "border-radius: 13px; padding: 2px 14px; "
                    "font-size: 11px; letter-spacing: 1px; }"
                    "QPushButton:hover { background-color: rgba(255,255,255,40); "
                    "color: #ffffff; border: 1px solid #1abc9c; }"
                    "QPushButton:disabled { color: #555; "
                    "border: 1px solid #333; }")
                btn.setGraphicsEffect(None)

    def resizeEvent(self, event):
        # サブビューボタン位置を維持
        if hasattr(self, "_sub_btns_widget"):
            self._sub_btns_widget.move(8, 8)
        super().resizeEvent(event)

    # --- Underwater シーン切替 (HR ヒステリシス, 2 ゾーン) ---------------
    def _update_underwater_scene(self):
        """HR EMA に応じて LOW (サンゴ礁) ↔ HIGH (ジンベエ) を切替.
        ヒステリシス: ENTER 82 / EXIT 72 BPM. dwell 6 秒."""
        if not self._uw_available:
            return
        cur = self._uw_target
        hr = self._hr_ema
        want = cur
        if cur == "low" and hr >= HR_HIGH_ENTER:
            want = "high"
        elif cur == "high" and hr <= HR_HIGH_EXIT:
            want = "low"
        if want != cur and want in self._uw_sources:
            now = time.monotonic()
            if (now - self._uw_last_switch) >= HR_MIN_DWELL_SEC:
                self._uw_target = want
                self._uw_fade_start = now
                self._uw_fading = True
                self._uw_last_switch = now
                # ゾーン跨ぎフラッシュトリガ
                self._flash_start = now
                self._flash_zone = want

    def _uw_fade_progress(self, now):
        if not self._uw_fading:
            return 1.0
        t = (now - self._uw_fade_start) / self.CROSSFADE_SEC
        if t >= 1.0:
            self._uw_current = self._uw_target
            self._uw_fading = False
            return 1.0
        return t

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
        # isVisible() チェックを外す: reparent 直後に False を返す Qt の挙動で
        # 動画が黒のままになる事象があったため. 描画は paintEvent に任せ、
        # 隠れているときは Qt 側がそもそも paintEvent を呼ばないので
        # 余分な負荷は小さい.
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

        # 動画フレーム更新
        if self._sub_view == "underwater" and self._uw_available:
            # 水中: ★ 全 3 本を常時デコード ★
            # 目的: シーン切替時に「frame 0 から開始 → 何もない水中」を避ける.
            # 切替先の動画も裏で進めているので、ジンベエが既に画面中央を
            # 泳いでいる瞬間にクロスフェード in できる.
            rate = 0.7 + self._c["arousal"] * 0.4   # 覚醒で少し速く
            for src in self._uw_sources.values():
                src.set_rate(rate)
                src.update(now)
        elif self._use_morph and self._morph_source is not None:
            # シンプル前進ループ. Arousal で再生速度のみ変える.
            # 動画自体に morph が入っているので emotion 連動を捨てても見栄え◯
            rate = 0.5 + self._c["arousal"] * 0.9
            self._morph_source.set_rate(rate)
            self._morph_source.update(now)
        else:
            rate = 0.7 + self._c["arousal"] * 0.7
            for src in self._sources.values():
                src.set_rate(rate)
            active_scenes = {self._current, self._target}
            for name in active_scenes:
                if name in self._sources:
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

        if self._sub_view == "underwater" and self._uw_available:
            self._draw_underwater_frame(qp, w, h, now)
        elif self._use_morph:
            self._draw_morph_frame(qp, w, h)
        elif self._fading and self._target != self._current:
            draw_frame(self._current, 1.0 - progress)
            draw_frame(self._target, progress)
        else:
            scene = self._target if self._fading else self._current
            if scene in self._sources:
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

        # HR ゾーン跨ぎフラッシュ (1.2 秒で減衰)
        if self._flash_start > 0:
            elapsed = time.monotonic() - self._flash_start
            if elapsed < 1.2:
                t = 1.0 - (elapsed / 1.2)   # 1→0
                flash_color = {"low": QColor(80, 180, 255),
                                "mid": QColor(120, 240, 160),
                                "high": QColor(255, 120, 180)}.get(
                    self._flash_zone, QColor(255, 255, 255))
                # 4辺に向かう glow vignette (内側透明 / 外側 flash)
                flash_alpha = int(150 * t)
                fg = QRadialGradient(QPointF(w * 0.5, h * 0.5),
                                      max(w, h) * 0.7)
                fg.setColorAt(0.4, QColor(flash_color.red(),
                                            flash_color.green(),
                                            flash_color.blue(), 0))
                fg.setColorAt(1.0, QColor(flash_color.red(),
                                            flash_color.green(),
                                            flash_color.blue(),
                                            flash_alpha))
                qp.fillRect(QRectF(0, 0, w, h), fg)

        # --- 駆動源バッジ (top-right) ---
        # 「この映像は何で動いているか」を一目で見せる. デモを見る人向け.
        self._paint_driver_badge(qp, w, h)

        qp.end()

    def _paint_driver_badge(self, qp, w, h):
        """画面右上に「駆動源 + 現在値」を半透明ピルで常時表示.
        - emoji の幅測定が Qt で不安定なため、left-side アクセントバー +
          固定 left padding で「emoji がはみ出ても切れない」レイアウトに.
        - Underwater は心拍だけを大きく見せる (α/β は無関係なので非表示).
        """
        if self._sub_view == "surface":
            emoji = "🧠"
            title = "EEG-DRIVEN"
            a = self._c["arousal"]
            v = self._c["valence"]
            e = self._c["engagement"]
            line2 = f"Arousal {a:.2f}   Valence {v:.2f}   Engage {e:.2f}"
            accent = QColor(155, 89, 182)   # purple = brain
        else:   # underwater
            hr = self._hr_ema
            zone = self._uw_target.upper() if self._uw_available else "—"
            emoji = "♥"
            title = "HR-DRIVEN"
            line2 = f"{hr:5.1f} BPM    zone: {zone}"
            accent = QColor(231, 76, 60)    # red = heart

        # フォント (一段大きく)
        title_font = QFont("Segoe UI", 12, QFont.Bold)
        title_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
        line2_font = QFont("Consolas", 11)
        emoji_font = QFont("Segoe UI Emoji", 16)

        # ピル寸法 (text 切れ防止のため右側に +24px の余白を取る)
        emoji_box_w = 32        # 絵文字ゾーン固定幅
        right_pad = 24          # 右端に余白 (text が切れないため)
        left_pad = 14
        # 文字列幅を測定
        qp.setFont(title_font)
        title_w = qp.fontMetrics().horizontalAdvance(title)
        qp.setFont(line2_font)
        line2_w = qp.fontMetrics().horizontalAdvance(line2)
        text_w = max(title_w, line2_w)
        pill_w = left_pad + emoji_box_w + text_w + right_pad
        pill_h = 60
        pill_x = w - pill_w - 14
        pill_y = 14

        # 半透明背景
        qp.setOpacity(0.94)
        qp.setPen(QPen(QColor(accent.red(), accent.green(),
                              accent.blue(), 200), 1.6))
        qp.setBrush(QColor(15, 15, 18, 230))
        qp.drawRoundedRect(QRectF(pill_x, pill_y, pill_w, pill_h),
                           pill_h / 2, pill_h / 2)

        # アクセント縦バー (左 emoji の更に左)
        qp.setPen(Qt.NoPen)
        qp.setBrush(accent)
        qp.drawRoundedRect(
            QRectF(pill_x + 8, pill_y + 10, 3, pill_h - 20), 1.5, 1.5)

        # 絵文字 (固定ゾーン、中央寄せ)
        qp.setFont(emoji_font)
        qp.setPen(accent)
        qp.drawText(QRectF(pill_x + left_pad, pill_y,
                           emoji_box_w, pill_h),
                    Qt.AlignCenter, emoji)

        # タイトル行 (上)
        text_x = pill_x + left_pad + emoji_box_w
        text_w_avail = pill_w - left_pad - emoji_box_w - right_pad
        qp.setFont(title_font)
        qp.setPen(accent)
        qp.drawText(QRectF(text_x, pill_y + 8,
                           text_w_avail, 20),
                    Qt.AlignLeft | Qt.AlignVCenter, title)
        # 2行目 (現在値)
        qp.setFont(line2_font)
        qp.setPen(QColor(235, 235, 235))
        qp.drawText(QRectF(text_x, pill_y + 30,
                           text_w_avail, 20),
                    Qt.AlignLeft | Qt.AlignVCenter, line2)
        qp.setOpacity(1.0)

    def _draw_underwater_frame(self, qp, w, h, now):
        """Underwater モード: low/mid/high 動画を crossfade.

        各動画は 8 秒で末尾→先頭ジャンプするため、最後 0.6 秒で
        先頭フレーム (seam_image) を上に重ねて jump cut を隠す.
        """
        prog = self._uw_fade_progress(now)

        def _fit_rect(img):
            src_ratio = img.width() / max(1, img.height())
            dst_ratio = w / max(1, h)
            if src_ratio > dst_ratio:
                draw_h = h
                draw_w = int(draw_h * src_ratio)
                x = (w - draw_w) // 2
                y = 0
            else:
                draw_w = w
                draw_h = int(draw_w / src_ratio)
                x = 0
                y = (h - draw_h) // 2
            return QRectF(x, y, draw_w, draw_h)

        def draw_uw(name, opacity):
            if opacity <= 0.01:
                return
            src = self._uw_sources.get(name)
            if src is None:
                return
            img = src.image()
            if img is None:
                return
            r = _fit_rect(img)
            # ベースフレーム描画
            qp.setOpacity(opacity)
            qp.drawImage(r, img)
            # ループ境界クロスフェード (jump cut 隠し)
            seam_f = src.seam_blend_factor()
            if seam_f > 0.01:
                seam_img = src.seam_image()
                if seam_img is not None:
                    qp.setOpacity(opacity * seam_f)
                    qp.drawImage(_fit_rect(seam_img), seam_img)

        if self._uw_fading and self._uw_target != self._uw_current:
            draw_uw(self._uw_current, 1.0 - prog)
            draw_uw(self._uw_target, prog)
        else:
            draw_uw(self._uw_target, 1.0)
        qp.setOpacity(1.0)

    def _draw_morph_frame(self, qp, w, h):
        """morph モード: 1 本の動画から現在フレームを描画 (loop seam fade 込み)."""
        if self._morph_source is None:
            return
        img = self._morph_source.image()
        if img is None:
            return
        # 通常フレーム
        src_ratio = img.width() / max(1, img.height())
        dst_ratio = w / max(1, h)
        if src_ratio > dst_ratio:
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
        # ループ境界のクロスフェード (surface の morph 動画は 8秒ループ)
        seam_f = self._morph_source.seam_blend_factor()
        if seam_f > 0.01:
            seam_img = self._morph_source.seam_image()
            if seam_img is not None:
                s_ratio = seam_img.width() / max(1, seam_img.height())
                if s_ratio > dst_ratio:
                    sdh = h; sdw = int(sdh * s_ratio)
                    sx = (w - sdw) // 2; sy = 0
                else:
                    sdw = w; sdh = int(sdw / s_ratio)
                    sx = 0; sy = (h - sdh) // 2
                qp.setOpacity(seam_f)
                qp.drawImage(QRectF(sx, sy, sdw, sdh), seam_img)
                qp.setOpacity(1.0)

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
        if self._morph_source is not None:
            self._morph_source.release()
        for src in self._uw_sources.values():
            src.release()
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
