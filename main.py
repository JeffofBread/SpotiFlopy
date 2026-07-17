import os
import csv
import logging
import msvcrt
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from yt_dlp import YoutubeDL
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK, TDRC, ID3NoHeaderError

# -------------------------
# Logging (thread-safe, avoids interleaved prints)
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("spotify_downloader")

# -------------------------
# Spotify Auth
# -------------------------
scope = "user-library-read"

load_dotenv()
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.getenv("SPOTIPY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
    scope=scope
))

# -------------------------
# Paths
# -------------------------
BASE_PATH = Path(os.path.dirname(os.path.abspath(__file__))) / "Songs"
BASE_PATH.mkdir(parents=True, exist_ok=True)
DENO_PATH = os.getenv("DENO_PATH")

if not DENO_PATH:
    raise EnvironmentError(
        "DENO_PATH is not set. Add it to your .env file\n"
    )

SONGS_TRACKER = BASE_PATH / "songs.csv"
if not SONGS_TRACKER.exists():
    with open(SONGS_TRACKER, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Artist", "Album", "Track", "Track Number"])  # header


# -------------------------
# Helpers
# -------------------------

def sanitize(text: str) -> str:
    cleaned = "".join(c for c in text if c not in r'\/:*?"<>|').strip()
    return cleaned.rstrip(". ")


def search_youtube(query: str) -> str:
    with YoutubeDL({
        "quiet": True,
        "skip_download": True,
        "js_runtimes": {"deno": {"path": DENO_PATH}},
        "remote_components": {"ejs:github"},
    }) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        # FIX: info["entries"] can be None (not just empty) on a failed search.
        entries = info.get("entries") or []
        if not entries:
            raise ValueError(f"No results for {query}")
        return f"https://www.youtube.com/watch?v={entries[0]['id']}"


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


def already_downloaded(artist: str, album: str, track_number: int, track_name: str) -> bool:
    file_path = BASE_PATH / sanitize(artist) / sanitize(album) / f"{track_number:02d} - {sanitize(track_name)}.mp3"
    return file_path.exists()


csv_lock = threading.Lock()


def save_to_csv(artist: str, album: str, track_name: str, track_number: int):
    with csv_lock:
        with open(SONGS_TRACKER, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([artist, album, track_name, track_number])


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
        "js_runtimes": {"deno": {"path": DENO_PATH}},
        "remote_components": {"ejs:github"},
        "file_access_retries": 10,
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
    }

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([youtube_url])

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


# -------------------------
# Main
# -------------------------

MAX_WORKERS = 3
stop_event = threading.Event()


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
        log.info(f"Skipping (stop requested): {track_number:02d} - {track_name} - {artist}")
        return

    if already_downloaded(artist, album, track_number, track_name):
        log.info(f"Skipping (already exists): {track_number:02d} - {track_name} - {artist}")
        return

    log.info(f"Downloading: {track_number:02d} - {track_name} - {artist}")
    download_song(track_name, artist, album, track_number, total_tracks, release_date, cover_url)
    save_to_csv(artist, album, track_name, track_number)


def main():
    liked_songs = get_liked_songs()
    log.info(f"Found {len(liked_songs)} liked songs.")
    log.info("Press 'q' at any time to stop -- in-progress downloads will finish first.")

    listener = threading.Thread(target=listen_for_quit, daemon=True, name="QuitListener")
    listener.start()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_song, song): song for song in liked_songs}

        for future in as_completed(futures):
            song = futures[future]
            try:
                future.result()
            except Exception as e:
                log.error(
                    f"Failed: {song['track_number']:02d} - {song['title']} - {song['artist']} -> {e}"
                )

    was_stopped_by_user = stop_event.is_set()
    stop_event.set()

    log.info("Stopped early by user." if was_stopped_by_user else "All downloads complete.")


if __name__ == "__main__":
    main()