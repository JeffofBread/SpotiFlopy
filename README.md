# SpotiFlopy
Automatically download all your liked songs on Spotify, without paying a dime.

## **Features** 🚀
- Automatically fetches and downloads your Spotify Liked Songs.
- Downloads audio from YouTube using `yt-dlp`.
- **Fast, Concurrent Downloading:** Uses multi-threading to download up to 8 songs simultaneously.
- **Audio Normalization & Metadata:** Embeds thumbnails, metadata, and normalizes audio volume using `ffmpeg`.
- **Organized Storage:** Stores downloaded MP3s logically in a `Songs/Artist/Album/` folder structure on your desktop, and tracks progress in a `songs.csv` file.

---

## **How It Works** ⚙️
1. The script uses the Spotify API to get all your Liked Songs.
2. It checks against `songs.csv` and existing files to prevent re-downloading.
3. New songs are searched on YouTube and downloaded concurrently in MP3 format using `yt-dlp`.
4. The downloaded songs are saved, normalized, and tagged before being placed in the **Songs** folder on your desktop.

---

## **Installation Instructions** 🔧

### 1. Clone the Repository:
```bash
git clone https://github.com/aneeb02/SpotiFlopy.git
cd SpotiFlopy
```

### 2. Install Dependencies:
Make sure you have Python 3 installed. Then install the required packages:
```bash
pip install -r requirements.txt
```
**Important:** You also need to install `ffmpeg` on your system and ensure it's available in your system's PATH, as it is required for audio extraction, volume normalization, and metadata embedding.

### 3. Set Up Spotify API Credentials:
1. Create a `.env` file in the root directory of the project.
2. Add your Spotify API credentials in the `.env` file:

```env
SPOTIPY_CLIENT_ID=your_spotify_client_id
SPOTIPY_CLIENT_SECRET=your_spotify_client_secret
SPOTIPY_REDIRECT_URI=http://localhost:8888/callback/
```
### 4. Run the script
```bash
python main.py
```

The first time you run the script, it will open a browser for you to authenticate with Spotify. After that, it will handle token refreshes automatically.

