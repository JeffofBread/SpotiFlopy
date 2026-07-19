import os
import csv
import json
import logging
import msvcrt
import sys
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK, TDRC, ID3NoHeaderError


class StatusLine:
    def __init__(self, stream):
        self.stream = stream
        self.text = ""
        self.lock = threading.Lock()

    def _clear(self):
        if self.text:
            self.stream.write("\r" + " " * len(self.text) + "\r")

    def update(self, text: str):
        with self.lock:
            self._clear()
            self.stream.write(text)
            self.stream.flush()
            self.text = text

    def clear(self):
        with self.lock:
            self._clear()
            self.text = ""


status_line = StatusLine(sys.stdout)


class StatusAwareHandler(logging.StreamHandler):
    def emit(self, record):
        with status_line.lock:
            status_line._clear()
            logging.StreamHandler.emit(self, record)
            if status_line.text:
                self.stream.write(status_line.text)
                self.stream.flush()


_handler = StatusAwareHandler(stream=sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(threadName)s] %(message)s", datefmt="%H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("spotify_downloader")
ytdlp_log = logging.getLogger("yt_dlp")

VERBOSE = "--verbose" in sys.argv or os.getenv("VERBOSE", "").lower() in ("1", "true", "yes")
ytdlp_log.setLevel(logging.DEBUG if VERBOSE else logging.CRITICAL)


class YTDLPLogger:
    def debug(self, msg):
        if msg.startswith("[debug] "):
            ytdlp_log.debug(msg)
        else:
            ytdlp_log.info(msg)

    def info(self, msg):
        ytdlp_log.info(msg)

    def warning(self, msg):
        ytdlp_log.warning(msg)

    def error(self, msg):
        ytdlp_log.error(msg)


load_dotenv()

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.getenv("SPOTIPY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
    scope="user-library-read",
))

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
BASE_PATH = SCRIPT_DIR / "Songs"
BASE_PATH.mkdir(parents=True, exist_ok=True)

LIKED_SONGS_CACHE = SCRIPT_DIR / "liked_songs_cache.json"
CACHE_MAX_AGE_HOURS = float(os.getenv("LIKED_SONGS_CACHE_HOURS", "24"))

SONGS_TRACKER = BASE_PATH / "songs.csv"
if not SONGS_TRACKER.exists():
    with open(SONGS_TRACKER, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Artist", "Album", "Track", "Track Number"])

MIN_WORKERS = int(os.getenv("MIN_WORKERS", "1"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "6"))
INITIAL_WORKERS = int(os.getenv("INITIAL_WORKERS", "3"))
SUCCESS_STREAK_TO_GROW = 8
COOLDOWN_AFTER_SHRINK_SECONDS = 15
STATUS_REFRESH_SECONDS = 1


def build_js_runtime_config() -> dict:
    name = os.getenv("JS_RUNTIME_NAME", "deno")
    path = os.getenv("JS_RUNTIME_PATH")
    if path:
        return {name: {"path": path}}
    log.warning(
        f"JS_RUNTIME_PATH not set -- falling back to '{name}' on PATH. "
        f"Set JS_RUNTIME_NAME/JS_RUNTIME_PATH in .env to pin an explicit runtime and path."
    )
    return {name: {}}


def build_cookies_opts() -> dict:
    browser = os.getenv("BROWSER_COOKIES")
    profile = os.getenv("BROWSER_PROFILE")
    if not browser:
        return {}
    log.info(f"Using cookies from browser: {browser}" + (f" (profile: {profile})" if profile else ""))
    if profile:
        return {"cookiesfrombrowser": (browser, profile)}
    return {"cookiesfrombrowser": (browser,)}


JS_RUNTIME_CONFIG = build_js_runtime_config()
COOKIES_OPTS = build_cookies_opts()


class AdaptiveConcurrency:
    def __init__(self, initial: int, minimum: int, maximum: int):
        self.limit = max(minimum, min(initial, maximum))
        self.minimum = minimum
        self.maximum = maximum
        self._in_use = 0
        self._success_streak = 0
        self._last_shrink_time = 0.0
        self._cond = threading.Condition(threading.Lock())

    def acquire(self):
        with self._cond:
            while self._in_use >= self.limit:
                self._cond.wait()
            self._in_use += 1

    def release(self):
        with self._cond:
            self._in_use -= 1
            self._cond.notify_all()

    def report_error(self):
        with self._cond:
            self._success_streak = 0
            self._last_shrink_time = time.time()
            if self.limit > self.minimum:
                self.limit -= 1
                log.warning(f"Reducing concurrent downloads to {self.limit} after a rate-limit error.")
            self._cond.notify_all()

    def report_success(self):
        with self._cond:
            if self.limit >= self.maximum:
                return
            if time.time() - self._last_shrink_time < COOLDOWN_AFTER_SHRINK_SECONDS:
                return
            self._success_streak += 1
            if self._success_streak >= SUCCESS_STREAK_TO_GROW:
                self._success_streak = 0
                self.limit += 1
                log.info(f"Increasing concurrent downloads to {self.limit} after sustained success.")
                self._cond.notify_all()


class Counter:
    def __init__(self):
        self._value = 0
        self._lock = threading.Lock()

    def increment(self):
        with self._lock:
            self._value += 1

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


class RollingAverage:
    def __init__(self):
        self._total = 0.0
        self._count = 0
        self._lock = threading.Lock()

    def add(self, value: float):
        with self._lock:
            self._total += value
            self._count += 1

    @property
    def average(self) -> float:
        with self._lock:
            return self._total / self._count if self._count else 0.0


worker_limiter = AdaptiveConcurrency(INITIAL_WORKERS, MIN_WORKERS, MAX_WORKERS)
stop_event = threading.Event()
all_done_event = threading.Event()
csv_lock = threading.Lock()

succeeded_count = Counter()
failed_count = Counter()
skipped_count = Counter()
download_durations = RollingAverage()
total_to_download = 0


def sanitize(text: str) -> str:
    cleaned = "".join(c for c in text if c not in r'\/:*?"<>|').strip()
    return cleaned.rstrip(". ")


def is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "403" in text or "429" in text or "forbidden" in text or "too many requests" in text


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def render_status_text() -> str:
    downloaded = succeeded_count.value
    failed = failed_count.value
    remaining = max(total_to_download - downloaded - failed, 0)
    avg = download_durations.average

    if remaining == 0:
        eta_str = "0s"
    elif avg > 0:
        eta_seconds = (remaining * avg) / max(worker_limiter.limit, 1)
        eta_str = format_duration(eta_seconds)
    else:
        eta_str = "calculating..."

    return f"[Progress] Downloaded: {downloaded} | Failed: {failed} | Remaining: {remaining} | ETA: {eta_str}"


def status_ticker():
    while not all_done_event.is_set():
        status_line.update(render_status_text())
        time.sleep(STATUS_REFRESH_SECONDS)


def get_liked_songs():
    results = sp.current_user_saved_tracks(limit=50)
    songs = []

    while results:
        for item in results["items"]:
            track = item["track"]
            images = track["album"]["images"]

            songs.append({
                "title": track["name"],
                "artist": ", ".join(a["name"] for a in track["artists"]),
                "album": track["album"]["name"],
                "track_number": track["track_number"],
                "release_date": track["album"]["release_date"],
                "total_tracks": track["album"]["total_tracks"],
                "cover_url": images[0]["url"] if images else None,
            })

        results = sp.next(results) if results["next"] else None

    return songs


def load_cached_liked_songs():
    if not LIKED_SONGS_CACHE.exists():
        return None

    age_hours = (time.time() - LIKED_SONGS_CACHE.stat().st_mtime) / 3600
    if age_hours > CACHE_MAX_AGE_HOURS:
        return None

    try:
        with open(LIKED_SONGS_CACHE, "r", encoding="utf-8") as f:
            songs = json.load(f)
        log.info(f"Loaded {len(songs)} liked songs from cache ({age_hours:.1f}h old).")
        return songs
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Could not read liked songs cache: {e}")
        return None


def save_liked_songs_cache(songs):
    try:
        with open(LIKED_SONGS_CACHE, "w", encoding="utf-8") as f:
            json.dump(songs, f)
    except OSError as e:
        log.warning(f"Could not write liked songs cache: {e}")


def already_downloaded(artist: str, album: str, track_number: int, track_name: str) -> bool:
    file_path = BASE_PATH / sanitize(artist) / sanitize(album) / f"{track_number:02d} - {sanitize(track_name)}.mp3"
    return file_path.exists()


def save_to_csv(artist: str, album: str, track_name: str, track_number: int):
    with csv_lock:
        with open(SONGS_TRACKER, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([artist, album, track_name, track_number])


def search_youtube(query: str) -> str:
    with YoutubeDL({
        "quiet": True,
        "skip_download": True,
        "js_runtimes": JS_RUNTIME_CONFIG,
        "remote_components": {"ejs:github"},
        "logger": YTDLPLogger(),
        **COOKIES_OPTS,
    }) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = info.get("entries") or []
        if not entries:
            raise ValueError(f"No results for {query}")
        return f"https://www.youtube.com/watch?v={entries[0]['id']}"


def download_song(track_name: str, artist: str, album: str, track_number: int,
                  total_tracks: int, release_date: str, cover_url: str):
    query = f"{track_name} {artist} official audio"
    youtube_url = search_youtube(query)

    artist_folder = BASE_PATH / sanitize(artist)
    album_folder = artist_folder / sanitize(album)
    album_folder.mkdir(parents=True, exist_ok=True)

    output_template = str(album_folder / f"{track_number:02d} - {sanitize(track_name)}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "js_runtimes": JS_RUNTIME_CONFIG,
        "remote_components": {"ejs:github"},
        "file_access_retries": 10,
        "sleep_interval": 1,
        "max_sleep_interval": 4,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            },
            {
                "key": "FFmpegMetadata"
            },
        ],
        "quiet": True,
        "addmetadata": True,
        "ffmpeg_postprocessor_args": ["-af", "loudnorm"],
        "logger": YTDLPLogger(),
        **COOKIES_OPTS,
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([youtube_url])
            worker_limiter.report_success()
            break
        except DownloadError as e:
            if is_rate_limit_error(e):
                worker_limiter.report_error()
            if attempt == max_attempts:
                raise
            backoff = 5 * (2 ** (attempt - 1))
            log.warning(
                f"Download failed for {track_name} - {artist} (attempt {attempt}/{max_attempts}). "
                f"Retrying in {backoff}s..."
            )
            time.sleep(backoff)

    mp3_file = album_folder / f"{track_number:02d} - {sanitize(track_name)}.mp3"

    write_metadata(
        mp3_file,
        track_name,
        artist,
        album,
        track_number,
        total_tracks,
        release_date,
        cover_url,
    )


def write_metadata(filename: Path, title: str, artist: str, album: str, track_number: int,
                   total_tracks: int, release_date: str, cover_url: str):
    try:
        tags = ID3(filename)
    except ID3NoHeaderError:
        tags = ID3()

    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TALB(encoding=3, text=album))
    tags.add(TRCK(encoding=3, text=f"{track_number}/{total_tracks}"))
    tags.add(TDRC(encoding=3, text=release_date))

    if cover_url:
        try:
            cover_image = requests.get(cover_url, timeout=20).content
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_image))
        except requests.RequestException as e:
            log.warning(f"Could not fetch cover art for {title}: {e}")

    tags.save(filename)


def listen_for_quit():
    while not stop_event.is_set():
        if msvcrt.kbhit():
            key = msvcrt.getch()
            if key.lower() == b"q":
                log.info("Stop requested -- finishing in-progress downloads, then exiting...")
                stop_event.set()
                break
        time.sleep(0.1)


def process_song(song: dict):
    artist = song["artist"]
    album = song["album"]
    track_name = song["title"]
    track_number = song["track_number"]
    total_tracks = song["total_tracks"]
    release_date = song["release_date"]
    cover_url = song["cover_url"]

    if stop_event.is_set():
        skipped_count.increment()
        return

    if already_downloaded(artist, album, track_number, track_name):
        log.info(f"Skipping (already exists): \"{track_name}\" by \"{artist}\"")
        skipped_count.increment()
        return

    worker_limiter.acquire()
    try:
        if stop_event.is_set():
            skipped_count.increment()
            return

        log.info(f"Downloading: \"{track_name}\" by \"{artist}\"")
        start_time = time.time()
        download_song(track_name, artist, album, track_number, total_tracks, release_date, cover_url)
        save_to_csv(artist, album, track_name, track_number)
        download_durations.add(time.time() - start_time)
        succeeded_count.increment()
        log.info(f"Finished: \"{track_name}\" by \"{artist}\"")
    finally:
        worker_limiter.release()
        status_line.update(render_status_text())


def main():
    global total_to_download
    force_refresh = "--refresh" in sys.argv

    liked_songs = None if force_refresh else load_cached_liked_songs()
    if liked_songs is None:
        liked_songs = get_liked_songs()
        save_liked_songs_cache(liked_songs)

    total_to_download = sum(
        1 for s in liked_songs
        if not already_downloaded(s["artist"], s["album"], s["track_number"], s["title"])
    )

    log.info(f"Found {len(liked_songs)} liked songs ({total_to_download} to download).")
    log.info(f"Starting with {worker_limiter.limit} concurrent downloads (adaptive, {MIN_WORKERS}-{MAX_WORKERS}).")
    log.info("Press 'q' at any time to stop -- in-progress downloads will finish first.")

    listener = threading.Thread(target=listen_for_quit, daemon=True, name="QuitListener")
    listener.start()

    ticker = threading.Thread(target=status_ticker, daemon=True, name="StatusTicker")
    ticker.start()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_song, song): song for song in liked_songs}

        for future in as_completed(futures):
            song = futures[future]
            try:
                future.result()
            except Exception as e:
                failed_count.increment()
                log.error(
                    f"Failed: \"{song['title']}\" by \"{song['artist']}\" -> {e}"
                )
                status_line.update(render_status_text())

    was_stopped_by_user = stop_event.is_set()
    stop_event.set()
    all_done_event.set()
    status_line.clear()

    log.info("Stopped early by user." if was_stopped_by_user else "All downloads complete.")
    log.info(
        f"Session summary -- succeeded: {succeeded_count.value}, "
        f"failed: {failed_count.value}, skipped: {skipped_count.value}"
    )


if __name__ == "__main__":
    main()