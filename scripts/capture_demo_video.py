"""
capture_demo_video.py
=====================
Watch のデモモードを 60 秒録画し、MP4 として出力する.
PyQt の QWidget.grab() で毎フレーム pixmap を取得 → cv2.VideoWriter に書き込む.

実行:
    python scripts/capture_demo_video.py [--out demo/demo_watch.mp4]
                                          [--fps 15] [--duration 60]
                                          [--width 1280]

完了後、必要なら以下で GIF 化:
    python scripts/mp4_to_gif.py demo/demo_watch.mp4 \\
        --out demo/demo_watch.gif --width 540 --fps 10
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
import cv2
from PyQt5 import QtCore, QtGui, QtWidgets

import realtime_monitor as rm


def inject_demo_state():
    """OSC が来ないので、それっぽい値を state に注入 (静止画スクショと同じ)."""
    rng = np.random.default_rng(7)
    t = np.linspace(0, 5, rm.BUF_LEN)
    base = (
        20 * np.sin(2 * np.pi * 10 * t)
        + 12 * np.sin(2 * np.pi * 2.5 * t)
        + 6 * np.sin(2 * np.pi * 20 * t)
    )
    for i in range(4):
        wave = base + rng.normal(0, 4, size=rm.BUF_LEN)
        rm.state.eeg_buf[i].clear()
        rm.state.eeg_buf[i].extend(wave.tolist())
    rm.state.bands["delta"] = [0.6, 0.5, 0.55, 0.62]
    rm.state.bands["theta"] = [0.9, 0.85, 0.95, 0.92]
    rm.state.bands["alpha"] = [1.6, 1.7, 1.65, 1.55]
    rm.state.bands["beta"]  = [1.2, 1.15, 1.25, 1.18]
    rm.state.bands["gamma"] = [0.7, 0.65, 0.72, 0.68]
    rm.state.quality = [1.0, 1.0, 1.0, 2.0]
    rm.state.touching = 1
    rm.state.msg_count = 6
    rm.state.last_eeg_time = time.time()
    rm.state.heart_rate = 72.0
    rm.state.last_ppg_time = time.time()
    rm.state.last_optics_time = time.time()
    for buf in rm.state.optics_buf:
        buf.clear()
        buf.extend(rng.normal(0, 0.001, size=rm.PPG_BUF_LEN).tolist())


def qpixmap_to_cv(pix, target_w=None):
    """QPixmap → numpy (BGR, cv2 用)."""
    qimg = pix.toImage().convertToFormat(QtGui.QImage.Format_RGB888)
    w, h = qimg.width(), qimg.height()
    # bytesPerLine が w*3 と一致するとは限らない
    bytes_per_line = qimg.bytesPerLine()
    ptr = qimg.bits()
    ptr.setsize(qimg.byteCount())
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape(
        h, bytes_per_line // 3, 3)
    arr = arr[:, :w, :].copy()
    # RGB → BGR
    arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    if target_w is not None and arr.shape[1] != target_w:
        scale = target_w / arr.shape[1]
        new_h = int(arr.shape[0] * scale)
        arr = cv2.resize(arr, (target_w, new_h),
                         interpolation=cv2.INTER_AREA)
    return arr


class Recorder:
    def __init__(self, app, win, out_path, fps, duration_sec, width):
        self.app = app
        self.win = win
        self.out_path = out_path
        self.fps = fps
        self.duration = duration_sec
        self.target_w = width
        self.writer = None
        self.frames_written = 0
        self.t_start = 0.0
        self.frame_interval = 1.0 / fps
        self.next_capture_t = 0.0

    def _init_writer(self, first_frame):
        h, w = first_frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        os.makedirs(os.path.dirname(self.out_path) or ".", exist_ok=True)
        self.writer = cv2.VideoWriter(
            self.out_path, fourcc, self.fps, (w, h))
        if not self.writer.isOpened():
            print(f"❌ VideoWriter open failed: {self.out_path}")
            self.writer = None

    def start(self):
        # 1) Watch モードに切替
        self.win._set_mode("watch")
        QtWidgets.QApplication.processEvents()
        time.sleep(0.3)
        # 2) Surface を選ぶ (デモ開始時に Surface phase なのでこれで良い)
        sea = getattr(self.win, "sea_widget", None)
        if sea is not None:
            sea.set_sub_view("surface")
        QtWidgets.QApplication.processEvents()
        time.sleep(0.3)
        # 3) デモモード ON
        self.win._watch_toggle_demo()
        QtWidgets.QApplication.processEvents()
        time.sleep(0.3)

        self.t_start = time.monotonic()
        self.next_capture_t = self.t_start
        # 録画 tick (50ms ごと判定)
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(1000 / max(self.fps * 2, 10)))

    def _tick(self):
        now = time.monotonic()
        elapsed = now - self.t_start
        if elapsed >= self.duration:
            self._finish()
            return
        if now < self.next_capture_t:
            return
        # フレームキャプチャ
        pix = self.win.grab()
        frame = qpixmap_to_cv(pix, target_w=self.target_w)
        if self.writer is None:
            self._init_writer(frame)
            if self.writer is None:
                self._finish()
                return
        self.writer.write(frame)
        self.frames_written += 1
        self.next_capture_t += self.frame_interval
        if self.frames_written % self.fps == 0:
            sec = self.frames_written // self.fps
            print(f"  recorded {sec:3d}s "
                  f"({self.frames_written} frames)")

    def _finish(self):
        self.timer.stop()
        if self.writer is not None:
            self.writer.release()
        size_mb = (os.path.getsize(self.out_path) / 1024 / 1024
                   if os.path.exists(self.out_path) else 0)
        print(f"✅ saved {self.out_path}  "
              f"({self.frames_written} frames, {size_mb:.2f} MB)")
        # デモを切る
        try:
            if self.win._watch_demo_active:
                self.win._watch_toggle_demo()
        except Exception:
            pass
        QtCore.QTimer.singleShot(200, self.app.quit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "demo",
                                                   "demo_watch.mp4"))
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--width", type=int, default=1280,
                    help="出力動画幅 px (高さはアスペクト比維持)")
    args = ap.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    inject_demo_state()
    win = rm.MainWindow()
    win.resize(1600, 1000)
    win.show()
    QtWidgets.QApplication.processEvents()
    time.sleep(0.5)   # スプラッシュ完了待ち

    print(f"recording: {args.duration}s @ {args.fps}fps, "
          f"width={args.width} -> {args.out}")
    rec = Recorder(app, win, args.out, args.fps,
                   args.duration, args.width)
    rec.start()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
