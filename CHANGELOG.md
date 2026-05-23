# Changelog

All notable changes to this project, newest first.
Format inspired by [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]
- Personal EEG calibration (per-user ML model) — Phase 8
- 1-minute polished demo video for portfolio / LabBase / TechOffer — Phase 9

## [0.7] — 2026-05  ·  Driver overlay + Demo mode
- **Watch driver-source overlay** — top-right pill showing
  `🧠 EEG-DRIVEN  ·  Arousal 0.72 · Valence 0.32` or
  `♥ HR-DRIVEN  ·  92 BPM · zone: HIGH 🐋` so viewers can tell at a
  glance what's driving the current scene.
- **Demo mode** (`▶ Demo` button) — 60-second loop simulating EEG/HR
  changes so reviewers without a Muse headset can see all scenes morph.
- Right-side **explainer panel** with live meters + per-phase Japanese
  narration. Built with Qt layouts (was custom paint that clipped).
- **Underwater 2-zone** scheme (was 3 zones LOW/MID/HIGH):
    - `< 82 BPM` → 🐠 Coral reef + fish
    - `≥ 82 BPM` → 🐋 Whale shark (replaced 4s..8s of a Veo source so the
      whole 4-second clip is whale-shark-on-screen).
- `_VideoSource` gained a `(loop_start, loop_end)` window so Veo
  intros/outros with empty water can be skipped.
- City and Forest sub-views **removed** (assets kept on disk for LFS
  history). Watch is now Surface + Underwater only.

## [0.6] — 2026-05  ·  Listen polish + click-to-adjust
- **Listen Heart-Rate panel** — big `♥ XXX BPM` label + PPG ch1 mini
  waveform plot.
- **Click-to-adjust EQ** — clicking the upper half of an instrument
  circle adds +0.5 dB, lower half subtracts 0.5 dB. Toast confirms.
- **Instrument-circle fader headers** — Mixer faders now show the same
  neon-ring + texture icon used in Listen, replacing the small emoji.
- Studio "🌊 Sea" sub-tab **removed** (Watch handles Sea now).
- Fixed multiple text-clipping bugs: BandSphere greek letters,
  HR/fNIRS channel labels, Russell pad corner labels and axis labels.

## [0.5] — 2026-05  ·  Performance + dead-code sweep
- Visibility guards on all per-frame animation widgets — `_BandSphere`,
  `_InstrumentCircle`, `_RussellPad`, `_BrainWave`, `_RibbonBar`,
  `_MatrixRain`, `_TronGrid`, `_MandalaOverlay`, `_ParticleEEG` —
  skip work when their widget is hidden.
- `update_ui` work gated by visible card: EEG `sosfiltfilt`, HR
  curves, spectrogram `welch`, band-power gauges, audio FFT.
- `setStyleSheet` calls cached: signal-quality dots (was 120/sec),
  touching/blink/jaw artefacts, streaming-status header label.
- Removed unused classes `_BandGauge`, `_ScanlineOverlay` + dead
  scanline-toggle code path. Dropped unused `pywt` import.
- Net: `realtime_monitor.py` shrank ~78 lines and CPU dropped
  noticeably during Watch / Listen modes.

## [0.4] — 2026-05  ·  Public release prep
- Repository made **Public**.
- 20 GitHub topics added for discoverability (eeg, affective-computing,
  brain-computer-interface, pyqt5, pedalboard, muse-headband, etc.).
- `archive/offline_ml/*.ipynb` outputs stripped (was leaking
  `C:\Users\hiro2\AppData\…` tmp paths in Jupyter cell output).
- README redesigned hero / badges / 3-mode table / Watch sub-views /
  features grid / quick start / architecture (mermaid) / signal
  processing / honest accuracy review / roadmap.

## [0.3] — 2026-05  ·  Tier-1 UI polish
- F12 screenshot, F11 fullscreen, F1 help.
- Cursor particle trail behind the mouse.
- CSV replay: A / E / L / K keys load a recorded session and inject
  values at original timestamps.
- Theme system: 15 accent colors × 6 BG palettes + custom QColorDialog
  picker. Hover-preview on combobox.
- Drag-and-drop card rearrange in Studio mode.
- Idle screensaver after 90s of no activity.
- Watch 📷 Photo button (save current sea view as PNG).

## [0.2] — 2026-05  ·  Watch + assets
- Watch mode with 4 sub-views (Surface / Underwater / City / Forest)
  — later collapsed to 2 in 0.7.
- AI-generated assets: Veo clips for sea scenes, Imagen for header
  circuit pattern + cyber-city background + 6 instrument textures.
- SeaWidget cross-fades between scenes with HR hysteresis.

## [0.1] — 2026-05  ·  Initial MVP
- Muse S Athena → Mind Monitor → OSC :5000 receiver.
- Real-time band-power (δ, θ, α, β, γ) extraction (Welch + log-norm).
- Russell circumplex + Engagement (β/α) emotion estimator.
- 6-band EQ via `pedalboard` (Drums / Bass / Mid / Vocals / High / Air)
  + Reverb wet, routed VB-CABLE → audio output.
- `ReflectController` maps Arousal/Valence/Engagement → target EQ.
- PyQt5 GUI: Raw EEG plots, spectrogram, band gauges, signal quality,
  Russell pad with trail, instrument faders, Auto/Manual switch.
