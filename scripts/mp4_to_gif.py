"""
mp4_to_gif.py
=============
動画ファイルから GIF を作成 (README 用).

使い方:
    python scripts/mp4_to_gif.py <input.mp4> [--start 5] [--end 15] [--width 720] [--fps 12]

例:
    python scripts/mp4_to_gif.py "C:/Users/hiro2/Videos/Captures/demo.mp4" \\
        --start 5 --end 15 --width 720 --fps 12
"""
import argparse
import os
import sys

try:
    import cv2
    from PIL import Image
except ImportError as e:
    print(f"❌ {e}\n  pip install opencv-python pillow")
    sys.exit(1)


def mp4_to_gif(src, dst, start_s=0.0, end_s=None, width=720, fps=12):
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        print(f"❌ open failed: {src}")
        return False
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    src_dur = total / src_fps
    if end_s is None or end_s > src_dur:
        end_s = src_dur
    print(f"src: {src_fps:.1f}fps, {src_dur:.1f}s")
    print(f"out: {width}px, {fps}fps, {start_s:.1f}s–{end_s:.1f}s")

    # 入力動画から GIF にするフレームを抽出
    step = src_fps / fps
    cur_t = start_s
    frames = []
    while cur_t < end_s:
        cap.set(cv2.CAP_PROP_POS_MSEC, cur_t * 1000)
        ok, frame = cap.read()
        if not ok:
            break
        h, w = frame.shape[:2]
        if w != width:
            new_h = int(h * width / w)
            frame = cv2.resize(frame, (width, new_h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frames.append(Image.fromarray(rgb))
        cur_t += 1.0 / fps
    cap.release()

    if not frames:
        print("❌ no frames")
        return False

    duration_ms = int(1000 / fps)
    print(f"saving {len(frames)} frames -> {dst}")
    frames[0].save(
        dst, save_all=True, append_images=frames[1:],
        duration=duration_ms, loop=0, optimize=True,
        # P (palette) mode に変換して軽量化
        disposal=2,
    )
    size_mb = os.path.getsize(dst) / 1024 / 1024
    print(f"✅ saved {dst} ({size_mb:.2f} MB)")
    if size_mb > 5:
        print("⚠ over 5MB. Consider --width 540 or --fps 8.")
    return True


def main():
    ap = argparse.ArgumentParser(description="MP4 → GIF for README")
    ap.add_argument("src", help="input mp4 path")
    ap.add_argument("--out", default=None,
                    help="output gif path (default: demo/demo_thumb.gif)")
    ap.add_argument("--start", type=float, default=0.0, help="start sec")
    ap.add_argument("--end", type=float, default=None, help="end sec")
    ap.add_argument("--width", type=int, default=720)
    ap.add_argument("--fps", type=int, default=12)
    args = ap.parse_args()
    if args.out is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        args.out = os.path.join(root, "demo", "demo_thumb.gif")
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
    ok = mp4_to_gif(args.src, args.out,
                    start_s=args.start, end_s=args.end,
                    width=args.width, fps=args.fps)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
