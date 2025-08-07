import os
import re
from flask import Flask, request, jsonify, redirect
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
from google_auth_oauthlib.flow import InstalledAppFlow
import time

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["https://preview--tune-twist-7ca04c74.base44.app", "*"]}})

# Configuration
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI')
YOUTUBE_CLIENT_ID = os.getenv('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.getenv('YOUTUBE_CLIENT_SECRET')
FRONTEND_URL = os.getenv('FRONTEND_URL', 'https://preview--tune-twist-7ca04c74.base44.app')
MONGO_URI = os.getenv('MONGO_URI')
if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, MONGO_URI]):
    missing = [k for k, v in {
        'SPOTIFY_CLIENT_ID': CLIENT_ID,
        'SPOTIFY_CLIENT_SECRET': CLIENT_SECRET,
        'SPOTIFY_REDIRECT_URI': REDIRECT_URI,
        'YOUTUBE_CLIENT_ID': YOUTUBE_CLIENT_ID,
        'YOUTUBE_CLIENT_SECRET': YOUTUBE_CLIENT_SECRET,
        'MONGO_URI': MONGO_URI
    }.items() if not v]
    logger.error(f"Missing environment variables: {missing}")
    raise ValueError(f"Missing environment variables: {missing}")
if not REDIRECT_URI.startswith('https://'):
    logger.warning(f"SPOTIFY_REDIRECT_URI ({REDIRECT_URI}) should use HTTPS for security")
logger.info(f"Environment: SPOTIFY_REDIRECT_URI={REDIRECT_URI}, YOUTUBE_REDIRECT_URI=https://hitster-randomizer.onrender.com/api/youtube_music/callback, FRONTEND_URL={FRONTEND_URL}")

# Valid icon names for playlists
VALID_ICONS = [
    'jukebox', 'boombox', 'microphone', 'bells',
    'music-note', 'record-player', 'guitar', 'headphones'
]
DEFAULT_ICON = 'music-note'

# MongoDB setup (deferred to gunicorn worker)
db = None
sessions = None
tracks = None
playlists = None
playlist_tracks = None
track_metadata = None

def init_mongodb():
    global db, sessions, tracks, playlists, playlist_tracks, track_metadata
    try:
        mongodb = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=30000,
            connectTimeoutMS=30000,
            socketTimeoutMS=30000,
            retryWrites=True,
            w='majority'
        )
        db = mongodb['hitster']
        sessions = db['sessions']
        tracks = db['tracks']
        playlists = db['playlists']
        playlist_tracks = db['playlist_tracks']
        track_metadata = db['track_metadata']
        mongodb.admin.command('ping')  # Test connection
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

# Get Client Credentials access token (Spotify)
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

# Refresh Authorization Code access token (Spotify)
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
    time.sleep(1)  # Respect MusicBrainz 1-second rate limit
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
            headers={'User-Agent': 'Songamizer/1.0 ( https://hitster-randomizer.onrender.com )'}
        )
        response.raise_for_status()
        data = response.json()
        logger.debug(f"MusicBrainz release response for {track_name} by {artist_name} (album: {album_name}): {data}")
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
                    logger.warning(f"Invalid date format in release for {track_name} by {artist_name}: {release['date']}")
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
        logger.warning(f"No valid MusicBrainz year for {track_name} by {artist_name}, using current year")
        return datetime.utcnow().year
    except Exception as e:
        logger.error(f"MusicBrainz error for {track_name} by {artist_name}: {e}")
        return datetime.utcnow().year

# Get playlist tracks
def get_playlist_tracks(playlist_id, session_id, service_type='spotify'):
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in get_playlist_tracks: {session_id}")
            return []
        cached = playlist_tracks.find_one({'playlist_id': playlist_id, 'service_type': service_type})
        if cached and cached.get('expires_at') > datetime.utcnow():
            logger.info(f"Using cached tracks for playlist {playlist_id}")
            return cached['tracks']
        tracks = []
        if service_type == 'spotify':
            token = session.get('spotify_access_token')
            if not token or (session.get('token_expires_at') and session['token_expires_at'] < datetime.utcnow()):
                if not refresh_access_token(session_id):
                    logger.error(f"Failed to refresh token for session {session_id}")
                    return []
                session = sessions.find_one({'_id': ObjectId(session_id)})
                token = session.get('spotify_access_token')
            headers = {'Authorization': f'Bearer {token}'}
            response = requests.get(
                f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?fields=items(track(id,name,artists(name),album(name,release_date)))',
                headers=headers
            )
            response.raise_for_status()
            tracks = [track['track'] for track in response.json().get('items', []) if track.get('track')]
        else:  # youtube_music
            ytmusic = YTMusic(auth={
                'access_token': session.get('youtube_music_access_token'),
                'refresh_token': session.get('youtube_music_refresh_token')
            })
            playlist = ytmusic.get_playlist(playlist_id, limit=None)
            tracks = [
                {
                    'id': track['videoId'],
                    'name': track['title'],
                    'artists': [{'name': track['artists'][0]['name']}],
                    'album': {'name': track.get('album', 'Unknown'), 'release_date': str(track.get('year', ''))}
                } for track in playlist['tracks'] if track.get('videoId')
            ]
        playlist_tracks.update_one(
            {'playlist_id': playlist_id, 'service_type': service_type},
            {'$set': {
                'tracks': tracks,
                'expires_at': datetime.utcnow() + timedelta(days=1)
            }},
            upsert=True
        )
        return tracks
    except Exception as e:
        logger.error(f"Error fetching tracks for playlist {playlist_id} (service: {service_type}): {e}")
        return []

# Play track (Spotify-specific)
def play_track(track_id, session_id):
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in play_track: {session_id}")
            return False, 'Invalid session_id'
        token = session.get('spotify_access_token')
        if not token or (session.get('token_expires_at') and session['token_expires_at'] < datetime.utcnow()):
            if not refresh_access_token(session_id):
                logger.error(f"Failed to refresh token for session {session_id}")
                return False, 'Failed to refresh token'
            session = sessions.find_one({'_id': ObjectId(session_id)})
            token = session.get('spotify_access_token')
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        response = requests.put(
            f'https://api.spotify.com/v1/me/player/play',
            headers=headers,
            json={'uris': [f'spotify:track:{track_id}']}
        )
        if response.status_code in [204, 202]:
            logger.info(f"Successfully played track {track_id} for session {session_id}")
            return True, None
        error_msg = response.json().get('error', {}).get('message', 'Unknown error')
        if 'Premium required' in error_msg:
            logger.error(f"Spotify Premium required for track {track_id}: {response.text}")
            return False, 'Spotify Premium account required for playback.'
        logger.error(f"Failed to play track {track_id}: {response.text}")
        return False, response.text
    except requests.RequestException as e:
        logger.error(f"Error playing track {track_id} for {session_id}: {e}")
        return False, str(e)

# Retry logic for ytmusicapi
def retry_get_song(ytmusic, track_id, retries=3):
    for attempt in range(retries):
        try:
            return ytmusic.get_song(track_id)
        except Exception as e:
            logger.warning(f"Retry {attempt+1}/{retries} for track {track_id}: {e}")
            time.sleep(1)
    raise Exception(f"Failed to fetch track {track_id} after {retries} attempts")

# Authentication Endpoints
@app.route('/api/spotify/authorize')
def spotify_authorize():
    try:
        session_id = str(ObjectId())
        redirect_uri = REDIRECT_URI
        scope = 'user-read-private playlist-read-private user-read-email user-modify-playback-state'
        params = {
            'client_id': CLIENT_ID,
            'response_type': 'code',
            'redirect_uri': redirect_uri,
            'scope': scope,
            'state': session_id
        }
        auth_url = f'https://accounts.spotify.com/authorize?{urlencode(params)}'
        logger.debug(f"Generated Spotify auth URL: {auth_url}")
        sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {
                'created_at': datetime.utcnow(),
                'is_active': True,
                'service_type': 'spotify'
            }},
            upsert=True
        )
        logger.info(f"Initiated Spotify auth for session {session_id}")
        return redirect(auth_url)
    except Exception as e:
        logger.error(f"Error in spotify_authorize: {e}")
        return jsonify({'error': 'Failed to initiate Spotify authentication'}), 500

@app.route('/api/spotify/callback')
def spotify_callback():
    code = request.args.get('code')
    session_id = request.args.get('state')
    error = request.args.get('error')
    logger.debug(f"Spotify callback received: code={code}, session_id={session_id}, error={error}, full_url={request.url}, headers={dict(request.headers)}")
    if error:
        logger.error(f"Spotify callback error: {error}")
        return jsonify({'error': error}), 400
    if not code or not session_id:
        logger.error(f"Missing code or session_id in Spotify callback: code={code}, session_id={session_id}")
        redirect_url = f'{FRONTEND_URL}/game?error=invalid_callback_parameters'
        logger.debug(f"Redirecting to frontend with error: {redirect_url}")
        return redirect(redirect_url)
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in Spotify callback: {session_id}")
            redirect_url = f'{FRONTEND_URL}/game?error=invalid_session_id'
            logger.debug(f"Redirecting to frontend with error: {redirect_url}")
            return redirect(redirect_url)
        response = requests.post(
            'https://accounts.spotify.com/api/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': REDIRECT_URI,
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET
            }
        )
        response.raise_for_status()
        data = response.json()
        logger.debug(f"Spotify token response: {data}")
        expires_in = data.get('expires_in', 3600)
        sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {
                'spotify_access_token': data.get('access_token'),
                'spotify_refresh_token': data.get('refresh_token'),
                'token_expires_at': datetime.utcnow() + timedelta(seconds=expires_in),
                'service_type': 'spotify'
            }}
        )
        logger.info(f"Spotify auth completed for session {session_id}")
        redirect_url = f'{FRONTEND_URL}/game?session_id={session_id}'
        logger.debug(f"Redirecting to frontend: {redirect_url}")
        return redirect(redirect_url)
    except requests.RequestException as e:
        logger.error(f"Spotify callback error for {session_id}: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        redirect_url = f'{FRONTEND_URL}/game?error=token_request_failed'
        logger.debug(f"Redirecting to frontend with error: {redirect_url}")
        return redirect(redirect_url)
    except Exception as e:
        logger.error(f"Unexpected error in Spotify callback for {session_id}: {e}")
        redirect_url = f'{FRONTEND_URL}/game?error=unexpected_error'
        logger.debug(f"Redirecting to frontend with error: {redirect_url}")
        return redirect(redirect_url)

@app.route('/api/youtube_music/authorize')
def youtube_music_authorize():
    session_id = str(ObjectId())
    redirect_uri = 'https://hitster-randomizer.onrender.com/api/youtube_music/callback'
    flow = InstalledAppFlow.from_client_config(
        {
            'web': {
                'client_id': YOUTUBE_CLIENT_ID,
                'client_secret': YOUTUBE_CLIENT_SECRET,
                'redirect_uris': [redirect_uri],
                'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                'token_uri': 'https://oauth2.googleapis.com/token'
            }
        },
        scopes=['https://www.googleapis.com/auth/youtube.readonly']
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        state=session_id
    )
    sessions.update_one(
        {'_id': ObjectId(session_id)},
        {'$set': {
            'created_at': datetime.utcnow(),
            'is_active': True,
            'service_type': 'youtube_music'
        }},
        upsert=True
    )
    logger.info(f"Initiated YouTube Music auth for session {session_id}")
    return jsonify({'authorization_url': authorization_url})

@app.route('/api/youtube_music/callback')
def youtube_music_callback():
    session_id = request.args.get('state')
    logger.debug(f"YouTube Music callback received: session_id={session_id}, full_url={request.url}, headers={dict(request.headers)}")
    if not session_id:
        logger.error("Missing session_id in YouTube Music callback")
        redirect_url = f'{FRONTEND_URL}/game?error=invalid_session_id'
        logger.debug(f"Redirecting to frontend with error: {redirect_url}")
        return redirect(redirect_url)
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in YouTube Music callback: {session_id}")
            redirect_url = f'{FRONTEND_URL}/game?error=invalid_session_id'
            logger.debug(f"Redirecting to frontend with error: {redirect_url}")
            return redirect(redirect_url)
        redirect_uri = 'https://hitster-randomizer.onrender.com/api/youtube_music/callback'
        flow = InstalledAppFlow.from_client_config(
            {
                'web': {
                    'client_id': YOUTUBE_CLIENT_ID,
                    'client_secret': YOUTUBE_CLIENT_SECRET,
                    'redirect_uris': [redirect_uri],
                    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                    'token_uri': 'https://oauth2.googleapis.com/token'
                }
            },
            scopes=['https://www.googleapis.com/auth/youtube.readonly']
        )
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        sessions.update_one(
            {'_id': ObjectId(session_id)},
            {
                '$set': {
                    'youtube_music_access_token': credentials.token,
                    'youtube_music_refresh_token': credentials.refresh_token,
                    'token_expires_at': datetime.utcnow() + timedelta(seconds=3600),
                    'service_type': 'youtube_music'
                }
            }
        )
        logger.info(f"YouTube Music auth completed for session {session_id}")
        redirect_url = f'{FRONTEND_URL}/game?session_id={session_id}'
        logger.debug(f"Redirecting to frontend: {redirect_url}")
        return redirect(redirect_url)
    except Exception as e:
        logger.error(f"YouTube Music callback error for {session_id}: {e}")
        redirect_url = f'{FRONTEND_URL}/game?error=youtube_auth_failed'
        logger.debug(f"Redirecting to frontend with error: {redirect_url}")
        return redirect(redirect_url)

# Playlist and Track Endpoints
@app.route('/api/playlists')
def get_playlists():
    session_id = request.args.get('session_id')
    service_type = request.args.get('service_type', 'spotify')  # Default to spotify for FE compatibility
    if not session_id:
        logger.error("No session_id provided in get_playlists")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in get_playlists: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        if service_type == 'spotify':
            token = session.get('spotify_access_token')
            if not token or (session.get('token_expires_at') and session['token_expires_at'] < datetime.utcnow()):
                if not refresh_access_token(session_id):
                    logger.error(f"Failed to refresh token for session {session_id}")
                    return jsonify({'error': 'Authentication required'}), 401
                session = sessions.find_one({'_id': ObjectId(session_id)})
                token = session.get('spotify_access_token')
            headers = {'Authorization': f'Bearer {token}'}
            response = requests.get('https://api.spotify.com/v1/me/playlists', headers=headers)
            response.raise_for_status()
            user_playlists = response.json().get('items', [])
            curated_playlists = playlists.find()
            all_playlists = []
            for playlist in curated_playlists:
                all_playlists.append({
                    'id': playlist['playlist_id'],
                    'name': playlist['name'],
                    'icon': playlist.get('icon', DEFAULT_ICON)
                })
            for playlist in user_playlists:
                all_playlists.append({
                    'id': playlist['id'],
                    'name': playlist['name'],
                    'icon': DEFAULT_ICON
                })
            logger.info(f"Retrieved {len(all_playlists)} Spotify playlists for session {session_id}")
            return jsonify(all_playlists)
        else:  # youtube_music
            ytmusic = YTMusic(auth={
                'access_token': session.get('youtube_music_access_token'),
                'refresh_token': session.get('youtube_music_refresh_token')
            })
            playlists = ytmusic.get_library_playlists()
            all_playlists = [
                {
                    'id': playlist['playlistId'],
                    'name': playlist['title'],
                    'icon': DEFAULT_ICON
                } for playlist in playlists
            ]
            logger.info(f"Retrieved {len(all_playlists)} YouTube Music playlists for session {session_id}")
            return jsonify(all_playlists)
    except Exception as e:
        logger.error(f"Error in get_playlists for {session_id} (service: {service_type}): {e}")
        return jsonify({'error': str(e)}), 400

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
        response = {
            'session_id': str(session['_id']),
            'playlist_theme': session.get('playlist_theme'),
            'tracks_played': session.get('tracks_played', []),
            'is_active': session.get('is_active', True),
            'created_at': session.get('created_at'),
            'user_playlists': []  # Maintain compatibility with existing FE
        }
        logger.debug(f"Session response for {session_id}: {response}")
        logger.info(f"Retrieved session {session_id}")
        return jsonify(response)
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
                    # Exclude service_type to maintain FE compatibility
                })
        logger.info(f"Retrieved {len(track_data)} tracks for session {session_id}")
        return jsonify(track_data)
    except Exception as e:
        logger.error(f"Error in get_tracks for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/play-track/<playlist_id>')
def play_next_song(playlist_id):
    session_id = request.args.get('session_id')
    service_type = request.args.get('service_type', 'spotify')  # Default to spotify for FE compatibility
    if not session_id:
        logger.error("No session_id provided in play_next_song")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in play_next_song: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {'playlist_theme': playlist_id, 'service_type': service_type}}
        )
        logger.info(f"Updated playlist_theme for session {session_id}, modified: {result.modified_count}")
        tracks_list = get_playlist_tracks(playlist_id, session_id, service_type)
        if not tracks_list:
            logger.error(f"No tracks available for playlist {playlist_id} (service: {service_type})")
            return jsonify({'error': f'No tracks available for playlist {playlist_id}. Playlist may be empty or inaccessible.'}), 400
        random_track = random.choice(tracks_list)
        track_id = random_track['id']
        track_name = random_track['name']
        artist_name = random_track['artists'][0]['name']
        album_name = random_track['album']['name']
        fallback_year = int(random_track['album']['release_date'].split('-')[0]) if random_track['album']['release_date'] else datetime.utcnow().year
        original_year = get_original_release_year(track_name, artist_name, album_name, fallback_year)
        stream_url = None
        if service_type == 'spotify':
            success, error = play_track(track_id, session_id)
            if not success:
                logger.error(f"Failed to play Spotify track {track_id} for session {session_id}: {error}")
                return jsonify({'error': error or 'Failed to play track. Ensure Spotify is open on a device and your account is Premium.'}), 400
        else:  # youtube_music
            ytmusic = YTMusic(auth={
                'access_token': session.get('youtube_music_access_token'),
                'refresh_token': session.get('youtube_music_refresh_token')
            })
            try:
                song_data = retry_get_song(ytmusic, track_id)
                stream_url = song_data.get('streamingData', {}).get('adaptiveFormats', [{}])[0].get('url')
                if not stream_url:
                    logger.error(f"No stream URL for YouTube Music track {track_id}")
                    return jsonify({'error': 'No stream URL available'}), 400
            except Exception as e:
                logger.error(f"Failed to fetch YouTube Music stream URL for {track_id}: {e}")
                return jsonify({'error': str(e)}), 400
        tracks.insert_one({
            'track_id': track_id,
            'title': track_name,
            'artist': artist_name,
            'album': album_name,
            'release_year': original_year,
            'playlist_theme': playlist_id,
            'played_at': datetime.utcnow().isoformat(),
            'session_id': str(session['_id']),
            'expires_at': datetime.utcnow() + timedelta(hours=2),
            'service_type': service_type,
            'stream_url': stream_url if service_type == 'youtube_music' else None
        })
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$push': {'tracks_played': track_id}}
        )
        logger.info(f"Played track {track_id} for session {session_id}, modified: {result.modified_count}")
        response = {
            'track_id': track_id,
            'title': track_name,
            'artist': artist_name,
            'release_year': original_year,
            'album': album_name,
            'playlist_theme': playlist_id,
            'played_at': datetime.utcnow().isoformat()
            # Exclude service_type and stream_url for FE compatibility
        }
        if service_type == 'youtube_music':
            response['stream_url'] = stream_url
        logger.debug(f"Play track response for {session_id}: {response}")
        return jsonify(response)
    except Exception as e:
        logger.error(f"Error in play_next_song for {session_id} (service: {service_type}): {e}")
        return jsonify({'error': str(e)}), 400

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

@app.route('/')
def index():
    return jsonify({'message': 'Songamizer Backend. Use the frontend to interact.'})

@app.route('/privacy')
def privacy_policy():
    policy = """
    <html>
    <head><title>Songamizer Privacy Policy</title></head>
    <body>
    <h1>Songamizer Privacy Policy</h1>
    <p><strong>Effective Date:</strong> August 7, 2025</p>
    <p>Songamizer collects and processes the following data:</p>
    <ul>
        <li><strong>Authentication Tokens:</strong> Spotify and YouTube Music OAuth tokens for accessing playlists and playback, stored securely in MongoDB with a 2-hour TTL for tracks and 30-day TTL for metadata.</li>
        <li><strong>Session Data:</strong> Session IDs and playlist selections to manage game state, stored in MongoDB.</li>
        <li><strong>Usage Data:</strong> Non-personal data (e.g., track plays) to improve gameplay, cached temporarily.</li>
    </ul>
    <p>We use this data to provide the Songamizer service, enabling music playback and game functionality. Data is not shared with third parties except as required by Spotify and YouTube APIs. You may revoke access via your Spotify or YouTube account settings.</p>
    <p>For questions, contact: support@songamizer.app</p>
    <p>We comply with GDPR and CCPA. You have the right to access, delete, or restrict your data. Email us to exercise these rights.</p>
    </body>
    </html>
    """
    return policy

@app.route('/api/data-request', methods=['POST'])
def data_request():
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in data_request")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in data_request: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        track_data = tracks.find({'session_id': str(session['_id'])})
        data = {
            'session': {
                'session_id': str(session['_id']),
                'created_at': session.get('created_at'),
                'tracks_played': session.get('tracks_played', [])
                # Exclude service_type for FE compatibility
            },
            'tracks': [
                {
                    'track_id': track['track_id'],
                    'title': track['title'],
                    'artist': track['artist'],
                    'album': track['album'],
                    'release_year': track['release_year']
                } for track in track_data
            ]
        }
        logger.info(f"Data request fulfilled for session {session_id}")
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error in data_request for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/data-delete', methods=['POST'])
def data_delete():
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in data_delete")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in data_delete: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        sessions.delete_one({'_id': ObjectId(session_id)})
        tracks.delete_many({'session_id': str(session['_id'])})
        logger.info(f"Data deleted for session {session_id}")
        return jsonify({'message': 'User data deleted'})
    except Exception as e:
        logger.error(f"Error in data_delete for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

if __name__ == '__main__':
    init_mongodb()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))