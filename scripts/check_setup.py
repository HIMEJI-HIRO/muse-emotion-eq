"""
check_setup.py
==============
依存ライブラリ + 音声デバイス + OSC ポートの疎通を一括チェック.
"""
import socket
import sys


REQUIRED = [
    ("numpy", "1.24"),
    ("scipy", "1.11"),
    ("PyQt5", "5.15"),
    ("pyqtgraph", "0.13"),
    ("sounddevice", "0.4"),
    ("pedalboard", "0.9"),
    ("pythonosc", "1.8"),
    ("cv2", "4.8"),
]


def check_imports():
    print("== Python packages ==")
    ok = True
    for name, _min in REQUIRED:
        try:
            mod = __import__(name)
            v = getattr(mod, "__version__", "?")
            print(f"  [OK] {name:<14} {v}")
        except ImportError as e:
            print(f"  [NG] {name:<14} not installed ({e})")
            ok = False
    return ok


def check_audio():
    print("\n== Audio devices ==")
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        cable_in = any("CABLE Output" in d["name"] for d in devs)
        wf_out = any("WF-1000" in d["name"] or "Headphone" in d["name"]
                     for d in devs)
        print(f"  CABLE Output (入力候補): {'OK' if cable_in else 'NG'}")
        print(f"  Headphone系 (出力候補): {'OK' if wf_out else 'NG'}")
        return cable_in and wf_out
    except Exception as e:
        print(f"  [NG] {e}")
        return False


def check_osc_port(port=5000):
    print(f"\n== OSC port :{port} ==")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", port))
        s.close()
        print(f"  [OK] port :{port} bindable")
        return True
    except OSError as e:
        print(f"  [NG] port :{port} in use ({e})")
        return False


if __name__ == "__main__":
    ok1 = check_imports()
    ok2 = check_audio()
    ok3 = check_osc_port()
    print()
    if ok1 and ok2 and ok3:
        print("✅ 全チェック OK")
        sys.exit(0)
    else:
        print("⚠ 一部に問題あり (上記参照)")
        sys.exit(1)
