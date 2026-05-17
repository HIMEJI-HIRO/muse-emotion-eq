"""
theme.py
========
アプリ全体のテーマ管理 (2軸).

  - Accent テーマ: ボタン/曲線/パック色 (明るい差し色)
  - Background パレット: 背景・パネル・カード・ボーダー・文字色

ThemeManager は両方を抱え、set() / set_bg() で切替時に購読者へ通知.
"""

# ======================================================================
# Accent テーマ (明るい差し色)
# ======================================================================
THEMES = {
    # 既存
    "Teal":    {"accent": "#1abc9c", "accent_dark": "#16a085",
                "accent_glow": (26, 188, 156)},
    "Purple":  {"accent": "#9b59b6", "accent_dark": "#7d3c98",
                "accent_glow": (155, 89, 182)},
    "Amber":   {"accent": "#f39c12", "accent_dark": "#c87f0a",
                "accent_glow": (243, 156, 18)},
    "Crimson": {"accent": "#e74c3c", "accent_dark": "#c0392b",
                "accent_glow": (231, 76, 60)},
    "Ice":     {"accent": "#3498db", "accent_dark": "#2874a6",
                "accent_glow": (52, 152, 219)},
    # 追加
    "Mint":    {"accent": "#2ecc71", "accent_dark": "#27ae60",
                "accent_glow": (46, 204, 113)},
    "Rose":    {"accent": "#ec7063", "accent_dark": "#cb4335",
                "accent_glow": (236, 112, 99)},
    "Lime":    {"accent": "#cddc39", "accent_dark": "#9e9d24",
                "accent_glow": (205, 220, 57)},
    "Indigo":  {"accent": "#5c6bc0", "accent_dark": "#3949ab",
                "accent_glow": (92, 107, 192)},
    "Sky":     {"accent": "#4fc3f7", "accent_dark": "#0288d1",
                "accent_glow": (79, 195, 247)},
    "Sunset":  {"accent": "#ff7043", "accent_dark": "#d84315",
                "accent_glow": (255, 112, 67)},
    "Forest":  {"accent": "#66bb6a", "accent_dark": "#2e7d32",
                "accent_glow": (102, 187, 106)},
    "Magenta": {"accent": "#e91e63", "accent_dark": "#ad1457",
                "accent_glow": (233, 30, 99)},
    "Gold":    {"accent": "#ffc107", "accent_dark": "#ff8f00",
                "accent_glow": (255, 193, 7)},
    "Pearl":   {"accent": "#bdbdbd", "accent_dark": "#757575",
                "accent_glow": (189, 189, 189)},
}
DEFAULT_THEME = "Teal"


# ======================================================================
# Background パレット (本体の暗さ・色味)
# ======================================================================
BG_PALETTES = {
    "Midnight": {   # 現状 (標準ダーク)
        "bg_deep":      "#141416",
        "bg_panel":     "#1f1f21",
        "bg_card":      "#222225",
        "border":       "#2a2a2c",
        "border_hover": "#353538",
        "text_main":    "#e8e8e8",
        "text_dim":     "#9a9a9a",
        "text_faint":   "#6a6a6a",
    },
    "Jet Black": {   # 完全黒
        "bg_deep":      "#070708",
        "bg_panel":     "#101011",
        "bg_card":      "#151517",
        "border":       "#202022",
        "border_hover": "#2c2c30",
        "text_main":    "#e8e8e8",
        "text_dim":     "#909090",
        "text_faint":   "#5a5a5a",
    },
    "Charcoal": {   # やや明るい炭色
        "bg_deep":      "#1e1e20",
        "bg_panel":     "#28282b",
        "bg_card":      "#2d2d30",
        "border":       "#3a3a3e",
        "border_hover": "#4a4a4f",
        "text_main":    "#ececec",
        "text_dim":     "#a5a5a5",
        "text_faint":   "#7a7a7a",
    },
    "Deep Ocean": {   # 紺黒
        "bg_deep":      "#0b111c",
        "bg_panel":     "#131c2a",
        "bg_card":      "#172238",
        "border":       "#24304a",
        "border_hover": "#2f3e5c",
        "text_main":    "#e4ebf5",
        "text_dim":     "#93a4c2",
        "text_faint":   "#5e6f8c",
    },
    "Warm": {   # 暖色寄り暗茶
        "bg_deep":      "#18130f",
        "bg_panel":     "#231c16",
        "bg_card":      "#2a2119",
        "border":       "#3a2e24",
        "border_hover": "#4a3b2e",
        "text_main":    "#efe5d9",
        "text_dim":     "#a9998a",
        "text_faint":   "#796a5c",
    },
    "Slate": {   # 青灰
        "bg_deep":      "#141820",
        "bg_panel":     "#1d232e",
        "bg_card":      "#232a36",
        "border":       "#2e3642",
        "border_hover": "#3d4654",
        "text_main":    "#e6eaf0",
        "text_dim":     "#9aa4b3",
        "text_faint":   "#6b7586",
    },
    # --- 近未来 HUD パレット (5種) ---
    "Cyber Cyan": {   # シアン + パープル (Tron系)
        "bg_deep":      "#05080f",
        "bg_panel":     "#0a1424",
        "bg_card":      "#0e1a30",
        "border":       "#1f3a5c",
        "border_hover": "#00e5ff",
        "text_main":    "#e0f7ff",
        "text_dim":     "#7fc7e6",
        "text_faint":   "#4a7a99",
    },
    "Neon Magenta": {   # マゼンタ + シアン (サイバーパンク)
        "bg_deep":      "#0a0510",
        "bg_panel":     "#1a0e24",
        "bg_card":      "#22122e",
        "border":       "#3a2052",
        "border_hover": "#ff2dd2",
        "text_main":    "#f5e0ff",
        "text_dim":     "#c98be0",
        "text_faint":   "#7a5290",
    },
    "Matrix Green": {   # 緑モノクロ (映画 Matrix 風)
        "bg_deep":      "#020806",
        "bg_panel":     "#061410",
        "bg_card":      "#0a1c16",
        "border":       "#163826",
        "border_hover": "#00ff66",
        "text_main":    "#c8ffd6",
        "text_dim":     "#5fbf80",
        "text_faint":   "#2e6644",
    },
    "Amber HUD": {   # 軍用 HUD 風 (Fallout/Alien)
        "bg_deep":      "#0a0805",
        "bg_panel":     "#1c140a",
        "bg_card":      "#241a0e",
        "border":       "#3a2c15",
        "border_hover": "#ffb84d",
        "text_main":    "#ffe5b4",
        "text_dim":     "#d99a4a",
        "text_faint":   "#7a5a2e",
    },
    "Arctic Blue": {   # 冷たい青白 (氷河 / Mass Effect)
        "bg_deep":      "#070d18",
        "bg_panel":     "#0e1a2c",
        "bg_card":      "#13243a",
        "border":       "#1f3a56",
        "border_hover": "#82d8ff",
        "text_main":    "#e8f4ff",
        "text_dim":     "#9ccfe8",
        "text_faint":   "#5a8aab",
    },
}
DEFAULT_BG = "Midnight"

# 後方互換: モジュール定数 (古いコードが import する用)
_DEFAULT_PAL = BG_PALETTES[DEFAULT_BG]
BG_DEEP = _DEFAULT_PAL["bg_deep"]
BG_PANEL = _DEFAULT_PAL["bg_panel"]
BG_CARD = _DEFAULT_PAL["bg_card"]
BORDER = _DEFAULT_PAL["border"]
BORDER_SOFT = _DEFAULT_PAL["border"]
TEXT_MAIN = _DEFAULT_PAL["text_main"]
TEXT_DIM = _DEFAULT_PAL["text_dim"]
TEXT_FAINT = _DEFAULT_PAL["text_faint"]


# ======================================================================
class ThemeManager:
    """Accent と Background の両軸を保持し、変更通知を購読者に配る."""

    def __init__(self, name=DEFAULT_THEME, bg_name=DEFAULT_BG):
        self.name = name if name in THEMES else DEFAULT_THEME
        self.bg_name = bg_name if bg_name in BG_PALETTES else DEFAULT_BG
        self._listeners = []

    def subscribe(self, callback):
        """テーマ変更時 (accent / bg どちらでも) に呼ばれる."""
        self._listeners.append(callback)

    def _notify(self):
        for cb in self._listeners:
            try:
                cb(self)
            except Exception as e:
                print(f"[theme] listener error: {e}")

    def set(self, name):
        if name not in THEMES or name == self.name:
            return
        self.name = name
        self._notify()

    def set_bg(self, bg_name):
        if bg_name not in BG_PALETTES or bg_name == self.bg_name:
            return
        self.bg_name = bg_name
        self._notify()

    # --- Accent ----------------------------------------------------------
    @property
    def accent(self):
        return THEMES[self.name]["accent"]

    @property
    def accent_dark(self):
        return THEMES[self.name]["accent_dark"]

    @property
    def accent_glow(self):
        return THEMES[self.name]["accent_glow"]

    def accent_rgba(self, alpha=60):
        r, g, b = self.accent_glow
        return (r, g, b, alpha)

    # --- Background palette ---------------------------------------------
    @property
    def palette(self):
        return BG_PALETTES[self.bg_name]

    @property
    def bg_deep(self):
        return self.palette["bg_deep"]

    @property
    def bg_panel(self):
        return self.palette["bg_panel"]

    @property
    def bg_card(self):
        return self.palette["bg_card"]

    @property
    def border(self):
        return self.palette["border"]

    @property
    def border_hover(self):
        return self.palette["border_hover"]

    @property
    def text_main(self):
        return self.palette["text_main"]

    @property
    def text_dim(self):
        return self.palette["text_dim"]

    @property
    def text_faint(self):
        return self.palette["text_faint"]
