# Screenshots & Demo Recording Guide

ポートフォリオ提出前に撮影すべき素材一覧と推奨設定のメモ。

## 📸 静止画スクショ

| ファイル名 | 内容 | 撮影 mode | Theme | BG |
|---|---|---|---|---|
| `docs/images/ui_studio.png` | Studio フルビュー (全カード見える状態) | Studio | Cyber Cyan | Jet Black |
| `docs/images/ui_listen.png` | Listen の Happy 状態 | Listen | Cyber Cyan | Deep Ocean |
| `docs/images/ui_watch_surface.png` | Watch + Surface サブビュー | Watch | Cyber Cyan | (背景: 海面動画) |
| `docs/images/ui_watch_underwater.png` | Watch + Underwater | Watch | Cyber Cyan | (水中映像) |
| `docs/images/ui_watch_city.png` | Watch + City + 高 HR | Watch | Neon Magenta | (都市夜景) |
| `docs/images/splash.png` | スプラッシュスクリーン | (起動時) | Cyber Cyan | – |
| `docs/images/theme_grid.png` | テーマバリエーション (4枚並べ) | 各モード | 4色 × 4 BG | – |

撮影方法 (Windows):
- `Win + Shift + S` (画面の一部選択キャプチャ)
- Snip & Sketch で全画面取得 → トリミング

## 🎬 1分デモ動画

ストーリーボード:
```
0:00–0:05  スプラッシュスクリーン (タイトル + プログレス)
0:05–0:15  Studio モード — EEG リアルタイム + Band Power リング
0:15–0:25  Listen モード — 大きな感情ラベル + 楽器サークル + プリセット切替
0:25–0:35  Watch / Surface — 海面 morph 動画 + HUD + δθαβγ 弧
0:35–0:45  Watch / Underwater — HR で 3 シーン切替 (HRを変動させると魚が増える)
0:45–0:55  Watch / City — 高 HR で都市が脈動 + 鼓動 vignette
0:55–1:00  Theme/BG 切替で 90 パターン見せる
```

### Windows 録画
- `Win + G` → ゲームバー → 録画ボタン
- 出力: `videos/Captures/*.mp4`
- 30 fps / 1080p / システム音 + マイク (BGM 用)

### 編集 (任意)
- 字幕: Microsoft Clipchamp / DaVinci Resolve / Premiere
- BGM: テンポ変化のある曲 (Royalty Free から)
  - 例: Lo-fi → エレクトロ → アンビエント

### GIF 化 (README 用)
- 1分動画 → 10秒抜粋 → GIF (ezgif.com)
- 出力: `demo/demo_thumb.gif` (5 MB 以下推奨)
- 解像度: 720px幅, 8-12 fps で十分

## 🖼 Social Preview (1280×640)

GitHub の OGP 画像。Twitter/Slack シェア時に表示される。

推奨構成:
- 背景: Watch モードのスクショ (オーブ中央 + 海)
- オーバーレイ: タイトル "EEG ADAPTIVE EQ" + サブタイトル
- 右下に小さく "PyQt5 + Muse + pedalboard"

Figma / Canva で 1280×640 のテンプレ作成 → エクスポート → GitHub Settings → Social preview にアップロード。

## チェックリスト

提出前に確認:
- [ ] 全 7 枚のスクショが `docs/images/` に揃っている
- [ ] README の画像リンクが**実画像**に置き換わっている
- [ ] `demo/demo_thumb.gif` が README に表示される (5 MB 以下)
- [ ] `demo/demo_1min.mp4` が Git LFS で push されている
- [ ] Social preview 画像が GitHub にアップ済み
- [ ] About 欄に Description + Website (YouTube URL or ポートフォリオ) が設定済み
- [ ] Topics が全部設定済み (eeg, bci, muse, ...)
- [ ] Public 化のタイミング決定済み
