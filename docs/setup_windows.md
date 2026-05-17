# Windows セットアップ詳細

## 1. Muse S Athena ハードウェア

- Muse S Athena 本体 (Gen 2 でも動作可だが PPG 精度に差)
- Bluetooth 接続用の iPhone or Android (Mind Monitor アプリを動かす)

## 2. Mind Monitor アプリ

iOS / Android で **Mind Monitor** を購入 (有料、~1500円)。

設定:
1. Muse S を Bluetooth でスマホに接続
2. Mind Monitor を起動 → 接続確認
3. 設定 → **OSC Stream** を ON
4. **Target IP** = PC の IP アドレス (`ipconfig` で確認)
5. **Target Port** = `5000`
6. Streaming Mode = "All" 推奨

## 3. VB-CABLE Virtual Audio Device

公式: https://vb-audio.com/Cable/

1. インストーラ実行 (要再起動)
2. Windows サウンド設定で:
   - **Spotify の出力**: `CABLE Input (VB-Audio Virtual Cable)`
   - **本アプリの入力**: 自動検出される
   - **本アプリの出力**: `WF-1000XM5` などお好みのヘッドホン

## 4. Python 環境

```powershell
# anaconda3 推奨
conda create -n eeg python=3.11
conda activate eeg
pip install -r requirements.txt
```

## 5. 起動確認

```powershell
# OSC 疎通テスト
python scripts/test_osc.py

# 音声ルーティングテスト
python scripts/test_audio_eq.py

# 本体起動
python realtime_monitor.py
```

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `● No Signal` のまま | Mind Monitor の Stream OFF / IP 違い | Mind Monitor 設定見直し |
| 音が出ない | Spotify が CABLE Input になってない | サウンド設定確認 |
| 音が CABLE に戻る | アプリ出力が CABLE になってる | ヘッダ Out 選択を修正 |
| Sea 動画が黒画面 | OpenCV 未インストール | `pip install opencv-python` |
| 起動時に PyQt5 エラー | バージョン不整合 | `pip install PyQt5==5.15.10` |
