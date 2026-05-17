# Muse S Athena の精度 — 正直なレビュー

ポートフォリオ提出にあたり、**できること/できないこと**を率直にまとめる。
データセットの過大評価はしないという姿勢を示すための文書。

## ハードウェア構成

- 乾式電極 4ch: TP9 / TP10 / AF7 / AF8 (側頭2 + 前頭2)
- PPG (脈波) 1基 + 加速度 + ジャイロ
- 256 Hz EEG, Bluetooth 経由

## 信号別の信頼度評価

| 信号 | 信頼度 | コメント |
|---|---|---|
| **HR (BPM, PPG)** | ★★★★★ | ±2 BPM 程度で安定。呼吸性変動も観測可 |
| **HRV (RMSSD)** | ★★★★ | 1 分窓で安定、リラックス指標に十分 |
| **α / β 帯域パワー (絶対値)** | ★★ | 電極接触で倍半分ブレる |
| **β/α 比 (Engagement)** | ★★★ | 比なので接触ムラに強い |
| **Arousal** | ★★☆ | 噛み締め・まばたきで簡単に跳ねる |
| **Valence (前頭 α 左右差)** | ★☆ | **論文再現性が低い**。前頭 2 点だけでは厳しい |
| **加速度・ジャイロ (頷き)** | ★★★★★ | ジェスチャー入力に流用可 |
| **Mind Monitor 独自 集中スコア** | ★★★ | 平滑済みでデモ映えする |

## 本プロジェクトでの判断

Valence は弱いとわかっていたので、設計を以下のように寄せた:

1. **シーン選択ロジックに hysteresis + 6 秒最小滞留**
   ノイズが閾値を跨いでもシーンがバタつかないように
2. **判定用に超スロー EMA (τ≈8s) を別系統で持つ**
   描画用 EMA とは分離
3. **HR を主役にする要素 (海面の脈動リング)**
   信頼度の高い信号を体験の中心に
4. **オーバーレイの霧で正直なフィードバック**
   電極接触が悪い (HSI 高) と画面が霞む = "信号が悪い" ことを隠さない

## 比較対象

| デバイス | 電極数 | Valence 再現性 | コスト | ポータビリティ |
|---|---|---|---|---|
| Muse S Athena | 4 (dry) | ★ | ~$400 | ◎ |
| OpenBCI Cyton | 8-16 (wet) | ★★ | ~$1200 | △ |
| Emotiv EPOC X | 14 (saline) | ★★ | ~$850 | ○ |
| 医療研 EEG | 32+ (gel) | ★★★ | $10000+ | ✗ |

Muse S は**精度より体験/装着性**の選択肢。本プロジェクトの「気軽に脳波で音楽を変える」コンセプトには適合している。

## 将来の改善案

- **個人専用キャリブレーション** — ユーザー毎に静的な閾値ではなく、安静時・覚醒時の自己ベースラインを学習
- **アーティファクト除去** — 噛み締め (jaw_clench OSC) や瞬き (blink OSC) を検出して該当窓の値を捨てる
- **Mind Monitor 内蔵スコアの活用** — Concentration/Meditation スコアは Muse 側で正規化されており、生 EEG よりロバスト

## 参考文献

- Russell, J. A. (1980). A circumplex model of affect. *Journal of Personality and Social Psychology*.
- Pope, A. T., Bogart, E. H., & Bartolome, D. S. (1995). Biocybernetic system evaluates indices of operator engagement. *Biological Psychology*.
- Davidson, R. J. (1992). Anterior cerebral asymmetry and the nature of emotion. *Brain and Cognition*.
