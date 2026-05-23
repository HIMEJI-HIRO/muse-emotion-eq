"""
capture_screenshots.py
======================
アプリを起動して各モード/サブビューのスクショを自動撮影し、
docs/images/ に保存する.

最新構成 (2026-05):
    - 3 モード: Studio / Listen / Watch
    - Watch サブビュー: Surface (EEG) / Underwater (HR) の 2 種類
    - Underwater: LOW (サンゴ礁 + 魚) / HIGH (ジンベエザメ) の 2 ゾーン
    - Demo モード: 右側に説明パネル付き

実行:
    python scripts/capture_screenshots.py
"""
import os
import sys
import time

# realtime_monitor のあるディレクトリを path に
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

import realtime_monitor as rm


OUT_DIR = os.path.join(ROOT, "docs", "images")
os.makedirs(OUT_DIR, exist_ok=True)


def inject_demo_state():
    """OSC が来ないので、それっぽい値を state に注入."""
    rng = np.random.default_rng(7)
    t = np.linspace(0, 5, rm.BUF_LEN)
    base = (
        20 * np.sin(2 * np.pi * 10 * t)        # α 波
        + 12 * np.sin(2 * np.pi * 2.5 * t)     # δ-θ 帯
        + 6 * np.sin(2 * np.pi * 20 * t)       # β 波
    )
    for i in range(4):
        wave = base + rng.normal(0, 4, size=rm.BUF_LEN)
        wave += rng.normal(0, 2)
        rm.state.eeg_buf[i].clear()
        rm.state.eeg_buf[i].extend(wave.tolist())

    rm.state.bands["delta"] = [0.6, 0.5, 0.55, 0.62]
    rm.state.bands["theta"] = [0.9, 0.85, 0.95, 0.92]
    rm.state.bands["alpha"] = [1.6, 1.7, 1.65, 1.55]
    rm.state.bands["beta"]  = [1.2, 1.15, 1.25, 1.18]
    rm.state.bands["gamma"] = [0.7, 0.65, 0.72, 0.68]

    rm.state.quality = [1.0, 1.0, 1.0, 2.0]
    rm.state.touching = 1
    rm.state.blink = 0
    rm.state.jaw = 0
    rm.state.msg_count = 6
    rm.state.last_eeg_time = time.time()
    rm.state.heart_rate = 72.0
    rm.state.last_ppg_time = time.time()
    rm.state.last_optics_time = time.time()
    for buf in rm.state.optics_buf:
        buf.clear()
        buf.extend(rng.normal(0, 0.001, size=rm.PPG_BUF_LEN).tolist())


def grab_window(win, out_name):
    QtWidgets.QApplication.processEvents()
    time.sleep(0.05)
    QtWidgets.QApplication.processEvents()
    pix = win.grab()
    out = os.path.join(OUT_DIR, out_name)
    pix.save(out, "PNG")
    print(f"[saved] {out}  ({pix.width()}x{pix.height()})")


def schedule_captures(app, win):
    sea = getattr(win, "sea_widget", None)

    def _force_uw_scene(scene_name, target_frame=160, hr=100):
        """Underwater サブビューを強制的に指定 scene にして表示."""
        import cv2
        if sea is None:
            return
        sea.set_sub_view("underwater")
        sea._hr_ema = hr
        sea._uw_current = scene_name
        sea._uw_target = scene_name
        sea._uw_fading = False
        src = sea._uw_sources.get(scene_name)
        if src is not None and src._cap is not None:
            try:
                src._cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                ok, frame = src._cap.read()
                if ok:
                    src._set_image(frame)
            except Exception as e:
                print(f"[uw seek {scene_name}]", e)

    def show_underwater_low():   # サンゴ礁 + 魚
        _force_uw_scene("low", 60, hr=65)

    def show_underwater_high():  # ジンベエザメ
        _force_uw_scene("high", 90, hr=98)

    def turn_on_demo():
        """Demo モードを ON にして説明パネルを表示."""
        if hasattr(win, "_watch_toggle_demo"):
            win._watch_toggle_demo()
            # デモ tick を 1 回回して状態セット
            QtWidgets.QApplication.processEvents()

    def turn_off_demo():
        if (hasattr(win, "_watch_demo_active") and
                win._watch_demo_active and
                hasattr(win, "_watch_toggle_demo")):
            win._watch_toggle_demo()

    # (action, delay_after_ms)
    plan = [
        (lambda: None, 1500),                              # 起動安定待ち
        # ===== Studio =====
        (lambda: win._set_mode("studio"), 1200),
        (lambda: grab_window(win, "ui_studio.png"), 200),
        # ===== Listen =====
        (lambda: win._set_mode("listen"), 1400),
        (lambda: grab_window(win, "ui_listen.png"), 200),
        # ===== Watch / Surface =====
        (lambda: win._set_mode("watch"), 800),
        (lambda: sea and sea.set_sub_view("surface"), 1400),
        (lambda: grab_window(win, "ui_watch_surface.png"), 200),
        # ===== Watch / Underwater LOW (Coral reef + fish) =====
        (show_underwater_low, 1500),
        (lambda: grab_window(win, "ui_watch_underwater_low.png"), 200),
        # ===== Watch / Underwater HIGH (Whale shark) =====
        (show_underwater_high, 1500),
        (lambda: grab_window(win, "ui_watch_underwater_high.png"), 200),
        # ===== Watch / Demo mode (with right-side explainer panel) =====
        (turn_on_demo, 1500),
        (lambda: grab_window(win, "ui_watch_demo.png"), 200),
        (turn_off_demo, 400),
        (lambda: app.quit(), 0),
    ]

    state = {"i": 0}

    def next_step():
        if state["i"] >= len(plan):
            return
        action, delay = plan[state["i"]]
        state["i"] += 1
        try:
            action()
        except Exception as e:
            print(f"[step {state['i']}] error: {e}")
        QtCore.QTimer.singleShot(delay, next_step)

    QtCore.QTimer.singleShot(50, next_step)


def main():
    app = QtWidgets.QApplication(sys.argv)
    inject_demo_state()
    win = rm.MainWindow()
    win.resize(2200, 1400)
    win.show()
    schedule_captures(app, win)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
