"""
Muse S Athena BrainFlow connection test.
5秒間ストリームしてデータ形状とEEGサンプルを表示。
"""
import time
import numpy as np
from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds

BoardShim.enable_dev_board_logger()

# Muse S Athena は MUSE_S_BOARD (id=39) で接続できる
# ダメな場合は MUSE_S_BLED_BOARD (id=21, BLED112ドングル必要) に変更
board_id = BoardIds.MUSE_S_BOARD.value

params = BrainFlowInputParams()
# mac_address を指定するとスキャンをスキップできる
# params.mac_address = "00:55:DA:BB:C6:60"

board = BoardShim(board_id, params)

print("[1/5] Preparing session (BLE接続中...)")
board.prepare_session()

# Muse S Athena: プリセットを送ってEEG subscribeを有効化
# p20=EEG, p21=EEG+aux, p50=EEG+accel, p61=EEG+accel+gyro+PPG
print("[2/5] Configuring board (preset p50)")
try:
    board.config_board("p50")
except Exception as e:
    print(f"  config_board failed (継続します): {e}")

print("[3/5] Starting stream")
board.start_stream()

print("[4/5] Streaming for 5 seconds...")
time.sleep(5)

data = board.get_board_data()
board.stop_stream()
board.release_session()

print("[5/5] Done")
print(f"\nData shape: {data.shape}  (channels x samples)")

eeg_channels = BoardShim.get_eeg_channels(board_id)
eeg_names = BoardShim.get_eeg_names(board_id)
fs = BoardShim.get_sampling_rate(board_id)

print(f"Sampling rate: {fs} Hz")
print(f"EEG channel indices: {eeg_channels}")
print(f"EEG channel names:   {eeg_names}")
print(f"\nFirst 3 EEG samples (channels x time):")
print(data[eeg_channels, :3])
