# SpotiFlopy

> **This is a fork** of the original [SpotiFlopy](https://github.com/aneeb02/SpotiFlopy) project. The script in this repo has been substantially rewritten with the help of LLMs.
> The majority of this fork was AI-generated based on the original project's concept and code. I do not recommend this code as an example, template, or to learn from, it is messy. 

Automatically download all your liked songs on Spotify, without paying a dime.

---

## Features 🚀

- **Fetches your Spotify Liked Songs** and downloads matching audio from YouTube via `yt-dlp`.
- **Adaptive concurrent downloading**: runs multiple downloads in parallel, automatically shrinking concurrency when it hits rate-limit errors (403/429) and growing it back after a
  streak of clean downloads.
- **Automatic retries**: transient download failures are retried with exponential backoff before being logged as a real failure.
- **Live progress tracking**: a persistent status line at the bottom of the terminal shows songs downloaded, failed, remaining, and an estimated time remaining — updated in real
  time without disrupting the scrolling log above it.
- **Graceful stop**: press `q` at any time to stop after in-progress downloads finish, instead of killing the process mid-download.
- **Liked-songs caching**: avoids re-hitting Spotify's API (and its rate limits) on every run; force a refresh with `--refresh` when you want to pick up newly liked songs.
- **Clean, structured logging**: all output — including yt-dlp's own internal messages — goes through one consistent, timestamped logger. Quiet by default; full yt-dlp verbosity
  available with `--verbose`.
- **Audio normalization & metadata**: embeds cover art, ID3 tags, and normalizes volume via `ffmpeg`.
- **Organized storage**: MP3s are stored in `<Project Root>/Songs/<Artist>/<Album>/` and tracked in `<Project Root>/Songs/songs.csv`.
- **Configurable JS runtime & cookies**: works with Deno, Node, or Bun for solving YouTube's JS challenges, and optionally supports pulling YouTube cookies from a browser to reduce
  rate-limiting.

---

## How It Works ⚙️

1. The script authenticates with the Spotify API and fetches your Liked Songs (or loads them from a local cache, if recent enough).
2. Each song is checked against what's already downloaded on disk — already-downloaded songs are skipped.
3. New songs are searched on YouTube and downloaded concurrently as MP3s via `yt-dlp`, with concurrency automatically tuned based on how YouTube responds.
4. Downloaded songs are tagged, normalized, and saved into `<Project Root>/Songs/<Artist>/<Album>/`, with progress tracked in `<Project Root>/Songs/songs.csv`.
5. A live status line shows overall progress and an ETA; a session summary prints when the run finishes (or you stop it early with `q`).

---

## Installation Instructions 🔧

### 1. Clone the Repository

```bash
git clone https://github.com/aneeb02/SpotiFlopy.git
cd SpotiFlopy
```

### 2. Install Dependencies

Make sure you have Python 3 installed, then install the required packages:

```bash
pip install -r requirements.txt
```

**Important:** you also need `ffmpeg` installed and available on your system PATH — it's required for audio extraction, volume normalization, and metadata embedding.

You'll also need a JS runtime for `yt-dlp` to solve YouTube's JS challenges — [Deno](https://deno.land) is recommended, though Node.js or Bun also work (see `JS_RUNTIME_NAME`
below).

### 3. Set Up Environment Variables

Create a `.env` file in the project root. For Spotify, create an app to get a client ID and secret at [developer.spotify.com](https://developer.spotify.com/).

**Required:**

```env
SPOTIPY_CLIENT_ID=your_spotify_client_id
SPOTIPY_CLIENT_SECRET=your_spotify_client_secret
SPOTIPY_REDIRECT_URI=http://localhost:8888/callback/
```

**Optional (with defaults):**

```env
# JS runtime yt-dlp uses to solve YouTube's JS challenges
JS_RUNTIME_NAME=deno
JS_RUNTIME_PATH=C:\path\to\deno.exe

# Concurrency (adaptive -- grows/shrinks automatically within this range)
MIN_WORKERS=1
MAX_WORKERS=6
INITIAL_WORKERS=3

# How long to trust the cached liked-songs list before re-fetching from Spotify
LIKED_SONGS_CACHE_HOURS=24

# Optional: pull YouTube cookies from a browser to help with rate limiting.
# Close the browser fully before running the script if you set this.
# Note: recent Chrome versions on Windows use App-Bound Encryption, which
# blocks cookie extraction entirely -- use Firefox if you hit that.
BROWSER_COOKIES=firefox
BROWSER_PROFILE=default-release

# Show full yt-dlp debug/warning output instead of the quiet default
VERBOSE=false
```

### 4. Run the Script

```bash
python spotify_downloader.py
```

The first run opens a browser for Spotify authentication; after that, token refreshes happen automatically.

**Flags:**
| Flag | Description |
|---|---|
| `--refresh` | Force a fresh fetch of your liked songs from Spotify, bypassing the local cache. |
| `--verbose` | Show yt-dlp's full internal logging (useful for debugging download issues). |

**While running:** press `q` at any time to stop -- any downloads already in progress will finish before the script exits.

---

## Changes From the Original Script

This fork started from the original SpotiFlopy concept and was substantially reworked. Highlights:

- **Fixed a data bug** where song metadata was being read incorrectly due to a dict/tuple mismatch, silently corrupting every download's artist/album/track info.
- **Fixed several crash-on-edge-case bugs**: missing album art, a `Path` vs `str` type error on startup, and duplicate/conflicting ID3 thumbnail embedding.
- **Replaced ad-hoc `print()` output** with structured, timestamped logging -- including routing yt-dlp's own internal messages through the same logger instead of letting them
  print raw to the console.
- **Added adaptive concurrency control**, replacing a fixed thread count with a self-tuning limiter that backs off on rate-limit errors and recovers gradually.
- **Added automatic retries** with exponential backoff for transient download failures.
- **Added a liked-songs cache** to cut down on Spotify API calls (and the very long rate-limit lockouts that can follow from exceeding them).
- **Added graceful shutdown** (`q` to stop cleanly) instead of requiring a hard kill.
- **Added a live progress/ETA status line** at the bottom of the terminal.
- **Generalized the JS runtime configuration** (Deno/Node/Bun, configurable path) instead of hardcoding Deno.
- **Added optional browser-cookie support** to help with YouTube rate limiting.
- **Reorganized file output** under a `Songs/` subfolder, and added `--refresh` and `--verbose` flags for more control over runtime behavior.
- **Collaboration songs** are now filed under the primary artist's folder instead of creating a separate folder per unique artist combination.

---

## Known Limitations

- YouTube's anti-bot measures (JS challenges, PO tokens, rate limiting) are a moving target -- occasional 403s are expected even with everything configured correctly.
- Browser cookie extraction doesn't work with recent Chrome versions on Windows due to Chrome's App-Bound Encryption; use Firefox instead if you need this feature.
- This script depends on yt-dlp's ability to keep up with YouTube's changes -- keep `yt-dlp` and `yt-dlp-ejs` updated together, as they're version-coupled.