from flask import Flask, redirect, request, jsonify, abort
from flask_cors import CORS
from pymongo import MongoClient
import requests
import random
from datetime import datetime, timedelta
from bson import ObjectId
import logging
import os
import urllib.parse
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app)  # Enable CORS for frontend
logger = logging.getLogger("server")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
logger.addHandler(handler)

# Initialize MongoDB client
mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    logger.error("MONGO_URI environment variable is missing")
    raise RuntimeError("MONGO_URI environment variable is missing")
try:
    client = MongoClient(mongo_uri)
    db = client['hitster']
    logger.info("Connected to MongoDB")
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {str(e)}")
    raise RuntimeError(f"Failed to connect to MongoDB: {str(e)}")

# Spotify OAuth settings
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")

@app.route("/api/spotify/authorize")
def spotify_authorize():
    if not CLIENT_ID or not REDIRECT_URI:
        logger.error("Spotify CLIENT_ID or REDIRECT_URI missing")
        abort(500, description="Server configuration error")
    
    session_id = str(ObjectId())
    db.sessions.insert_one({
        "_id": session_id,
        "is_active": True,
        "created_at": datetime.utcnow(),
    })
    
    scope = "user-read-private user-read-email playlist-read-private streaming user-modify-playback-state"
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "state": session_id,
    }
    auth_url = f"{SPOTIFY_AUTH_URL}?{urllib.parse.urlencode(params)}"
    logger.info(f"Redirecting to Spotify auth: {auth_url}")
    return redirect(auth_url)

@app.route("/api/spotify/callback")
def spotify_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        logger.error("Missing code or state in callback")
        abort(400, description="Missing required parameters")
    
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        logger.error("Spotify credentials missing")
        abort(500, description="Server configuration error")
    
    session = db.sessions.find_one({"_id": state})
    if not session or not session.get("is_active"):
        logger.error(f"Invalid session: {state}")
        abort(400, description="Invalid session")
    
    response = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    data = response.json()
    if response.status_code != 200:
        logger.error(f"Failed to get Spotify token: {data}")
        abort(400, description="Failed to authenticate with Spotify")
    
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in", 3600)
    
    db.sessions.update_one(
        {"_id": state},
        {
            "$set": {
                "spotify_access_token": access_token,
                "spotify_refresh_token": refresh_token,
                "token_expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
            }
        },
    )
    
    redirect_url = f"https://preview--tune-twist-7ca04c74.base44.app/?session_id={state}"
    logger.info(f"Redirecting to frontend: {redirect_url}")
    return redirect(redirect_url)

@app.route("/api/spotify/playlists")
def get_playlists():
    session_id = request.args.get("session_id")
    if not session_id:
        logger.error("Missing session_id")
        abort(400, description="Missing session_id")
    
    session = db.sessions.find_one({"_id": session_id})
    if not session or not session.get("is_active"):
        logger.error(f"Invalid session: {session_id}")
        abort(400, description="Invalid session")
    
    access_token = session.get("spotify_access_token")
    if datetime.utcnow() >= session.get("token_expires_at"):
        access_token = refresh_access_token(session.get("spotify_refresh_token"), session_id)
    
    response = requests.get(
        "https://api.spotify.com/v1/me/playlists",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    data = response.json()
    if response.status_code != 200:
        logger.error(f"Failed to fetch playlists: {data}")
        abort(400, description="Failed to fetch playlists")
    
    playlists = [
        {"id": item["id"], "name": item["name"]} for item in data.get("items", [])
    ]
    return jsonify({"playlists": playlists})

def refresh_access_token(refresh_token, session_id):
    response = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    data = response.json()
    if response.status_code != 200:
        logger.error(f"Failed to refresh token: {data}")
        abort(400, description="Failed to refresh Spotify token")
    
    access_token = data.get("access_token")
    expires_in = data.get("expires_in", 3600)
    db.sessions.update_one(
        {"_id": session_id},
        {
            "$set": {
                "spotify_access_token": access_token,
                "token_expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
            }
        },
    )
    return access_token

@app.route("/api/spotify/play-track")
def play_track():
    playlist_id = request.args.get("playlist_id")
    session_id = request.args.get("session_id")
    if not playlist_id or not session_id:
        logger.error("Missing playlist_id or session_id")
        abort(400, description="Missing required parameters")
    
    logger.info(f"Received play-track request for playlist {playlist_id}, session {session_id}")
    session = db.sessions.find_one({"_id": session_id})
    if not session or not session.get("is_active"):
        logger.error(f"Invalid or inactive session: {session_id}")
        abort(400, description="Invalid or inactive session")
    
    access_token = session.get("spotify_access_token")
    if datetime.utcnow() >= session.get("token_expires_at"):
        access_token = refresh_access_token(session.get("spotify_refresh_token"), session_id)
    
    tracks = []
    offset = 0
    while True:
        response = requests.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"limit": 50, "offset": offset},
        )
        data = response.json()
        tracks.extend(data.get("items", []))
        if not data.get("next"):
            break
        offset += 50
    
    tracks = [
        t["track"] for t in tracks if t.get("track") and not t["track"].get("is_local")
    ]
    if not tracks:
        logger.error(f"No playable tracks found for playlist {playlist_id}")
        abort(400, description="No playable tracks found")
    
    tracks_played = session.get("tracks_played", [])
    available_tracks = [t for t in tracks if t["id"] not in tracks_played]
    if not available_tracks:
        db.sessions.update_one(
            {"_id": session_id}, {"$set": {"tracks_played": []}}
        )
        available_tracks = tracks
    
    track = random.choice(available_tracks)
    response = requests.get(
        "https://api.spotify.com/v1/me/player/devices",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    devices = response.json().get("devices", [])
    if not devices:
        logger.error(f"No active Spotify device found for session {session_id}")
        abort(400, description="No active Spotify device found")
    
    response = requests.put(
        "https://api.spotify.com/v1/me/player/play",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"uris": [f"spotify:track:{track['id']}"]},
    )
    logger.info(f"Play request status for session {session_id}: {response.status_code}")
    
    try:
        db.tracks.insert_one({
            "spotify_id": track["id"],
            "title": track["name"],
            "artist": ", ".join(a["name"] for a in track["artists"]),
            "release_year": track["album"].get("release_date", "")[:4],
            "album": track["album"]["name"],
            "playlist_theme": playlist_id,
            "played_at": datetime.utcnow().isoformat(),
            "session_id": session_id,
        })
        
        result = db.sessions.update_one(
            {"_id": session_id},
            {"$push": {"tracks_played": track["id"]}},
        )
        logger.info(f"Played track {track['id']} for session {session_id}, modified: {result.modified_count}")
    except Exception as e:
        logger.error(f"Error saving track {track['id']} for session {session_id}: {str(e)}")
        abort(500, description="Failed to save track")
    
    return jsonify({
        "spotify_id": track["id"],
        "title": track["name"],
        "artist": ", ".join(a["name"] for a in track["artists"]),
        "release_year": track["album"].get("release_date", "")[:4],
        "album": track["album"]["name"],
        "playlist_theme": playlist_id,
    })

if __name__ == "__main__":
    app.run(debug=True)