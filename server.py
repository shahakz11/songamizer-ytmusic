from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
import os
from dotenv import load_dotenv
import logging

# Load environment variables
load_dotenv()
app = Flask(__name__)
CORS(app)

# MongoDB setup
mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
client = MongoClient(mongo_uri)
db = client["hitster"]
sessions = db["sessions"]
tracks = db["tracks"]
playlists = db["playlists"]
playlist_tracks = db["playlist_tracks"]
track_metadata = db["track_metadata"]

# Ensure indexes
tracks.create_index("expires_at", expireAfterSeconds=7200, partialFilterExpression={"expires_at": {"$exists": True}})
playlist_tracks.create_index("playlist_id", unique=True)
track_metadata.create_index([("track_name", 1), ("artist_name", 1)], unique=True)

# Spotify configuration (existing)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "https://hitster-randomizer.onrender.com/api/spotify/callback")

# YouTube Music configuration (unofficial API placeholder)
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")  # To be updated with unofficial API key
import youtube_dl  # Example library for unofficial access

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

@app.route('/api/select-service', methods=['POST'])
def select_service():
    data = request.get_json()
    service = data.get('service')
    if service not in ["spotify", "youtube"]:
        return jsonify({"error": "Invalid service"}), 400
    session_id = request.headers.get('X-Session-ID')
    if not session_id:
        session_id = os.urandom(16).hex()
    sessions.update_one(
        {"session_id": session_id},
        {"$set": {"service": service, "session_id": session_id, "is_active": True}},
        upsert=True
    )
    return jsonify({"session_id": session_id, "service": service})

@app.route('/api/spotify/authorize')
def spotify_authorize():
    if not sessions.find_one({"session_id": request.headers.get('X-Session-ID'), "service": "spotify"}):
        return jsonify({"error": "Spotify service not selected"}), 403
    auth_url = f"https://accounts.spotify.com/authorize?client_id={SPOTIFY_CLIENT_ID}&response_type=code&redirect_uri={REDIRECT_URI}&scope=user-read-playback-state user-modify-playback-state playlist-read-private"
    return jsonify({"auth_url": auth_url})

@app.route('/api/spotify/callback')
def spotify_callback():
    # Existing Spotify callback logic
    code = request.args.get('code')
    # ... (implement token exchange and session update)
    return jsonify({"status": "success"})

@app.route('/api/youtube/authorize')
def youtube_authorize():
    if not sessions.find_one({"session_id": request.headers.get('X-Session-ID'), "service": "youtube"}):
        return jsonify({"error": "YouTube service not selected"}), 403
    # Unofficial API auth (placeholder)
    if not YOUTUBE_API_KEY:
        return jsonify({"error": "YouTube API key not configured"}), 500
    # Use youtube_dl or similar to authenticate
    return jsonify({"status": "authenticated"})  # Placeholder

@app.route('/api/play-track/<playlist_id>')
def play_track(playlist_id):
    session_id = request.headers.get('X-Session-ID')
    session = sessions.find_one({"session_id": session_id})
    if not session:
        return jsonify({"error": "Session not found"}), 404
    service = session.get("service", "spotify")
    if service == "spotify":
        # Existing Spotify play logic
        pass
    elif service == "youtube":
        # Unofficial YouTube play logic using youtube_dl
        with youtube_dl.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(f"ytsearch:{playlist_id}", download=False)['entries'][0]
            return jsonify({"url": info['url']})
    return jsonify({"error": "Service not supported"}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))