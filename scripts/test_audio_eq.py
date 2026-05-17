"""
test_audio_eq.py
================
VB-CABLE から音声を受け取り、pedalboard で EQ を適用して
ヘッドホン/スピーカーに出力する最小プロトタイプ。

前提:
  - VB-CABLE インストール済み
  - Spotify の出力を "CABLE Input (VB-Audio Virtual Cable)" に設定済み
  - 出力デバイス (ヘッドホン / スピーカー) が接続済み

依存:
  pip install pedalboard sounddevice

キー操作 (Pythonターミナルにフォーカスを置いて押す):
  ↑ / ↓  : 低音 (Bass, LowShelf 200Hz) ゲイン ±1 dB
  ← / →  : 高音 (Treble, HighShelf 5kHz) ゲイン ±1 dB
  m / n  : 中音 (Mid, Peak 1kHz) ゲイン ±1 dB
  r      : 全ゲインリセット
  q      : 終了
"""
import sys
import msvcrt
import numpy as np
import sounddevice as sd
from pedalboard import Pedalboard, LowShelfFilter, HighShelfFilter, PeakFilter

SAMPLE_RATE = 48000
BLOCK_SIZE = 512
CHANNELS = 2


def find_device(name_part, kind):
    """デバイス名の部分一致で検索."""
    name_part = name_part.lower()
    for i, d in enumerate(sd.query_devices()):
        if name_part in d["name"].lower():
            if kind == "input" and d["max_input_channels"] > 0:
                return i, d
            if kind == "output" and d["max_output_channels"] > 0:
                return i, d
    return None, None


def pick_output_device():
    """ヘッドホン → デフォルト の順で検索."""
    for kw in ["ヘッドホン", "Headphone", "Headphones", "Speakers", "スピーカー"]:
        idx, dev = find_device(kw, "output")
        if idx is not None:
            return idx, dev
    default_idx = sd.default.device[1]
    return default_idx, sd.query_devices(default_idx)


def main():
    # ---- デバイス選択 ----
    in_idx, in_dev = find_device("CABLE Output", "input")
    if in_idx is None:
        print("❌ 'CABLE Output' が見つかりません。VB-CABLE のインストールを確認してください。")
        print("\n利用可能な入力デバイス:")
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"  [{i}] {d['name']}  ({d['max_input_channels']}ch)")
        sys.exit(1)

    out_idx, out_dev = pick_output_device()

    print("=" * 60)
    print(f"Input  : [{in_idx}] {in_dev['name']}")
    print(f"Output : [{out_idx}] {out_dev['name']}")
    print(f"Rate   : {SAMPLE_RATE} Hz / Block: {BLOCK_SIZE} / {CHANNELS}ch")
    print("=" * 60)

    # ---- Pedalboard ----
    low = LowShelfFilter(cutoff_frequency_hz=200.0, gain_db=0.0, q=0.7)
    mid = PeakFilter(cutoff_frequency_hz=1000.0, gain_db=0.0, q=1.0)
    high = HighShelfFilter(cutoff_frequency_hz=5000.0, gain_db=0.0, q=0.7)
    board = Pedalboard([low, mid, high])

    def print_status():
        print(f"  Bass: {low.gain_db:+5.1f} dB   "
              f"Mid: {mid.gain_db:+5.1f} dB   "
              f"Treble: {high.gain_db:+5.1f} dB")

    # ---- コールバック ----
    def audio_callback(indata, outdata, frames, t, status):
        if status:
            # XRun等の警告
            print(status, file=sys.stderr)
        try:
            out = board(indata.copy(), SAMPLE_RATE)
        except Exception as e:
            print(f"Pedalboard error: {e}")
            outdata[:] = indata
            return
        # チャンネル合わせ
        if out.ndim == 1:
            out = out[:, None]
        if out.shape[1] == 1 and outdata.shape[1] == 2:
            out = np.repeat(out, 2, axis=1)
        n = min(out.shape[0], outdata.shape[0])
        c = min(out.shape[1], outdata.shape[1])
        outdata[:n, :c] = out[:n, :c]
        if n < outdata.shape[0]:
            outdata[n:] = 0

    # ---- ストリーム開始 ----
    print("\nStreaming... (Spotifyで何か再生してみてください)")
    print("操作: ↑↓=Bass  ←→=Treble  m/n=Mid  r=Reset  q=Quit\n")

    try:
        with sd.Stream(samplerate=SAMPLE_RATE,
                       blocksize=BLOCK_SIZE,
                       channels=CHANNELS,
                       dtype="float32",
                       device=(in_idx, out_idx),
                       callback=audio_callback,
                       latency="low"):
            print_status()
            while True:
                if msvcrt.kbhit():
                    k = msvcrt.getch()
                    # 矢印キーは 2バイト (\xe0, \x00) + 方向コード
                    if k in (b"\xe0", b"\x00"):
                        k2 = msvcrt.getch()
                        if k2 == b"H":     # ↑
                            low.gain_db = min(low.gain_db + 1.0, 12.0)
                        elif k2 == b"P":   # ↓
                            low.gain_db = max(low.gain_db - 1.0, -12.0)
                        elif k2 == b"M":   # →
                            high.gain_db = min(high.gain_db + 1.0, 12.0)
                        elif k2 == b"K":   # ←
                            high.gain_db = max(high.gain_db - 1.0, -12.0)
                        print_status()
                    elif k == b"q":
                        print("Quitting...")
                        break
                    elif k == b"r":
                        low.gain_db = mid.gain_db = high.gain_db = 0.0
                        print("Reset")
                        print_status()
                    elif k == b"m":
                        mid.gain_db = min(mid.gain_db + 1.0, 12.0)
                        print_status()
                    elif k == b"n":
                        mid.gain_db = max(mid.gain_db - 1.0, -12.0)
                        print_status()
                sd.sleep(50)
    except KeyboardInterrupt:
        print("\nInterrupted")
    except Exception as e:
        print(f"\nStream error: {e}")
        print("\nヒント:")
        print("  - 出力デバイス (ヘッドホン/スピーカー) が接続されているか確認")
        print("  - Spotify の出力先が CABLE Input になっているか確認")
        print("  - サンプルレートが合わない場合: Windowsサウンド設定で両デバイスを 48000 Hz に揃える")


if __name__ == "__main__":
    main()
