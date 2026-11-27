#!/usr/bin/env python3
"""Join gameplay clips into one long video and schedule it on YouTube.

Commands:
  python autopost.py auth      sign in to your YouTube channel (once)
  python autopost.py preview   build videos and show the details, no upload
  python autopost.py run       build, upload and schedule every ready video
"""
import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
VIDEOS = ROOT / "videos"
WORK = ROOT / "work"
STATE = ROOT / "state.json"
TOKEN = ROOT / "token.json"
SECRET = ROOT / "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIP_TYPES = {".mp4", ".mkv", ".mov", ".webm", ".flv", ".ts"}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MAX_SECONDS = 12 * 3600
log = logging.getLogger("autopost")


class VideoError(Exception):
    """A problem with one video folder that the user can fix."""


def read_json(path):
    try:
        # utf-8-sig also accepts files saved by Windows Notepad with a BOM
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise VideoError(f"{path.name} is not valid JSON (line {e.lineno}): {e.msg}")


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def natural_key(path):
    """Sort clip2 before clip10."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", path.name)]


def find_clips(folder):
    return sorted((p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in CLIP_TYPES), key=natural_key)


def run_tool(args):
    result = subprocess.run(args, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise VideoError(f"{args[0]} failed:\n{result.stderr[-1500:]}")
    return result.stdout


def probe(path):
    out = run_tool(["ffprobe", "-v", "error", "-show_entries",
                    "format=duration:stream=codec_type", "-of", "json", str(path)])
    data = json.loads(out)
    kinds = [s["codec_type"] for s in data.get("streams", [])]
    if "video" not in kinds:
        raise VideoError(f"{path.name} has no video track")
    return float(data["format"]["duration"]), kinds.count("audio")


def normalise_clip(src, dst, cfg):
    """Same size, frame rate and loudness for every clip, so they join cleanly."""
    duration, audio_count = probe(src)
    w, h, fps = cfg["width"], cfg["height"], cfg["fps"]
    video = (f"[0:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v]")
    level = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,apad[a]"
    args = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if audio_count == 0:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        audio = "[1:a]apad[a]"
    elif audio_count > 1 and cfg.get("audio_tracks") == "mix":
        inputs = "".join(f"[0:a:{i}]" for i in range(audio_count))
        audio = f"{inputs}amix=inputs={audio_count}:normalize=0,{level}"
    else:
        audio = f"[0:a:0]{level}"
    # apad + -t keeps sound exactly as long as the picture, so nothing drifts
    args += ["-filter_complex", f"{video};{audio}", "-map", "[v]", "-map", "[a]",
             "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(dst)]
    run_tool(args)


def fingerprint(clips, cfg):
    parts = [[c.name, c.stat().st_size, c.stat().st_mtime_ns] for c in clips]
    keys = ("width", "height", "fps", "audio_tracks")
    raw = json.dumps([parts, [cfg.get(k) for k in keys]])
    return hashlib.sha256(raw.encode()).hexdigest()


def build_video(folder, cfg):
    clips = find_clips(folder)
    if not clips:
        raise VideoError("no video clips in this folder")
    out = WORK / folder.name
    final, info = out / "final.mp4", out / "build.json"
    stamp = fingerprint(clips, cfg)
    if final.exists() and info.exists():
        saved = read_json(info)
        if saved["fingerprint"] == stamp:
            log.info("Using the video already built for %s", folder.name)
            return final, saved["chapters"]
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    chapters, total, parts = [], 0.0, []
    for i, clip in enumerate(clips):
        log.info("Processing clip %d of %d: %s", i + 1, len(clips), clip.name)
        part = out / f"part{i:04d}.mkv"
        normalise_clip(clip, part, cfg)
        seconds, _ = probe(part)
        # "03 - Boss Fight.mp4" becomes the chapter "Boss Fight"
        name = re.sub(r"^\d+[\s._-]*", "", clip.stem).replace("_", " ").strip()
        chapters.append([total, name or f"Part {i + 1}", seconds])
        total += seconds
        parts.append(part)
    if total > MAX_SECONDS:
        raise VideoError("the joined video is longer than YouTube's 12 hour limit")
    listing = out / "parts.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    log.info("Joining %d clips (%s)", len(parts), timestamp(total))
    run_tool(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
              "-i", str(listing), "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-movflags", "+faststart", str(final)])
    for p in parts:
        p.unlink()
    write_json(info, {"fingerprint": stamp, "chapters": chapters})
    return final, chapters


def timestamp(seconds):
    s = int(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def chapter_lines(chapters):
    # YouTube needs at least 3 chapters of 10 seconds or more
    if len(chapters) < 3 or any(c[2] < 10 for c in chapters):
        return
    return [f"{timestamp(start)} {name}" for start, name, _ in chapters]


def check_details(meta):
    for key in ("title", "summary", "game", "publisher"):
        if not str(meta.get(key, "")).strip():
            raise VideoError(f'video.json needs "{key}"')
    template = ("Game Name", "Chapter Name", "Publisher Name",
                "Write one or two sentences")
    for key in ("title", "summary", "game", "publisher"):
        if any(t in meta[key] for t in template):
            raise VideoError(f'"{key}" still has the example text')
    if len(meta["title"]) > 100:
        raise VideoError("the title is longer than 100 characters")
    if any(c in meta["title"] + meta["summary"] for c in "<>"):
        raise VideoError("YouTube does not allow < or > in titles and descriptions")


def build_details(meta, chapters, cfg):
    check_details(meta)
    blocks = [meta["summary"].strip()]
    lines = chapter_lines(chapters)
    if lines:
        blocks.append("Chapters\n" + "\n".join(lines))
    blocks.append(f"Game: {meta['game']} by {meta['publisher']}")
    if cfg.get("description_footer"):
        blocks.append(cfg["description_footer"])
    description = "\n\n".join(blocks).replace("<", "").replace(">", "")
    if len(description.encode("utf-8")) > 5000:
        raise VideoError("the description is over 5000 bytes; use fewer or shorter chapters")
    tags, used = [], 0
    for tag in meta.get("tags", []) + cfg.get("default_tags", []):
        cost = len(tag) + (2 if " " in tag else 0) + 1
        if tag not in tags and used + cost <= 500:
            tags.append(tag)
            used += cost
    return {"title": meta["title"].strip(), "description": description, "tags": tags}


def load_font(size):
    from PIL import ImageFont
    for name in ("impact.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf",
                 "/System/Library/Fonts/Supplemental/Impact.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def make_thumbnail(folder, video, meta):
    """Use thumbnail.jpg/png from the folder, or make one from a video frame."""
    from PIL import Image, ImageDraw
    out = WORK / folder.name / "thumbnail.jpg"
    own = next((folder / n for n in ("thumbnail.jpg", "thumbnail.png")
                if (folder / n).exists()), None)
    if own:
        image = Image.open(own).convert("RGB")
        image.thumbnail((1280, 720))
    else:
        frame = WORK / folder.name / "frame.png"
        duration, _ = probe(video)
        at = min(float(meta.get("thumbnail_at_seconds", 30)), max(duration - 1, 0))
        run_tool(["ffmpeg", "-y", "-v", "error", "-ss", str(at), "-i", str(video),
                  "-frames:v", "1", "-vf",
                  "scale=1280:720:force_original_aspect_ratio=decrease,"
                  "pad=1280:720:(ow-iw)/2:(oh-ih)/2", str(frame)])
        image = Image.open(frame).convert("RGB")
        text = str(meta.get("thumbnail_text", "")).upper().strip()
        if text:
            draw = ImageDraw.Draw(image)
            size = 150
            while size > 40 and draw.textlength(text, font=load_font(size)) > 1180:
                size -= 6
            draw.text((50, 670), text, font=load_font(size), anchor="ls",
                      fill="yellow", stroke_width=max(size // 14, 3), stroke_fill="black")
    # YouTube rejects thumbnails over 2 MB
    for quality in (92, 82, 70, 55):
        image.save(out, "JPEG", quality=quality)
        if out.stat().st_size < 2_000_000:
            break
    return out


def next_slot(cfg, taken, now=None):
    """First free publishing time that is far enough in the future."""
    zone = ZoneInfo(cfg["timezone"])
    now = (now or datetime.now(zone)).astimezone(zone)
    earliest = now + timedelta(hours=cfg["min_hours_before_publish"])
    try:
        days = {DAYS.index(d.lower()) for d in cfg["publish_days"]}
        hour, minute = map(int, cfg["publish_time"].split(":"))
    except ValueError:
        raise SystemExit("config.json: check publish_days and publish_time")
    if not days:
        raise SystemExit("config.json: publish_days is empty")
    used = {datetime.fromisoformat(t.replace("Z", "+00:00")) for t in taken}
    for offset in range(0, 3700):
        day = (earliest + timedelta(days=offset)).date()
        slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        if day.weekday() in days and slot >= earliest and slot not in used:
            return slot
    raise SystemExit("No free publishing slot found")


def to_utc(slot):
    return slot.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def credentials(interactive=False):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    creds = None
    if TOKEN.exists() and not interactive:
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise SystemExit("Not signed in. Run: python autopost.py auth")
        if not SECRET.exists():
            raise SystemExit("client_secret.json is missing. See README.md, step 3.")
        flow = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def youtube_client():
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=credentials())


def upload_video(youtube, video, details, publish_at, cfg):
    from googleapiclient.http import MediaFileUpload
    body = {
        "snippet": {**details, "categoryId": cfg["category_id"]},
        # A scheduled video must be private; YouTube makes it public at publishAt
        "status": {"privacyStatus": "private", "publishAt": publish_at,
                   "selfDeclaredMadeForKids": cfg["made_for_kids"],
                   "containsSyntheticMedia": cfg["contains_synthetic_media"]},
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4",
                            chunksize=16 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        progress, response = request.next_chunk(num_retries=8)
        if progress:
            log.info("Uploaded %d%%", progress.progress() * 100)
    return response["id"]


def set_thumbnail(youtube, video_id, image):
    from googleapiclient.http import MediaFileUpload
    youtube.thumbnails().set(
        videoId=video_id, media_body=MediaFileUpload(str(image), mimetype="image/jpeg")
    ).execute(num_retries=5)


def ready_folders(state):
    if not VIDEOS.exists():
        return
    for folder in sorted((p for p in VIDEOS.iterdir() if p.is_dir()), key=natural_key):
        if folder.name in state or not (folder / "video.json").exists():
            continue
        try:
            meta = read_json(folder / "video.json")
        except VideoError as e:
            log.error("%s: %s", folder.name, e)
            yield folder, None
            continue
        if meta.get("ready") is True:
            yield folder, meta


def process(preview):
    cfg = read_json(ROOT / "config.json")
    # A damaged state.json stops the run, so nothing gets uploaded twice
    state = read_json(STATE) if STATE.exists() else {}
    youtube, failed, found = None, 0, 0
    taken = [v["publish_at"] for v in state.values()]
    for folder, meta in ready_folders(state):
        found += 1
        if meta is None:
            failed += 1
            continue
        try:
            check_details(meta)
            video, chapters = build_video(folder, cfg)
            details = build_details(meta, chapters, cfg)
            thumb = make_thumbnail(folder, video, meta)
            if not preview and youtube is None:
                youtube = youtube_client()
            slot = next_slot(cfg, taken)
            if preview:
                taken.append(to_utc(slot))
                print(f"\n=== {folder.name} ===\nVideo:     {video}\nThumbnail: {thumb}\n"
                      f"Publish:   {slot:%A %d %B %Y %H:%M %Z}\nTitle:     {details['title']}\n"
                      f"Tags:      {', '.join(details['tags'])}\n\n{details['description']}\n")
                continue
            publish_at = to_utc(slot)
            video_id = upload_video(youtube, video, details, publish_at, cfg)
            # Save at once: a later failure must never cause a second upload
            state[folder.name] = {"video_id": video_id, "publish_at": publish_at}
            write_json(STATE, state)
            taken.append(publish_at)
            log.info("Scheduled %s for %s: https://youtu.be/%s", folder.name,
                     slot.strftime("%a %d %b %H:%M"), video_id)
            try:
                set_thumbnail(youtube, video_id, thumb)
            except Exception as e:
                log.warning("Video uploaded, but the thumbnail failed (%s). "
                            "Add it in YouTube Studio: %s", e, thumb)
                continue
            video.unlink()
        except VideoError as e:
            failed += 1
            log.error("%s: %s", folder.name, e)
        except Exception:
            failed += 1
            log.exception("%s: failed, it will be tried again next run", folder.name)
    if not found:
        log.info('Nothing to do. Set "ready": true in a new folder\'s video.json.')
    return 1 if failed else 0


def lock():
    """Stop two runs working at the same time. Freed automatically on exit."""
    handle = open(ROOT / ".lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("Another run is still working. Try again later.")
    return handle


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["auth", "preview", "run"])
    command = parser.parse_args().command
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(ROOT / "autopost.log", encoding="utf-8")])
    if command == "auth":
        credentials(interactive=True)
        print("Signed in. You can now use: python autopost.py run")
        return 0
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is not installed. See README.md, step 1.")
    held = lock()
    try:
        return process(preview=command == "preview")
    finally:
        held.close()


if __name__ == "__main__":
    sys.exit(main())
