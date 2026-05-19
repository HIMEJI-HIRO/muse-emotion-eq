<div align="center">

# 🧠🎵 muse-emotion-eq

### **Your brain controls your music. In real time.**

EEG・心拍から感情を推定し、音楽の EQ と没入型映像をリアルタイム制御するデスクトップアプリ

[![Status](https://img.shields.io/badge/status-MVP-brightgreen?style=flat-square)](.)
[![Python](https://img.shields.io/badge/python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)](.)
[![PyQt5](https://img.shields.io/badge/PyQt5-5.15-41CD52?style=flat-square&logo=qt)](.)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6?style=flat-square&logo=windows&logoColor=white)](.)
[![License](https://img.shields.io/badge/license-MIT-yellow?style=flat-square)](LICENSE)
[![AI-Assisted](https://img.shields.io/badge/AI--assisted-Claude-D97757?style=flat-square)](docs/ai_assisted_dev.md)

<img src="demo/demo_thumb.gif" width="720" alt="demo">

[**🚀 Quick Start**](#-quick-start) · [**🎬 Demo**](#-demo) · [**🏗 Architecture**](#-architecture) · [**📊 Accuracy Notes**](docs/muse_accuracy_notes.md) · [**🤖 AI Workflow**](docs/ai_assisted_dev.md)

</div>

---

## ✨ Why this project

> **「感情で音楽が変わる体験」を、自分の脳波と心拍で作る**

Vital Sensing × Affective Computing × Audio の交差点を、**MVP として 2 週間で動かしました**。Muse S Athena 1 台で取れる EEG / PPG から感情を推定し、それを **音 (EQ)** と **映像 (Sea ビュー)** の両方に同時反映させます。

---

## 🎬 Demo

### 🎞 Intro

[![demo intro](demo/screenshots/promo_thumb.png)](demo/promo_intro.mp4)

> Brain hologram → neural network → music waveform → ocean horizon (8s cinematic intro)

### Three Modes

| 🧠 Studio (分析) | 🎚 Listen (操作) | 🌊 Watch (没入) |
|:---:|:---:|:---:|
| ![studio](docs/images/ui_studio.png) | ![listen](docs/images/ui_listen.png) | ![watch](docs/images/ui_watch_surface.png) |
| パーティクル EEG / Band Power Ring / Russell Pad / Spectrogram / Adaptive EQ | リボン感情バー / 楽器サークル / Big "Happy" / プリセット | 神経網オーブ / Matrix rain / Tron Grid / 海背景 |

### Watch — 4 Subviews driven by biosignals

| 🌅 Surface | 🐳 Underwater | 🌆 City | 🌲 Forest |
|:---:|:---:|:---:|:---:|
| ![surface](docs/images/ui_watch_surface.png) | ![underwater](docs/images/ui_watch_underwater_high.png) | ![city](docs/images/ui_watch_city.png) | ![forest](docs/images/ui_watch_forest.png) |
| Arousal で速度可変 | HR で 3段階 (空海 → 魚群 → ジンベエ + マンタ) | HR で色温度・脈動 vignette | Engagement で速度 / Valence で柔らかな tint |

---

## ⚡ Features

### 🎛 Audio Engine
- **EEG → EQ Auto**: Arousal / Valence / Engagement で 6 バンド (Drums / Bass / Mid / Vocals / High / Air) + Reverb を自動追従
- **Manual / Auto モード切替**: 手動フェーダと EEG 駆動を即切替
- VB-CABLE → pedalboard → 任意出力デバイス
- リアルタイムオーディオレベルメータ (ヘッダ)

### 🎨 三段モード UI
- **🧠 Studio**: パーティクル EEG / Spectrogram / 5バンドリングゲージ / Russell 2D マップ / 信号品質 / HR / Adaptive EQ — すべてフル表示
- **🎚 Listen**: 大きな感情ラベル "Happy" + リボン感情バー + 6 楽器サークル (テクスチャ画像) + プリセット
- **🌊 Watch**: フルスクリーン没入。サブビュー 3 種:
  - **Surface** — 海面 morph 動画 (Arousal で再生速度変化)
  - **Underwater** — HR 駆動 3 段階の海中映像 (空/小魚/ジンベエ+マンタ)
  - **City** — サイバーパンク都市 (HR シンクで明度・色温度・鼓動 vignette)

### 🌟 Visual Polish
- **神経網オーブ** (Fibonacci 球面 120点パーティクル, マゼンタ+シアン渦巻き)
- **Matrix 風 binary rain** 背景
- **Tron 風 wireframe grid 床** (パースペクティブ)
- **HUD パネル**: NEURAL STATE / EQ STATE / [STATUS: CALM ●●●○○]
- **六角形 EQ ラベル + 放射状ライン** (中心オーブから)
- **δθαβγ ギリシャ文字弧** 配置
- **回路パターン背景ヘッダ** + accent neon 縁発光
- **起動時スプラッシュスクリーン** (回路 + neon タイトル + プログレスバー)
- **モード切替スライドアニメ** (260ms + opacity)
- **カードホバー info popup** (accent border + fade/rise アニメ)

### ⚙ Customization
- アクセント 15 色 × 背景 6 パレット = **90 通り**
- 走査線 / fog / グリッター / 泡 etc. の演出パラメータ調整可

### 📡 Hardware Integration
- **Muse S Athena** (EEG 4ch + PPG + 光学) → Mind Monitor (iOS/Android) → PC OSC
- Bluetooth レイテンシ対応 (BLOCK_SIZE 1024, latency='high')

### 🤖 AI Workflow
- **Claude (Anthropic)** と 2 週間で MVP
- 人間が**意思決定** / AI が**実装** の明確な分業

---

## 🚀 Quick Start

```powershell
# 1. Clone
git clone https://github.com/HIMEJI-HIRO/muse-emotion-eq.git
cd muse-emotion-eq

# 2. Install deps
pip install -r requirements.txt

# 3. Launch
python realtime_monitor.py
```

**前提**:
- Windows 10/11 + Python 3.11 (anaconda3 推奨)
- Muse S Athena + Mind Monitor (iOS/Android)
- VB-CABLE Virtual Audio Device

詳細セットアップ手順は [📖 docs/setup_windows.md](docs/setup_windows.md) を参照.

---

## 🏗 Architecture

```mermaid
graph LR
    A[Muse S Athena<br/>EEG + PPG] -->|Bluetooth| B[Mind Monitor]
    B -->|OSC :5000| C[Python App]
    C --> D[Russell + Engagement<br/>感情推定]
    D --> E[ReflectController<br/>EQ マッピング]
    F[Spotify] -->|VB-CABLE| G[pedalboard<br/>6-band EQ + Reverb]
    E --> G
    G --> H[🎧 Headphones]
    D --> I[SeaWidget<br/>動画 + Overlay]
```

詳細: [docs/architecture.md](docs/architecture.md)

---

## 🧪 Signal Processing

| 指標 | 計算 | 用途 |
|---|---|---|
| **Arousal** | β + γ 高域パワー | EQ Drums / High / Vocals |
| **Valence** | 前頭 α 左右差 (AF7/AF8) | EQ Air / Reverb / シーン選択 |
| **Engagement** | β / α 比 | EQ Mid / Vocals |
| **HR (BPM)** | PPG ピーク検出 (0.7–3 Hz BP) | 海面の脈動リング |
| **HSI** | Muse horseshoe (1 Good — 4 Bad) | 映像の霧エフェクト |

詳細: [docs/signal_processing.md](docs/signal_processing.md)

---

## 📊 Honest Accuracy Review

ポートフォリオには **「できること」と同じくらい「できないこと」** を書きました。

| 信号 | 信頼度 |
|---|:---:|
| **HR (BPM, PPG)** | ★★★★★ |
| **β/α 比 (Engagement)** | ★★★ |
| **Arousal** | ★★☆ |
| **Valence (前頭 α 左右差)** | ★☆ |

→ Valence は弱いため UI 側で **slow EMA + ヒステリシス + 最小滞留時間** を入れて誤認による画面バタつきを抑制。
詳細: [docs/muse_accuracy_notes.md](docs/muse_accuracy_notes.md)

---

## 🛠 Tech Stack

| Layer | Library |
|---|---|
| GUI | PyQt5, pyqtgraph |
| 信号処理 | NumPy, SciPy (Butterworth, Welch) |
| 音声 DSP | [pedalboard](https://github.com/spotify/pedalboard) (Spotify R&D), sounddevice |
| 動画背景 | OpenCV |
| OSC | python-osc |
| EEG | Muse S Athena + Mind Monitor |

---

## 📁 Repository Structure

```
muse-emotion-eq/
├── realtime_monitor.py      # メインエントリ (PyQt5 GUI + OSC)
├── audio_engine.py          # VB-CABLE → pedalboard → 出力
├── eq_controllers.py        # 感情 → EQ マッピング
├── eq_widgets.py            # 6-band 楽器フェーダ
├── sea_widget.py            # Emotional Seascape (動画 + overlay)
├── theme.py                 # 2軸テーマ (Accent × BG)
├── assets/sea/              # シーン動画 (Git LFS)
├── docs/                    # 設計ドキュメント
├── demo/                    # デモ動画・スクショ
└── scripts/                 # 環境チェック・診断ツール
```

---

## 🗺 Roadmap

- [x] **Phase 0** — Muse 受信 / 可視化基盤
- [x] **Phase 1** — 6-band EQ + 感情自動制御
- [x] **Phase 1.5** — Emotional Seascape (Calm / Golden / Storm)
- [x] **Phase 2** — UI 大改修 (Studio / Listen / Watch 3 モード)
- [x] **Phase 3** — Underwater シーン (HR 駆動 3 段階)
- [x] **Phase 3.5** — City + Forest サブビュー (動画 + HR シンク)
- [x] **Phase 4** — CSV セッションリプレイ機能 (▶ Replay)
- [x] **Phase 5** — UX 微調整層
  - スプラッシュ / Toast 通知 / カード hover popup
  - キーボードショートカット (`1/2/3/4` 文脈別)
  - Settings ダイアログ / Welcome オーバーレイ / About
  - 音量スライダ / カスタム accent ピッカー / hover preview
  - REC タイマー / Audio glow / HR フラッシュ / Uptime
  - F12 スクショ / F11 全画面 / 📷 Photo ボタン (Watch)
  - マウス追従パーティクル / アイドルスクリーンセーバー
  - カード drag-and-drop 並び替え
- [ ] **Phase 6** — 個人 EEG キャリブレーション (ML)
- [ ] **Phase 7** — 1分デモ動画 + Public 化

---

## 🤖 AI Workflow

このプロジェクトは **人間が意思決定 → Claude (Anthropic) が実装** の分業で開発しました。
工程の正直な記録: [docs/ai_assisted_dev.md](docs/ai_assisted_dev.md)

---

## 📝 Design Decisions

実装中に下した主要な意思決定の理由をまとめました:
- なぜ 6-band フェーダから Sea ビューへ重心を移したか
- なぜ Storm を Underwater に置換予定か
- なぜ QMediaPlayer ではなく OpenCV を選んだか
- なぜ Watch HUD に 8 角プリズムを使ったか

詳細: [docs/design_decisions.md](docs/design_decisions.md)

---

## 📜 License

[MIT](LICENSE) — 自由に fork / 改変 / 商用利用可

---

<div align="center">

### Built by [@HIMEJI-HIRO](https://github.com/HIMEJI-HIRO)

Portfolio project — **Vital Sensing × Affective Computing × Audio**
🧠 + 🎵 + 🤖

</div>
