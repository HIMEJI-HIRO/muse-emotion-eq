"""
Mind Monitor OSC receiver test.
スマホのMind Monitorから送られてくるMuse EEGデータを受信して表示。
"""
from pythonosc import dispatcher, osc_server
import time

# --- 受信設定 ---
LISTEN_IP = "0.0.0.0"     # 全インターフェースで待ち受け
LISTEN_PORT = 5000        # Mind Monitor デフォルト

# --- 受信カウンタ（統計用）---
counts = {}
last_report = time.time()


def log_handler(address, *args):
    """すべてのOSCメッセージをカウント + 最初の3件だけ表示"""
    global last_report
    counts[address] = counts.get(address, 0) + 1

    # 最初の数回だけ中身をプリント
    if counts[address] <= 3:
        print(f"{address}  <- {args}")

    # 3秒ごとにカウント集計を表示
    if time.time() - last_report > 3.0:
        print("\n--- Received counts (last 3s) ---")
        for addr, n in sorted(counts.items()):
            print(f"  {addr}: {n}")
        print("---------------------------------\n")
        counts.clear()
        last_report = time.time()


disp = dispatcher.Dispatcher()
disp.map("/muse/*", log_handler)          # Mind Monitorの全 /muse/* を拾う
disp.map("/*", log_handler)               # それ以外も念のため

server = osc_server.ThreadingOSCUDPServer((LISTEN_IP, LISTEN_PORT), disp)
print(f"Listening on {LISTEN_IP}:{LISTEN_PORT}")
print("Mind Monitorで OSC Stream を ON にしてください。Ctrl+C で終了。\n")

try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nStopped.")
