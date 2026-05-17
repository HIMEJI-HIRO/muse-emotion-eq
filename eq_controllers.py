"""
eq_controllers.py
=================
EEG → 6-band EQ + Reverb を動かす Reflect コントローラ.

感情 (arousal, valence, engagement) を各バンドの目標 dB + Reverb wet に
マッピングし, EMA で滑らかに追従. audio へのパラメータ送信は
スロットル (最小間隔 + 変化量しきい値) を入れて pedalboard の更新負荷を抑える.

目的: Spotify 再生中でも音が途切れないように GUI→audio 更新を節約する.
"""
import time

from audio_engine import BAND_KEYS


# 33ms/frame (GUI 30Hz) 想定, τ≈3s → α≈0.011
ALPHA_DEFAULT = 0.011

# audio へ push する最小間隔 (秒) と最小変化量
PUSH_MIN_INTERVAL_SEC = 0.10   # 10Hz
PUSH_MIN_DELTA_DB = 0.05
PUSH_MIN_DELTA_REVERB = 0.01


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _ema(cur, target, alpha):
    return cur + alpha * (target - cur)


class ReflectController:
    """感情 → 楽器バンド + Reverb にマッピング.

    マッピング方針 (-1..+1 にバイポーラ化して足し合わせ):
      Arousal  ↑: Drums + Vocals + High をブースト (エネルギー前面)
      Arousal  ↓: Bass + Air 方向 (低域 + 空気感)
      Valence  ↑: Air + Mid を少し持ち上げる (心地よさ)
      Valence  ↓: Drums 抑え気味 (重く沈む音)
      Engagement ↑: Vocals + Mid (芯・声の明瞭度)
      Engagement ↓: Drums + Bass (ぼんやり低域寄り)

      Valence  → Reverb wet (快 = 広く空間的)
    """

    def __init__(self, audio, gain_max_db=4.0, alpha=ALPHA_DEFAULT,
                 push_interval=PUSH_MIN_INTERVAL_SEC):
        self.audio = audio
        self.gain_max = gain_max_db
        self.alpha = alpha
        self.push_interval = push_interval
        # 滑らかに追従する現在値
        self._cur = {k: 0.0 for k in BAND_KEYS}
        self._cur["reverb"] = 0.0
        self._target = dict(self._cur)
        # 前回 audio に push した値 (delta 判定用)
        self._last_pushed = dict(self._cur)
        self._last_push_time = 0.0

    # ------------------------------------------------------------------
    def compute_target(self, rus, eng):
        arousal = float(rus.get("arousal", 0.5))
        valence = float(rus.get("valence", 0.5))
        engagement = float(eng) if eng is not None else 0.5

        # -1..+1 バイポーラ
        a = 2.0 * arousal - 1.0
        v = 2.0 * valence - 1.0
        e = 2.0 * engagement - 1.0

        g = self.gain_max

        # 各バンドへの寄与 (重みは合計で ±1 程度に収まるよう調整)
        tgt = {
            # Drums: 覚醒で前に、沈むと引き、valence 低で重く
            "drums": (0.6 * a - 0.3 * v - 0.2 * e) * g,
            # Bass: 低覚醒で前に出る (暖色・落ち着き)
            "bass": (-0.5 * a - 0.1 * e) * g,
            # Mid: 集中と少し快 (ギター/キーを前に)
            "mid": (0.55 * e + 0.2 * v) * g,
            # Vocals: 集中↑と覚醒↑で存在感↑
            "vocal": (0.55 * e + 0.35 * a) * g,
            # High 楽器: 覚醒で輝き
            "high": (0.7 * a + 0.15 * v) * g,
            # Air: 快で空気感、集中でも少し
            "air": (0.55 * v + 0.15 * e) * g,
        }
        # Reverb: 快で空間が広がる
        tgt["reverb"] = _clip(0.5 * (v + 1.0), 0.0, 1.0)

        # clip
        for k in BAND_KEYS:
            tgt[k] = _clip(tgt[k], -g, g)
        return tgt

    # ------------------------------------------------------------------
    def tick(self, rus, eng, now=None):
        """毎フレーム呼ばれる. EMA → audio push (スロットル済み)."""
        if now is None:
            now = time.monotonic()

        tgt = self.compute_target(rus, eng)
        self._target = tgt
        a = self.alpha
        for k in self._cur:
            self._cur[k] = _ema(self._cur[k], tgt[k], a)

        # スロットル: 前回 push から push_interval 未満ならスキップ、
        # ただし ``どこかのバンドが大きく動いた`` 場合は push する
        dt = now - self._last_push_time
        if dt < self.push_interval:
            max_db_delta = max(
                abs(self._cur[k] - self._last_pushed[k]) for k in BAND_KEYS)
            reverb_delta = abs(self._cur["reverb"] - self._last_pushed["reverb"])
            if (max_db_delta < PUSH_MIN_DELTA_DB
                    and reverb_delta < PUSH_MIN_DELTA_REVERB):
                return

        # audio に push (変化のあったバンドのみ)
        bands_payload = {}
        for k in BAND_KEYS:
            if abs(self._cur[k] - self._last_pushed[k]) >= PUSH_MIN_DELTA_DB:
                bands_payload[k] = self._cur[k]
        if bands_payload:
            self.audio.set_bands(**bands_payload)
            for k in bands_payload:
                self._last_pushed[k] = self._cur[k]

        if (abs(self._cur["reverb"] - self._last_pushed["reverb"])
                >= PUSH_MIN_DELTA_REVERB):
            self.audio.set_reverb_wet(self._cur["reverb"])
            self._last_pushed["reverb"] = self._cur["reverb"]

        self._last_push_time = now

    # ------------------------------------------------------------------
    def current(self):
        return dict(self._cur)

    def target(self):
        return dict(self._target)

    def seed_from_audio(self):
        """モード切替時に音飛び防止用. audio の現在値を EMA 初期値に."""
        bands = self.audio.get_bands()
        for k in BAND_KEYS:
            self._cur[k] = bands.get(k, 0.0)
            self._last_pushed[k] = self._cur[k]
        self._cur["reverb"] = self.audio.get_reverb_wet()
        self._last_pushed["reverb"] = self._cur["reverb"]
        self._target = dict(self._cur)
        self._last_push_time = 0.0
