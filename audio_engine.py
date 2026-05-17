"""
audio_engine.py
===============
VB-CABLE → pedalboard (6-band EQ + Reverb) → ヘッドホン (WF-1000XM5).

EQ バンドは「楽器別」に配置:
  drums  80 Hz  LowShelf    キックの胴
  bass  180 Hz  Peak Q=1.2  ベースの胴鳴り
  mid  1000 Hz  Peak Q=1.0  ギター/キーボード
  vocal 2800 Hz Peak Q=1.4  ボーカル存在感
  high 6000 Hz  Peak Q=1.0  ハイ楽器 (ブラス・リード・ストリングス)
  air 10000 Hz  HighShelf   コーラス/空気感

Reverb は残す (空間的音響). Compressor は外した (音の途切れ対策).
ゲイン変更は Lock + GIL で atomic, GUI 側でスロットルされる想定.
"""
import threading

import numpy as np

try:
    import sounddevice as sd
    from pedalboard import (Pedalboard, LowShelfFilter, HighShelfFilter,
                            PeakFilter, Reverb)
    HAS_AUDIO = True
    _IMPORT_ERROR = None
except ImportError as e:
    HAS_AUDIO = False
    _IMPORT_ERROR = str(e)


SAMPLE_RATE = 48000
BLOCK_SIZE = 1024          # 512 → 1024 (≈21ms) 音飛び対策
LATENCY_HINT = "high"      # Bluetooth (WF-1000XM5) は低 latency 不可
CHANNELS = 2

INPUT_KEYWORDS = ["CABLE Output"]
# Bluetooth ヘッドホンは OS によって名称が変わるので候補を広く.
OUTPUT_KEYWORDS = [
    "WF-1000XM5", "WF-1000",
    "Headphones", "ヘッドホン", "Headphone",
    "Speakers", "スピーカー",
]
# 出力候補から除外すべきループバック系キーワード
OUTPUT_EXCLUDE = ["CABLE", "VB-Audio", "VoiceMeeter", "Virtual"]

GAIN_MIN_DB = -4.0
GAIN_MAX_DB = 4.0

# ---- 6 バンド定義 (UI とも共有する) -------------------------------------
# (key, emoji, label, freq_hz, filter_kind, q, blurb)
BANDS = [
    ("drums", "🥁", "Drums",    80.0,    "lowshelf",  0.7, "キックの胴"),
    ("bass",  "🎸", "Bass",    180.0,    "peak",      1.2, "ベースの胴鳴り"),
    ("mid",   "🎹", "Mid",    1000.0,    "peak",      1.0, "ギター/キー"),
    ("vocal", "🎤", "Vocals", 2800.0,    "peak",      1.4, "歌の存在感"),
    ("high",  "🎺", "High",   6000.0,    "peak",      1.0, "ブラス・リード"),
    ("air",   "🌟", "Air",   10000.0,    "highshelf", 0.7, "コーラス/空気感"),
]
BAND_KEYS = [b[0] for b in BANDS]

# Reverb: valence (快) で wet を上げて空間を広げる
REVERB_WET_MAX = 0.45
REVERB_ROOM_SIZE = 0.7
REVERB_DAMPING = 0.4


def _make_filter(kind, freq, q):
    if not HAS_AUDIO:
        return None
    if kind == "lowshelf":
        return LowShelfFilter(cutoff_frequency_hz=freq, gain_db=0.0, q=q)
    if kind == "highshelf":
        return HighShelfFilter(cutoff_frequency_hz=freq, gain_db=0.0, q=q)
    return PeakFilter(cutoff_frequency_hz=freq, gain_db=0.0, q=q)


def _is_excluded(name, excludes):
    nlo = name.lower()
    return any(x.lower() in nlo for x in excludes)


def find_device(name_part, kind, excludes=()):
    name_part = name_part.lower()
    for i, d in enumerate(sd.query_devices()):
        if _is_excluded(d["name"], excludes):
            continue
        if name_part in d["name"].lower():
            if kind == "input" and d["max_input_channels"] > 0:
                return i, d
            if kind == "output" and d["max_output_channels"] > 0:
                return i, d
    return None, None


def pick_input_device():
    for kw in INPUT_KEYWORDS:
        idx, dev = find_device(kw, "input")
        if idx is not None:
            return idx, dev
    return None, None


def find_input_on_hostapi(name_part, host_api_idx):
    """指定 host API 上で name_part を含む入力デバイスを探す."""
    name_part = name_part.lower()
    for i, d in enumerate(sd.query_devices()):
        if (d.get("hostapi") == host_api_idx
                and d["max_input_channels"] > 0
                and name_part in d["name"].lower()):
            return i, d
    return None, None


def pick_input_for_output(output_index):
    """出力デバイスと同じ host API 上の CABLE Output を返す.
    duplex stream の Illegal combination 回避用."""
    try:
        out_dev = sd.query_devices(output_index)
        host_api = out_dev.get("hostapi")
        for kw in INPUT_KEYWORDS:
            idx, dev = find_input_on_hostapi(kw, host_api)
            if idx is not None:
                return idx, dev
    except Exception:
        pass
    return None, None


def pick_output_device():
    """CABLE 系を除外して出力を探す.

    Windows の既定出力が CABLE Input になっているとフォールバックで
    ループバックしてしまうので, 既定出力でも CABLE は拒否する.
    """
    for kw in OUTPUT_KEYWORDS:
        idx, dev = find_device(kw, "output", excludes=OUTPUT_EXCLUDE)
        if idx is not None:
            return idx, dev
    # 既定出力 (ただし CABLE 系なら拒否)
    try:
        default_idx = sd.default.device[1]
        if default_idx is not None and default_idx >= 0:
            dev = sd.query_devices(default_idx)
            if not _is_excluded(dev["name"], OUTPUT_EXCLUDE):
                return default_idx, dev
    except Exception:
        pass
    # 最終フォールバック: 出力可能で除外に該当しない最初のデバイス
    try:
        for i, d in enumerate(sd.query_devices()):
            if (d["max_output_channels"] > 0
                    and not _is_excluded(d["name"], OUTPUT_EXCLUDE)):
                return i, d
    except Exception:
        pass
    return None, None


def list_output_devices():
    """UI 用: 出力可能なデバイス一覧 [(idx, name, is_excluded), ...] を返す."""
    out = []
    if not HAS_AUDIO:
        return out
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                out.append((i, d["name"],
                           _is_excluded(d["name"], OUTPUT_EXCLUDE)))
    except Exception:
        pass
    return out


class AudioEngine:
    """VB-CABLE → 6-band EQ → Reverb → ヘッドホン の音声スレッド管理."""

    def __init__(self, sample_rate=SAMPLE_RATE,
                 block_size=BLOCK_SIZE, channels=CHANNELS):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.channels = channels

        # 6-band filters
        self._filters = {}
        if HAS_AUDIO:
            for key, _emo, _name, freq, kind, q, _blurb in BANDS:
                self._filters[key] = _make_filter(kind, freq, q)
        # Reverb (wet 初期 0 = dry)
        self._reverb = (Reverb(room_size=REVERB_ROOM_SIZE,
                               damping=REVERB_DAMPING,
                               wet_level=0.0, dry_level=1.0)
                        if HAS_AUDIO else None)
        if HAS_AUDIO:
            chain = [self._filters[k] for k in BAND_KEYS] + [self._reverb]
            self._board = Pedalboard(chain)
        else:
            self._board = None

        self._stream = None
        self._lock = threading.Lock()
        self.input_name = None
        self.output_name = None
        self.last_error = _IMPORT_ERROR
        self.running = False

    # --- 6-band gain control (thread-safe) ---
    def set_band(self, key, db):
        if not HAS_AUDIO or key not in self._filters:
            return
        with self._lock:
            self._filters[key].gain_db = float(
                np.clip(db, GAIN_MIN_DB, GAIN_MAX_DB))

    def set_bands(self, **kwargs):
        """set_bands(drums=1.0, bass=-0.5, ...) のように一括設定."""
        if not HAS_AUDIO:
            return
        with self._lock:
            for k, v in kwargs.items():
                if v is None or k not in self._filters:
                    continue
                self._filters[k].gain_db = float(
                    np.clip(v, GAIN_MIN_DB, GAIN_MAX_DB))

    def get_band(self, key):
        if not HAS_AUDIO or key not in self._filters:
            return 0.0
        return float(self._filters[key].gain_db)

    def get_bands(self):
        if not HAS_AUDIO:
            return {k: 0.0 for k in BAND_KEYS}
        return {k: float(self._filters[k].gain_db) for k in BAND_KEYS}

    # --- Reverb (wet 0..1) ---
    def set_reverb_wet(self, wet):
        if not HAS_AUDIO:
            return
        w = float(np.clip(wet, 0.0, 1.0)) * REVERB_WET_MAX
        with self._lock:
            self._reverb.wet_level = w
            self._reverb.dry_level = 1.0 - w * 0.3

    def get_reverb_wet(self):
        if not HAS_AUDIO:
            return 0.0
        return self._reverb.wet_level / REVERB_WET_MAX

    def reset(self):
        self.set_bands(**{k: 0.0 for k in BAND_KEYS})
        self.set_reverb_wet(0.0)

    # --- sounddevice callback (別スレッド) ---
    def _callback(self, indata, outdata, frames, t, status):
        if status:
            self.last_error = str(status)
        try:
            out = self._board(indata.copy(), self.sample_rate)
        except Exception as e:
            self.last_error = f"pedalboard: {e}"
            outdata[:] = indata
            return
        if out.ndim == 1:
            out = out[:, None]
        if out.shape[1] == 1 and outdata.shape[1] == 2:
            out = np.repeat(out, 2, axis=1)
        n = min(out.shape[0], outdata.shape[0])
        c = min(out.shape[1], outdata.shape[1])
        outdata[:n, :c] = out[:n, :c]
        if n < outdata.shape[0]:
            outdata[n:] = 0

    # --- Stream lifecycle ---
    def start(self, output_index=None):
        """output_index: 手動で出力デバイスを指定したい場合. None なら自動."""
        if not HAS_AUDIO:
            self.last_error = (
                "sounddevice / pedalboard 未インストール: "
                "pip install sounddevice pedalboard")
            return False
        if self.running:
            return True

        # 出力を先に決める (host API を揃えるため)
        if output_index is not None:
            try:
                out_dev = sd.query_devices(output_index)
                if out_dev["max_output_channels"] <= 0:
                    self.last_error = "指定した出力デバイスは出力不可です"
                    return False
                if _is_excluded(out_dev["name"], OUTPUT_EXCLUDE):
                    self.last_error = (
                        f"{out_dev['name']} は VB-CABLE ループバック系のため "
                        "出力にできません")
                    return False
                out_idx = output_index
            except Exception as e:
                self.last_error = f"output device query: {e}"
                return False
        else:
            out_idx, out_dev = pick_output_device()
            if out_idx is None:
                self.last_error = (
                    "出力デバイスが見つかりません。"
                    "WF-1000XM5 を接続するかヘッダの Output ▾ から選択してください。")
                return False

        # 出力と同じ host API 上の CABLE Output を優先で選ぶ
        in_idx, in_dev = pick_input_for_output(out_idx)
        if in_idx is None:
            in_idx, in_dev = pick_input_device()
        if in_idx is None:
            self.last_error = (
                "'CABLE Output' が見つかりません。"
                "VB-CABLE と Spotify の出力設定を確認してください。")
            return False

        self.input_name = in_dev["name"]
        self.output_name = out_dev["name"]

        # duplex stream を試み、Illegal combination なら別 host API の入力で再試行
        def _try_open(i_idx, o_idx):
            return sd.Stream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                channels=self.channels,
                dtype="float32",
                device=(i_idx, o_idx),
                callback=self._callback,
                latency=LATENCY_HINT,
            )

        try:
            self._stream = _try_open(in_idx, out_idx)
            self._stream.start()
            self.running = True
            self.last_error = None
            return True
        except Exception as e:
            err_msg = str(e)
            # Illegal combination の場合、全 host API の CABLE Output を総当たり
            if "Illegal combination" in err_msg or "PaErrorCode -9993" in err_msg:
                for cand_idx, cand_dev in self._enumerate_cable_outputs():
                    if cand_idx == in_idx:
                        continue
                    try:
                        self._stream = _try_open(cand_idx, out_idx)
                        self._stream.start()
                        self.running = True
                        self.input_name = cand_dev["name"]
                        self.last_error = None
                        return True
                    except Exception:
                        continue
            self.last_error = f"stream start: {e}"
            self._stream = None
            return False

    @staticmethod
    def _enumerate_cable_outputs():
        """全 host API の CABLE Output エントリを返す."""
        if not HAS_AUDIO:
            return
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                for kw in INPUT_KEYWORDS:
                    if kw.lower() in d["name"].lower():
                        yield i, d
                        break

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self.running = False
