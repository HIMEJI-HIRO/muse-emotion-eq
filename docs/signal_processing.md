# 信号処理の解説

## 入力信号

| 信号 | サンプリング | チャネル | 取得経路 |
|---|---|---|---|
| Raw EEG | 256 Hz | 4ch (TP9, AF7, AF8, TP10) | Mind Monitor `/muse/eeg` |
| Band Power (絶対値) | ~10 Hz | δ/θ/α/β/γ × 4ch | Mind Monitor `/muse/elements/{band}_absolute` |
| Horseshoe | ~1 Hz | 4ch | `/muse/elements/horseshoe` (1=Good, 2=OK, 4=Bad) |
| PPG | 64 Hz | 3ch | `/muse/ppg` |
| HR (BPM) | 任意 | scalar | `/muse/elements/experimental/heart_rate` |

## 感情推定

### Arousal (覚醒度)
β + γ パワーの全電極平均 (品質 OK ch のみ使用)。
正規化: 0..1 (sigmoid 風スケール)。

### Valence (快/不快)
Russell Circumplex (1980) に基づく前頭 α 左右差:
```
valence = (α[AF8] - α[AF7]) / (α[AF8] + α[AF7])
```
**注意**: Muse の 2 点だけでは再現性が低い。アプリでは long EMA で平滑し、シーン選択判定は hysteresis を必須にしている。

### Engagement (集中度)
Pope et al. (1995) の β/α 比:
```
engagement = β_mean / (α_mean + ε)
```
比なので接触ムラに比較的強い (★★★)。

## 平滑化

### EMA (Exponential Moving Average)
```
y[n] = y[n-1] + α * (x[n] - y[n-1])
α = dt / τ
```

各信号の τ:

| 信号 | τ | フレームαで (33ms) |
|---|---|---|
| EQ 制御用 | 3 s | 0.011 |
| Sea 描画 (空色) | 8 s | 0.004 |
| Sea 描画 (太陽高度) | 1.6 s | 0.02 |
| Sea シーン判定 | 8 s | 0.004 |
| HR | 0.6 s | 0.05 |

## 音への反映 (audio_engine + ReflectController)

### 6-band → 感情マッピング

| バンド | 中心周波数 | 駆動式 |
|---|---|---|
| Drums | 80 Hz LowShelf | `0.6·A - 0.3·V - 0.2·E` |
| Bass | 180 Hz Peak | `-0.5·A - 0.1·E` |
| Mid | 1000 Hz Peak | `0.55·E + 0.2·V` |
| Vocals | 2800 Hz Peak | `0.55·E + 0.35·A` |
| High | 6000 Hz Peak | `0.7·A + 0.15·V` |
| Air | 10 kHz HighShelf | `0.55·V + 0.15·E` |

(A, V, E は bipolar -1..+1 化、gain は ±4 dB にクリップ)

### Reverb wet
`0.5·(V + 1)` で 0..1 → 最大 wet 0.45 にスケーリング。

## CPU 負荷対策

- pedalboard 更新は **10 Hz 上限 + 0.05 dB 変化しきい値**
- 音声 stream は `BLOCK_SIZE=1024`, `latency="high"` (Bluetooth 安定優先)
- 描画は 30 fps、Sea ビュー非表示時は描画スキップ
