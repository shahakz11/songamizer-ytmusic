from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pymongo import MongoClient
import aiohttp
import random
from datetime import datetime, timedelta
from bson import ObjectId
import logging
import os
import urllib.parse
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

# Spotify OAuth settings
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")

@app.get("/api/spotify/authorize")
async def spotify_authorize():
    if not CLIENT_ID or not REDIRECT_URI:
        logger.error("Spotify CLIENT_ID or REDIRECT_URI missing")
        raise HTTPException(status_code=500, detail="Server configuration error")
    
    session_id = str(ObjectId())
    await db.sessions.insert_one({
        "_id": ObjectId(session_id),
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
    return RedirectResponse(auth_url)

@app.get("/api/spotify/callback")
async def spotify_callback(code: str = Query(...), state: str = Query(...)):
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        logger.error("Spotify credentials missing")
        raise HTTPException(status_code=500, detail="Server configuration error")
    
    session = await db.sessions.find_one({"_id": ObjectId(state)})
    if not session or not session.get("is_active"):
        logger.error(f"Invalid session: {state}")
        raise HTTPException(status_code=400, detail="Invalid session")
    
    async with aiohttp.ClientSession() as http_session:
        async with http_session.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        ) as response:
            data = await response.json()
            if response.status != 200:
                logger.error(f"Failed to get Spotify token: {data}")
                raise HTTPException(status_code=400, detail="Failed to authenticate with Spotify")
    
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in", 3600)
    
    await db.sessions.update_one(
        {"_id": ObjectId(state)},
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
    return RedirectResponse(redirect_url)

@app.get("/api/spotify/playlists")
async def get_playlists(session_id: str = Query(...)):
    session = await db.sessions.find_one({"_id": ObjectId(session_id)})
    if not session or not session.get("is_active"):
        logger.error(f"Invalid session: {session_id}")
        raise HTTPException(status_code=400, detail="Invalid session")
    
    access_token = session.get("spotify_access_token")
    if datetime.utcnow() >= session.get("token_expires_at"):
        access_token = await refresh_access_token(session.get("spotify_refresh_token"), session_id)
    
    async with aiohttp.ClientSession() as http_session:
        async with http_session.get(
            "https://api.spotify.com/v1/me/playlists",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as response:
            data = await response.json()
            if response.status != 200:
                logger.error(f"Failed to fetch playlists: {data}")
                raise HTTPException(status_code=400, detail="Failed to fetch playlists")
    
    playlists = [
        {"id": item["id"], "name": item["name"]} for item in data.get("items", [])
    ]
    return {"playlists": playlists}

async def refresh_access_token(refresh_token: str, session_id: str):
    async with aiohttp.ClientSession() as http_session:
        async with http_session.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
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

@app.get("/api/spotify/play-track")
async def play_track(playlist_id: str = Query(...), session_id: str = Query(...)):
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
            {"$push": {"tracks_played": []}},
        )
        logger.info(f"Played track {track['id']} for session {session_id}, modified: {result}")
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