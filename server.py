import os
import re
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_cors import CORS
import requests
import random
from urllib.parse import urlencode
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import logging
from dotenv import load_dotenv
from ytmusicapi import YTMusic
import time
import json
import secrets

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-here')
CORS(app, resources={r"/api/*": {"origins": ["https://claude.ai/public/artifacts/3a78fe49-3a6d-463a-b3c0-49b13b2130fd", "*"]}})

# Configuration
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI')
FRONTEND_URL = os.getenv('FRONTEND_URL', 'https://claude.ai/public/artifacts/3a78fe49-3a6d-463a-b3c0-49b13b2130fd')
MONGO_URI = os.getenv('MONGO_URI')

# Spotify API credentials
SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')
SPOTIFY_REDIRECT_URI = os.environ.get('SPOTIFY_REDIRECT_URI', 'http://localhost:5000/callback')

if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, MONGO_URI]):
    missing = [k for k, v in {
        'SPOTIFY_CLIENT_ID': CLIENT_ID,
        'SPOTIFY_CLIENT_SECRET': CLIENT_SECRET,
        'SPOTIFY_REDIRECT_URI': REDIRECT_URI,
        'MONGO_URI': MONGO_URI
    }.items() if not v]
    logger.error(f"Missing environment variables: {missing}")
    raise ValueError(f"Missing environment variables: {missing}")
logger.info(f"Environment: REDIRECT_URI={REDIRECT_URI}, FRONTEND_URL={FRONTEND_URL}")

# Valid icon names for playlists
VALID_ICONS = [
    'jukebox', 'boombox', 'microphone', 'bells',
    'music-note', 'record-player', 'guitar', 'headphones'
]
DEFAULT_ICON = 'music-note'

# Initialize YouTube Music
ytmusic = YTMusic()

# In-memory storage (replace with database in production)
user_sessions = {}
playlists_data = {}

# MongoDB setup
try:
    mongodb = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=30000,
        connectTimeoutMS=30000,
        socketTimeoutMS=30000,
        retryWrites=True,
        w='majority'
    )
    db = mongodb['songamizer']
    sessions = db['sessions']
    tracks = db['tracks']
    playlists = db['playlists']
    playlist_tracks = db['playlist_tracks']
    track_metadata = db['track_metadata']
    mongodb.admin.command('ping')  # Test connection
    # Create TTL index on tracks.expires_at for 2-hour expiry
    tracks.create_index(
        [("expires_at", 1)],
        expireAfterSeconds=7200,
        partialFilterExpression={"expires_at": {"$exists": True}}
    )
    playlist_tracks.create_index([("playlist_id", 1)], unique=True)
    track_metadata.create_index([("track_name", 1), ("artist_name", 1)], unique=True)
    logger.info("MongoDB connected successfully and indexes ensured")
except Exception as e:
    logger.error(f"MongoDB connection or index creation failed: {e}")
    raise

# Get Client Credentials access token
def get_client_credentials_token():
    try:
        response = requests.post(
            'https://accounts.spotify.com/api/token',
            data={'grant_type': 'client_credentials', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}
        )
        response.raise_for_status()
        return response.json().get('access_token')
    except requests.RequestException as e:
        logger.error(f"Error getting client credentials token: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return None

# Refresh Authorization Code access token
def refresh_access_token(session_id):
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session or not session.get('spotify_refresh_token'):
            logger.error(f"No refresh token for session {session_id}")
            return False
        response = requests.post(
            'https://accounts.spotify.com/api/token',
            data={
                'grant_type': 'refresh_token',
                'refresh_token': session['spotify_refresh_token'],
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET
            }
        )
        response.raise_for_status()
        data = response.json()
        expires_in = data.get('expires_in', 3600)
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {
                'spotify_access_token': data.get('access_token'),
                'token_expires_at': datetime.utcnow() + timedelta(seconds=expires_in)
            }}
        )
        logger.info(f"Refreshed access token for session {session_id}, modified: {result.modified_count}")
        return True
    except requests.RequestException as e:
        logger.error(f"Error refreshing access token for {session_id}: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error in refresh_access_token for {session_id}: {e}")
        return False

# Fetch original release year from MusicBrainz
def get_original_release_year(track_name, artist_name, album_name, fallback_year):
    time.sleep(1)  # Respect MusicBrainz rate limit
    if fallback_year and (1900 <= fallback_year <= datetime.utcnow().year):
        logger.info(f"Using valid fallback year {fallback_year} for {track_name} by {artist_name}")
        return fallback_year
    cached = track_metadata.find_one({'track_name': track_name, 'artist_name': artist_name})
    if cached and cached.get('expires_at') > datetime.utcnow():
        logger.info(f"Using cached original year for {track_name} by {artist_name}: {cached['original_year']}")
        return cached['original_year']
    try:
        query = f'release:"{album_name}" AND artist:"{artist_name}"'
        response = requests.get(
            f'https://musicbrainz.org/ws/2/release?query={urlencode({"query": query})}&fmt=json',
            headers={'User-Agent': 'Songamizer/1.0 ( https://songamizer-ytmusic-backend.onrender.com )'}
        )
        response.raise_for_status()
        data = response.json()
        logger.debug(f"MusicBrainz response for {track_name} by {artist_name}: {data}")
        earliest_year = datetime.utcnow().year
        found_valid_year = False
        for release in data.get('releases', []):
            if 'date' in release and release['date']:
                try:
                    year = int(release['date'].split('-')[0])
                    if 1900 <= year <= datetime.utcnow().year:
                        if year < earliest_year or not found_valid_year:
                            earliest_year = year
                            found_valid_year = True
                except ValueError:
                    logger.warning(f"Invalid date format in release: {release['date']}")
                    continue
        if found_valid_year:
            track_metadata.update_one(
                {'track_name': track_name, 'artist_name': artist_name},
                {'$set': {
                    'original_year': earliest_year,
                    'expires_at': datetime.utcnow() + timedelta(days=30)
                }},
                upsert=True
            )
            return earliest_year
        logger.warning(f"No valid MusicBrainz year for {track_name}, using current year")
        return datetime.utcnow().year
    except Exception as e:
        logger.error(f"MusicBrainz error for {track_name}: {e}")
        return datetime.utcnow().year

# Get playlist tracks from Spotify
def get_playlist_tracks(playlist_id, session_id):
    cache = playlist_tracks.find_one({'playlist_id': playlist_id})
    if cache and cache.get('cached_at') and (datetime.utcnow() - cache['cached_at']).total_seconds() < 300:  # 5 minutes
        return cache['tracks']
    token = get_client_credentials_token()
    if not token:
        logger.error(f"No client credentials token for playlist {playlist_id}")
        return []
    tracks = []
    offset = 0
    limit = 50
    while True:
        response = requests.get(
            f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit={limit}&offset={offset}',
            headers={'Authorization': f'Bearer {token}'}
        )
        if response.status_code == 401:
            token = get_client_credentials_token()
            if not token:
                logger.error(f"Failed to refresh client credentials token for playlist {playlist_id}")
                return []
            response = requests.get(
                f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit={limit}&offset={offset}',
                headers={'Authorization': f'Bearer {token}'}
            )
        response.raise_for_status()
        data = response.json()
        if 'items' not in data:
            logger.error(f"No 'items' in response for playlist {playlist_id}, Response: {data}")
            return []
        new_tracks = [item['track'] for item in data['items'] if item['track'] and item['track']['id']]
        tracks.extend(new_tracks)
        if len(data['items']) < limit:
            break
        offset += limit
    playlist_tracks.update_one(
        {'playlist_id': playlist_id},
        {'$set': {'tracks': tracks, 'cached_at': datetime.utcnow()}},
        upsert=True
    )
    return tracks

# Fetch stream URL from ytmusicapi
def get_stream_url(track_name, artist_name):
    ytmusic = YTMusic()
    try:
        search_results = ytmusic.search(f"{track_name} {artist_name}", filter="songs")
        if not search_results:
            logger.error(f"No search results for {track_name} by {artist_name}")
            return None
        video_id = search_results[0]['videoId']
        song_data = ytmusic.get_song(video_id)
        stream_url = song_data.get('streamingData', {}).get('adaptiveFormats', [{}])[0].get('url')
        if not stream_url:
            logger.error(f"No stream URL for {video_id}")
            return None
        logger.info(f"Fetched stream URL for {track_name} by {artist_name}")
        return stream_url
    except Exception as e:
        logger.error(f"ytmusicapi error for {track_name}: {e}")
        return None

# Play track (using ytmusicapi for stream URL)
def play_track(track_id, session_id):
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id: {session_id}")
            return False, "No session found"
        token = session.get('spotify_access_token')
        if not token:
            logger.error(f"No access token for session {session_id}")
            return False, "No access token available"
        response = requests.get(
            'https://api.spotify.com/v1/me/player/devices',
            headers={'Authorization': f'Bearer {token}'}
        )
        if response.status_code == 401:
            if refresh_access_token(session_id):
                session = sessions.find_one({'_id': ObjectId(session_id)})
                token = session.get('spotify_access_token')
                response = requests.get(
                    'https://api.spotify.com/v1/me/player/devices',
                    headers={'Authorization': f'Bearer {token}'}
                )
            else:
                logger.error(f"Failed to refresh access token for session {session_id}")
                return False, "Failed to refresh access token"
        response.raise_for_status()
        devices = response.json().get('devices', [])
        active_device = next((d for d in devices if d['is_active']), None)
        device_id = active_device['id'] if active_device else devices[0]['id'] if devices else None
        if not device_id:
            return False, "No active devices found. Open Spotify and play/pause a track."
        response = requests.put(
            'https://api.spotify.com/v1/me/player/play',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'uris': [f'spotify:track:{track_id}'], 'device_id': device_id}
        )
        if response.status_code == 401:
            if refresh_access_token(session_id):
                session = sessions.find_one({'_id': ObjectId(session_id)})
                token = session.get('spotify_access_token')
                response = requests.put(
                    'https://api.spotify.com/v1/me/player/play',
                    headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                    json={'uris': [f'spotify:track:{track_id}'], 'device_id': device_id}
                )
            else:
                return False, "Failed to refresh access token"
        if response.status_code == 403:
            return False, "Premium account required to play tracks."
        logger.info(f"Play request status for session {session_id}: {response.status_code}, Response: {response.text}")
        return response.status_code == 204, None
    except requests.RequestException as e:
        logger.error(f"Error checking devices or playing track for session {session_id}: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return False, f"Error checking devices: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in play_track for {session_id}: {e}")
        return False, f"Error playing track: {str(e)}"

@app.route('/')
def index():
    return render_template('index.html')

# Authentication Endpoints
@app.route('/api/spotify/authorize')
def spotify_authorize():
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    
    params = {
        'client_id': SPOTIFY_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': SPOTIFY_REDIRECT_URI,
        'state': state,
        'scope': 'user-read-private user-read-email playlist-read-private playlist-read-collaborative user-modify-playback-state user-read-playback-state'
    }
    
    auth_url = f"https://accounts.spotify.com/authorize?{urlencode(params)}"
    return jsonify({'auth_url': auth_url})

@app.route('/callback')
def spotify_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    
    if not code or state != session.get('oauth_state'):
        return redirect('/?error=auth_failed')
    
    # Exchange code for access token
    token_data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': SPOTIFY_REDIRECT_URI,
        'client_id': SPOTIFY_CLIENT_ID,
        'client_secret': SPOTIFY_CLIENT_SECRET
    }
    
    response = requests.post('https://accounts.spotify.com/api/token', data=token_data)
    
    if response.status_code == 200:
        token_info = response.json()
        session_id = secrets.token_urlsafe(16)
        user_sessions[session_id] = token_info
        
        return redirect(f'/?session_id={session_id}')
    else:
        return redirect('/?error=token_failed')

# Playlist and Track Endpoints
@app.route('/api/playlists')
def get_playlists():
    # Return sample playlists for demo
    sample_playlists = [
        {'id': '1', 'name': 'מגמיד עשירים - 500 השירים של 5 מעני השירים המחודשים'},
        {'id': '2', 'name': 'Greatest Pop Songs'},
        {'id': '3', 'name': 'Hitster Playlist'},
        {'id': '4', 'name': 'ישראלי כל הזמנים'},
        {'id': '5', 'name': 'HITSTER : ROCK EDITION'},
        {'id': '6', 'name': 'HITSTER guilty pleasures'},
        {'id': '7', 'name': 'Top 100 Greatest Songs of All Time'},
        {'id': '8', 'name': 'Top 1000 music songs'},
        {'id': '9', 'name': 'יש בי אהבה'}
    ]
    
    return jsonify({'playlists': sample_playlists})

@app.route('/api/playlists', methods=['POST'])
def add_playlist():
    data = request.get_json()
    url = data.get('url')
    
    # Here you would parse the Spotify URL and add the playlist
    # For demo purposes, we'll just return success
    return jsonify({'success': True})

@app.route('/api/playlists/<playlist_id>', methods=['DELETE'])
def remove_playlist(playlist_id):
    # Here you would remove the playlist from storage
    return jsonify({'success': True})

@app.route('/api/spotify/session')
def get_session():
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in get_session")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in get_session: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        logger.info(f"Retrieved session {session_id}")
        return jsonify({
            'session_id': str(session['_id']),
            'playlist_theme': session.get('playlist_theme'),
            'tracks_played': session.get('tracks_played', []),
            'is_active': session.get('is_active', True),
            'created_at': session.get('created_at')
        })
    except Exception as e:
        logger.error(f"Error in get_session for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/tracks')
def get_tracks():
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in get_tracks")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in get_tracks: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        track_ids = session.get('tracks_played', [])
        track_data = []
        for track_id in track_ids:
            track = tracks.find_one({'track_id': track_id, 'session_id': str(session['_id'])})
            if track:
                track_data.append({
                    'track_id': track['track_id'],
                    'title': track['title'],
                    'artist': track['artist'],
                    'album': track['album'],
                    'release_year': track['release_year'],
                    'playlist_theme': track['playlist_theme'],
                    'played_at': track['played_at']
                })
        logger.info(f"Retrieved {len(track_data)} tracks for session {session_id}")
        return jsonify(track_data)
    except Exception as e:
        logger.error(f"Error in get_tracks for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/play-track/<playlist_id>')
def play_track(playlist_id):
    # Sample track data for demo
    sample_tracks = [
        {
            'name': "Who's That Chick? (feat. Rihanna)",
            'artist': 'David Guetta',
            'year': '2010',
            'spotify_url': 'https://open.spotify.com/track/example',
            'stream_url': None  # Would be populated with YouTube Music stream URL
        },
        {
            'name': 'Blinding Lights',
            'artist': 'The Weeknd',
            'year': '2019',
            'spotify_url': 'https://open.spotify.com/track/example2',
            'stream_url': None
        },
        {
            'name': 'Shape of You',
            'artist': 'Ed Sheeran',
            'year': '2017',
            'spotify_url': 'https://open.spotify.com/track/example3',
            'stream_url': None
        }
    ]
    
    import random
    track = random.choice(sample_tracks)
    
    # Try to get YouTube Music stream URL
    try:
        search_query = f"{track['artist']} {track['name']}"
        search_results = ytmusic.search(search_query, filter='songs', limit=1)
        
        if search_results:
            video_id = search_results[0]['videoId']
            # Note: Getting actual stream URL requires additional setup
            # For demo, we'll use a placeholder
            track['stream_url'] = f"https://www.youtube.com/watch?v={video_id}"
    except Exception as e:
        print(f"Error getting YouTube Music stream: {e}")
    
    return jsonify({'track': track})

@app.route('/api/reset', methods=['POST'])
def reset_game():
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in reset_game")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in reset_game: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {'tracks_played': [], 'playlist_theme': None}}
        )
        tracks.delete_many({'session_id': session_id})
        logger.info(f"Reset game for session {session_id}, modified: {result.modified_count}")
        return jsonify({'message': 'Game session reset'}), 200
    except Exception as e:
        logger.error(f"Error in reset_game for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)