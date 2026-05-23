# Contributing

Thanks for taking a look 👋

This is a solo portfolio project (Vital Sensing × Affective Computing × Audio × AI),
but PRs / issues / discussions are welcome — especially around:

- 🎛 **Better emotion-to-EQ mappings** (the current Russell + Engagement curve is
  educated-guess-based; ML calibration is on the roadmap).
- 🧪 **Reproducibility of Valence (FAA)** — the literature is split and so is my data.
  If you have a session protocol that gives you stable FAA, I want to hear it.
- 🎨 **New Watch scenes / overlays** — the FSM is just `_VideoSource` + a loop window;
  drop a new mp4 in `assets/sea/` and wire it in `sea_widget.py`.
- 🐋 **Replacement Veo prompts** that produce a more seamless 8-second loop than
  what's currently shipped.

---

## Quick dev setup

```bash
# 1. Clone + venv
git clone https://github.com/HIMEJI-HIRO/muse-emotion-eq.git
cd muse-emotion-eq
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt

# 2. Run smoke test (no headset required)
python realtime_monitor.py

# 3. Auto-screenshot regression (after UI changes)
python scripts/capture_screenshots.py
```

## House style

| Topic | Rule |
|---|---|
| Python | Black-ish (4 spaces, ~88 col), no type stubs required |
| Commits | Imperative subject in English, multi-line body in EN or JP |
| Branches | `main` is the only long-lived branch; PR off feature branches |
| Big binaries | All `*.mp4 *.png *.jpg *.gif` are Git LFS — already configured in `.gitattributes` |
| Tests | None yet (solo + UI-heavy). Visual regression via `scripts/capture_screenshots.py` |

## Code layout (where to look)

| File | Role |
|---|---|
| `realtime_monitor.py` | Main entry point + OSC server + Qt main window + all widgets |
| `audio_engine.py` | `AudioEngine` — sounddevice + pedalboard 6-band EQ chain |
| `eq_controllers.py` | `ReflectController` — maps Arousal/Valence/Engagement → EQ values |
| `eq_widgets.py` | Custom Qt fader widgets used in Studio + Listen |
| `sea_widget.py` | `SeaWidget` — Watch mode's video scenes + overlays |
| `theme.py` | `ThemeManager` — accent × BG palette system |
| `scripts/` | Headless capture + setup checks (no app dependency) |

## Issues

Open a GitHub Issue — no template, just include:
1. What you ran
2. What you expected
3. What you saw (screenshot / paste) + your OS / Python version

---

By contributing, you agree to license your changes under the
[MIT License](LICENSE).
