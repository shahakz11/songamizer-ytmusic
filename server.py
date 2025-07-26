from fastapi import FastAPI, HTTPException, Query
from pymongo import MongoClient
import aiohttp
import random
from datetime import datetime, timedelta
from bson import ObjectId
import logging
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = FastAPI()
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

# ... (rest of the server.py code, including /api/spotify/play-track, remains unchanged)
async def refresh_access_token(refresh_token: str, session_id: str):
    async with aiohttp.ClientSession() as http_session:
        async with http_session.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": os.getenv("SPOTIFY_CLIENT_ID"),
                "client_secret": os.getenv("SPOTIFY_CLIENT_SECRET"),
            },
        ) as response:
            data = await response.json()
            if response.status != 200:
                logger.error(f"Failed to refresh token: {data}")
                raise HTTPException(status_code=400, detail="Failed to refresh Spotify token")
            access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            await db.sessions.update_one(
                {"_id": ObjectId(session_id)},
                {
                    "$set": {
                        "spotify_access_token": access_token,
                        "token_expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
                    }
                },
            )
            return access_token

@app.get("/api/spotify/play-track/<playlist_id>")
async def play_track(playlist_id: str, session_id: str = Query(...)):
    logger.info(f"Received play-track request for playlist {playlist_id}, session {session_id}")
    session = await db.sessions.find_one({"_id": ObjectId(session_id)})
    if not session or not session.get("is_active"):
        logger.error(f"Invalid or inactive session: {session_id}")
        raise HTTPException(400, "Invalid or inactive session")
    
    access_token = session.get("spotify_access_token")
    if datetime.utcnow() >= session.get("token_expires_at"):
        access_token = await refresh_access_token(session.get("spotify_refresh_token"), session_id)
    
    tracks = []
    offset = 0
    while True:
        async with aiohttp.ClientSession() as http_session:
            async with http_session.get(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"limit": 50, "offset": offset},
            ) as response:
                data = await response.json()
                tracks.extend(data.get("items", []))
                if not data.get("next"):
                    break
                offset += 50
    
    tracks = [
        t["track"] for t in tracks if t.get("track") and not t["track"].get("is_local")
    ]
    if not tracks:
        logger.error(f"No playable tracks found for playlist {playlist_id}")
        raise HTTPException(400, "No playable tracks found")
    
    tracks_played = session.get("tracks_played", [])
    available_tracks = [t for t in tracks if t["id"] not in tracks_played]
    if not available_tracks:
        await db.sessions.update_one(
            {"_id": ObjectId(session_id)}, {"$set": {"tracks_played": []}}
        )
        available_tracks = tracks
    
    track = random.choice(available_tracks)
    async with aiohttp.ClientSession() as http_session:
        async with http_session.get(
            "https://api.spotify.com/v1/me/player/devices",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as response:
            devices = (await response.json()).get("devices", [])
            if not devices:
                logger.error(f"No active Spotify device found for session {session_id}")
                raise HTTPException(400, "No active Spotify device found")
        
        async with http_session.put(
            "https://api.spotify.com/v1/me/player/play",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"uris": [f"spotify:track:{track['id']}"]},
        ) as response:
            logger.info(f"Play request status for session {session_id}: {response.status}")
    
    try:
        await db.tracks.insert_one({
            "spotify_id": track["id"],
            "title": track["name"],
            "artist": ", ".join(a["name"] for a in track["artists"]),
            "release_year": track["album"].get("release_date", "")[:4],
            "album": track["album"]["name"],
            "playlist_theme": playlist_id,
            "played_at": datetime.utcnow().isoformat(),
            "session_id": session_id,
        })
        
        result = await db.sessions.update_one(
            {"_id": ObjectId(session_id)},
            {"$push": {"tracks_played": track["id"]}},
        )
        logger.info(f"Played track {track['id']} for session {session_id}, modified: {result.modified_count}")
    except Exception as e:
        logger.error(f"Error saving track {track['id']} for session {session_id}: {str(e)}")
        raise HTTPException(500, "Failed to save track")
    
    return {
        "spotify_id": track["id"],
        "title": track["name"],
        "artist": ", ".join(a["name"] for a in track["artists"]),
        "release_year": track["album"].get("release_date", "")[:4],
        "album": track["album"]["name"],
        "playlist_theme": playlist_id,
    }