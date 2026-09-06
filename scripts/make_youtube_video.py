#!/usr/bin/env python3
"""Build a ~3 minute Quant-Striker YouTube video from high-confidence picks.

Usage:
  cd /workspace/football-platform
  PYTHONPATH=. .venv/bin/python scripts/make_youtube_video.py
  PYTHONPATH=. .venv/bin/python scripts/make_youtube_video.py --no-tts
  PYTHONPATH=. .venv/bin/python scripts/make_youtube_video.py --n 6

Outputs under data/youtube/:
  picks.json, script_zh.md, title.txt, description.txt, tags.txt,
  quant_striker_picks.mp4
Also persists data/youtube_picks_YYYYMMDD.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.youtube_picks import (  # noqa: E402
    DISCLAIMER_ZH,
    picks_to_narration,
    select_youtube_picks,
    youtube_metadata,
)

OUT_DIR = ROOT / "data" / "youtube"
FONT_REG = ROOT / "assets" / "fonts" / "NotoSansCJK-Regular.ttc"
FONT_BOLD = ROOT / "assets" / "fonts" / "NotoSansCJK-Bold.ttc"
# Noto Sans CJK TTC: 0=JP 1=KR 2=SC 3=TC 4=HK
FONT_INDEX_TC = 3

BG = (11, 18, 32)  # #0b1220
GREEN = (34, 197, 94)  # #22c55e
GREEN_DIM = (21, 128, 61)
WHITE = (226, 232, 240)
MUTED = (148, 163, 184)
CARD = (15, 23, 42)
ACCENT = (56, 189, 248)
RED = (248, 113, 113)
AMBER = (251, 191, 36)

W, H = 1920, 1080
TITLE_SECS = 15.0
MATCH_SECS = 25.0
CTA_SECS = 15.0


def _ensure_deps() -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pillow", "-q"]
        )


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    path = FONT_BOLD if bold and FONT_BOLD.exists() else FONT_REG
    if not path.exists():
        # system fallback
        for p in (
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ):
            if p.exists():
                path = p
                break
    try:
        return ImageFont.truetype(str(path), size=size, index=FONT_INDEX_TC)
    except Exception:
        try:
            return ImageFont.truetype(str(path), size=size)
        except Exception:
            return ImageFont.load_default()


def _rounded_rect(draw, xy, radius, fill):
    draw.rounded_rectangle(xy, radius=radius, fill=fill)


def _text_center(draw, xy_center, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = xy_center[0] - tw // 2
    y = xy_center[1] - th // 2
    draw.text((x, y), text, font=font, fill=fill)


def _wrap(draw, text: str, font, max_width: int) -> List[str]:
    if not text:
        return []
    lines: List[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _conf_color(tier: str):
    t = (tier or "").upper()
    if t == "STRONG":
        return GREEN
    if t == "LEAN":
        return ACCENT
    return AMBER


def _conf_zh(tier: str) -> str:
    return {"STRONG": "高信心 STRONG", "LEAN": "偏傾 LEAN", "TOSS-UP": "膠著 TOSS-UP"}.get(
        (tier or "").upper(), tier or "—"
    )


def render_title_card(date_hkt: str, path: Path) -> None:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(im)
    # top accent bar
    draw.rectangle([0, 0, W, 8], fill=GREEN)
    _rounded_rect(draw, (160, 220, W - 160, H - 220), 28, CARD)
    draw.rectangle([160, 220, 180, H - 220], fill=GREEN)

    title_f = _font(64, bold=True)
    sub_f = _font(36, bold=True)
    body_f = _font(28)
    small_f = _font(22)

    _text_center(draw, (W // 2, 340), "Quant-Striker", title_f, GREEN)
    _text_center(draw, (W // 2, 430), "今日六場高信心預測", sub_f, WHITE)
    _text_center(draw, (W // 2, 520), f"香港時間 {date_hkt}", body_f, MUTED)
    _text_center(draw, (W // 2, 620), DISCLAIMER_ZH, small_f, AMBER)
    _text_center(
        draw,
        (W // 2, 700),
        "Statistical estimates for education — not gambling advice",
        small_f,
        MUTED,
    )
    im.save(path)


def render_match_card(pick: Dict[str, Any], path: Path) -> None:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, 0, W, 8], fill=GREEN)

    rank = pick.get("rank") or 1
    home = pick.get("home") or ""
    away = pick.get("away") or ""
    league = pick.get("league") or ""
    kick = pick.get("kickoff_hkt_display") or ""
    conf = pick.get("confidence") or "TOSS-UP"
    ph = float(pick.get("p_home") or 0)
    pd = float(pick.get("p_draw") or 0)
    pa = float(pick.get("p_away") or 0)
    score = pick.get("scoreline") or "—"
    note = pick.get("factor_note") or ""

    # header
    hdr = _font(28, bold=True)
    draw.text((80, 40), f"場次 {rank} / 6", font=hdr, fill=MUTED)
    draw.text((W - 420, 40), "Quant-Striker", font=hdr, fill=GREEN)

    _rounded_rect(draw, (80, 100, W - 80, H - 100), 24, CARD)

    title_f = _font(52, bold=True)
    vs_f = _font(36, bold=True)
    meta_f = _font(30)
    big_f = _font(44, bold=True)
    pct_f = _font(40, bold=True)
    note_f = _font(26)
    badge_f = _font(28, bold=True)

    # teams
    _text_center(draw, (W // 2, 200), f"{home}", title_f, WHITE)
    _text_center(draw, (W // 2, 270), "vs", vs_f, MUTED)
    _text_center(draw, (W // 2, 340), f"{away}", title_f, WHITE)

    _text_center(draw, (W // 2, 420), f"{league}  ·  {kick}", meta_f, MUTED)

    # confidence badge
    badge = _conf_zh(conf)
    col = _conf_color(conf)
    bb = draw.textbbox((0, 0), badge, font=badge_f)
    bw, bh = bb[2] - bb[0] + 48, bb[3] - bb[1] + 24
    bx = (W - bw) // 2
    by = 460
    _rounded_rect(draw, (bx, by, bx + bw, by + bh), 12, (20, 40, 30) if conf == "STRONG" else (20, 30, 45))
    _text_center(draw, (W // 2, by + bh // 2), badge, badge_f, col)

    # probability bars
    y0 = 560
    labels = [("主勝", ph, GREEN), ("和局", pd, AMBER), ("客勝", pa, ACCENT)]
    bar_left, bar_right = 280, W - 280
    bar_w = bar_right - bar_left
    for i, (lab, p, color) in enumerate(labels):
        y = y0 + i * 70
        draw.text((120, y), lab, font=big_f, fill=WHITE)
        # track
        _rounded_rect(draw, (bar_left, y + 12, bar_right, y + 36), 8, (30, 41, 59))
        fill_w = int(bar_w * max(0.0, min(1.0, p)))
        if fill_w > 8:
            _rounded_rect(
                draw, (bar_left, y + 12, bar_left + fill_w, y + 36), 8, color
            )
        draw.text((bar_right + 24, y), f"{int(round(p * 100))}%", font=pct_f, fill=color)

    # scoreline
    _text_center(
        draw,
        (W // 2, 800),
        f"參考比分  {score}",
        big_f,
        WHITE,
    )

    if note:
        lines = _wrap(draw, f"因素：{note}", note_f, W - 240)
        for i, line in enumerate(lines[:2]):
            _text_center(draw, (W // 2, 880 + i * 36), line, note_f, MUTED)

    _text_center(draw, (W // 2, 1000), DISCLAIMER_ZH, _font(20), (100, 116, 139))
    im.save(path)


def render_cta_card(path: Path) -> None:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, 0, W, 8], fill=GREEN)
    _rounded_rect(draw, (200, 260, W - 200, H - 260), 28, CARD)
    _text_center(draw, (W // 2, 420), "訂閱 Quant-Striker", _font(64, bold=True), GREEN)
    _text_center(
        draw, (W // 2, 520), "每日高信心統計預測摘要", _font(36), WHITE
    )
    _text_center(draw, (W // 2, 640), DISCLAIMER_ZH, _font(28), AMBER)
    _text_center(
        draw,
        (W // 2, 720),
        "Not gambling advice · Research / education only",
        _font(24),
        MUTED,
    )
    im.save(path)


def _segment_narrations(payload: Dict[str, Any]) -> List[Tuple[str, float]]:
    """Return list of (text, duration) for TTS segments matching video."""
    date = payload.get("date_hkt") or ""
    segs: List[Tuple[str, float]] = []
    segs.append(
        (
            f"歡迎收看 Quant-Striker。今日六場高信心預測。日期 {date}。{DISCLAIMER_ZH}",
            TITLE_SECS,
        )
    )
    for p in payload.get("picks") or []:
        ph = int(round(float(p["p_home"]) * 100))
        pd = int(round(float(p["p_draw"]) * 100))
        pa = int(round(float(p["p_away"]) * 100))
        conf_zh = {"STRONG": "高信心", "LEAN": "偏傾", "TOSS-UP": "膠著"}.get(
            p.get("confidence") or "", p.get("confidence") or ""
        )
        note = p.get("factor_note") or ""
        text = (
            f"第{p.get('rank')}場。{p.get('home')}對{p.get('away')}。"
            f"聯賽{p.get('league')}，開賽{p.get('kickoff_hkt_display')}。"
            f"信心等級{conf_zh}。主勝{ph}％，和局{pd}％，客勝{pa}％。"
            f"參考比分{p.get('scoreline')}。"
        )
        if note:
            text += f"因素提示：{note}"
        segs.append((text, MATCH_SECS))
    segs.append(
        (
            f"感謝收看。請訂閱 Quant-Striker。{DISCLAIMER_ZH}",
            CTA_SECS,
        )
    )
    return segs


async def _tts_one(text: str, out_mp3: Path, voice: str) -> bool:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice=voice)
    await communicate.save(str(out_mp3))
    return out_mp3.exists() and out_mp3.stat().st_size > 0


def try_generate_tts(
    segments: List[Tuple[str, float]], work: Path, voice: str
) -> Optional[Path]:
    """Generate per-segment mp3, pad/trim to duration, concat → audio.mp3."""
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "edge-tts", "-q"]
            )
            import edge_tts  # noqa: F401
        except Exception as e:
            print(f"[tts] edge-tts unavailable: {e}", file=sys.stderr)
            return None

    mp3s: List[Path] = []
    for i, (text, dur) in enumerate(segments):
        raw = work / f"tts_{i:02d}_raw.mp3"
        padded = work / f"tts_{i:02d}.mp3"
        try:
            ok = asyncio.run(_tts_one(text, raw, voice))
        except Exception as e:
            print(f"[tts] segment {i} failed: {e}", file=sys.stderr)
            return None
        if not ok:
            print(f"[tts] segment {i} empty", file=sys.stderr)
            return None
        # pad or trim to exact duration with anull/apad
        # Use apad + atrim to force exact length
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(raw),
            "-af",
            f"apad=whole_dur={dur},atrim=0:{dur}",
            "-t",
            str(dur),
            str(padded),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[tts] pad failed: {r.stderr[-400:]}", file=sys.stderr)
            return None
        mp3s.append(padded)

    # concat
    list_file = work / "audio_concat.txt"
    list_file.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in mp3s), encoding="utf-8"
    )
    out_audio = work / "narration.mp3"
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(out_audio),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # re-encode if copy fails
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(out_audio),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[tts] concat failed: {r.stderr[-400:]}", file=sys.stderr)
            return None
    return out_audio


def assemble_video(
    frames: List[Tuple[Path, float]],
    audio: Optional[Path],
    out_mp4: Path,
    work: Path,
) -> float:
    """Assemble still frames into MP4; return duration seconds."""
    # concat demuxer with duration
    concat_path = work / "frames.txt"
    lines = []
    total = 0.0
    for img, dur in frames:
        lines.append(f"file '{img.resolve()}'")
        lines.append(f"duration {dur}")
        total += dur
    # last frame must be repeated without duration for concat demuxer
    if frames:
        lines.append(f"file '{frames[-1][0].resolve()}'")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    silent = work / "silent.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-vf",
        f"fps=30,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-t",
        str(total),
        str(silent),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg video failed:\n{r.stderr[-800:]}")

    if audio and audio.exists():
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(silent),
            "-i",
            str(audio),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_mp4),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[warn] mux audio failed, using silent: {r.stderr[-300:]}", file=sys.stderr)
            shutil.copy(silent, out_mp4)
    else:
        shutil.copy(silent, out_mp4)

    # probe duration
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(out_mp4),
        ],
        capture_output=True,
        text=True,
    )
    try:
        dur = float(probe.stdout.strip())
    except Exception:
        dur = total
    return dur


def write_outputs(payload: Dict[str, Any], out_dir: Path) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, Path] = {}

    picks_path = out_dir / "picks.json"
    picks_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    paths["picks"] = picks_path

    script = picks_to_narration(payload)
    script_path = out_dir / "script_zh.md"
    script_path.write_text(script, encoding="utf-8")
    paths["script"] = script_path

    meta = youtube_metadata(payload)
    for key, fname in (
        ("title", "title.txt"),
        ("description", "description.txt"),
        ("tags", "tags.txt"),
    ):
        p = out_dir / fname
        p.write_text(meta[key] if key != "tags" else meta["tags"] + "\n", encoding="utf-8")
        paths[key] = p
    return paths


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Make Quant-Striker YouTube video")
    parser.add_argument("--n", type=int, default=6, help="Number of matches (default 6)")
    parser.add_argument("--no-tts", action="store_true", help="Skip TTS (mute + on-screen text)")
    parser.add_argument(
        "--voice",
        default="zh-HK-HiuMaanNeural",
        help="edge-tts voice (zh-HK or zh-TW)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=OUT_DIR,
        help="Output directory (default data/youtube)",
    )
    args = parser.parse_args(argv)

    _ensure_deps()

    print("Selecting picks…")
    payload = select_youtube_picks(n=args.n, persist=True)
    if payload["count"] < args.n:
        print(
            f"[warn] only {payload['count']} picks available (requested {args.n})",
            file=sys.stderr,
        )
    if payload["count"] == 0:
        print("ERROR: no picks available", file=sys.stderr)
        return 1

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = write_outputs(payload, out_dir)
    print(f"Wrote {paths['picks']}")
    print(f"Wrote {paths['script']}")

    with tempfile.TemporaryDirectory(prefix="qs_yt_") as td:
        work = Path(td)
        frames: List[Tuple[Path, float]] = []

        title_png = work / "title.png"
        render_title_card(payload.get("date_hkt") or "", title_png)
        frames.append((title_png, TITLE_SECS))

        for p in payload["picks"]:
            png = work / f"match_{p['rank']:02d}.png"
            render_match_card(p, png)
            frames.append((png, MATCH_SECS))

        cta_png = work / "cta.png"
        render_cta_card(cta_png)
        frames.append((cta_png, CTA_SECS))

        audio: Optional[Path] = None
        if not args.no_tts:
            print(f"Generating TTS ({args.voice})…")
            segs = _segment_narrations(payload)
            # If fewer than 6 picks, still match frame count
            # Rebuild segs length to frames
            if len(segs) != len(frames):
                # truncate/pad durations already tied to picks count
                pass
            audio = try_generate_tts(segs, work, args.voice)
            if audio is None:
                print("[warn] TTS failed — producing mute video with on-screen text")
        else:
            print("Skipping TTS (--no-tts)")

        out_mp4 = out_dir / "quant_striker_picks.mp4"
        print("Assembling video with ffmpeg…")
        duration = assemble_video(frames, audio, out_mp4, work)

    print(f"Video: {out_mp4} ({duration:.1f}s)")
    print("Picks:")
    for p in payload["picks"]:
        print(
            f"  #{p['rank']} [{p['confidence']}] {p['home']} vs {p['away']} "
            f"| {p['league']} | {p['kickoff_hkt_display']} "
            f"| 主{p['p_home']:.0%} 和{p['p_draw']:.0%} 客{p['p_away']:.0%} "
            f"| {p['scoreline']}"
        )
    if duration < 150 or duration > 210:
        print(
            f"[warn] duration {duration:.1f}s outside 150–210s target",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
