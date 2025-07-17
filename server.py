import os
from flask import Flask, request, jsonify, redirect
import requests
import random
from urllib.parse import urlencode
from pymongo import MongoClient
from bson import ObjectId

app = Flask(__name__)

# Configuration from environment variables
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID', '2c46aa652c2b4da797b7bd26f4e436d0')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET', 'a65c11eca47346e0bee9ba261d7e3126')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI', 'https://hitster-randomizer.onrender.com/api/spotify/callback')
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
AUTH_CODE_ACCESS_TOKEN = None
REFRESH_TOKEN = None
CLIENT_CREDENTIALS_ACCESS_TOKEN = None

# MongoDB setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client['hitster']
sessions = db['sessions']

# Playlist configuration
THEME_PLAYLISTS = {
    'HITSTER - UK Summer Party': '2hZhVv7z6cpGcRBEgvlXLz',
    'Hebrew Hits': '37i9dQZF1DX5uM2K3k2o0Y'
}

# Get Client Credentials access token
def get_client_credentials_token():
    global CLIENT_CREDENTIALS_ACCESS_TOKEN
    try:
        response = requests.post(
            'https://accounts.spotify.com/api/token',
            data={'grant_type': 'client_credentials', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}
        )
        response.raise_for_status()
        CLIENT_CREDENTIALS_ACCESS_TOKEN = response.json().get('access_token')
        return CLIENT_CREDENTIALS_ACCESS_TOKEN
    except requests.RequestException as e:
        print(f"Error getting client credentials token: {e}")
        return None

# Refresh Authorization Code access token
def refresh_access_token():
    global AUTH_CODE_ACCESS_TOKEN, REFRESH_TOKEN
    if not REFRESH_TOKEN:
        print("Error: No refresh token available")
        return False
    try:
        response = requests.post(
            'https://accounts.spotify.com/api/token',
            data={
                'grant_type': 'refresh_token',
                'refresh_token': REFRESH_TOKEN,
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET
            }
        )
        response.raise_for_status()
        AUTH_CODE_ACCESS_TOKEN = response.json().get('access_token')
        print(f"Refreshed access token: {AUTH_CODE_ACCESS_TOKEN}")
        return True
    except requests.RequestException as e:
        print(f"Error refreshing access token: {e}")
        return False

# Fetch tracks from a playlist
def get_playlist_tracks(theme, session_id):
    global CLIENT_CREDENTIALS_ACCESS_TOKEN
    if not CLIENT_CREDENTIALS_ACCESS_TOKEN:
        CLIENT_CREDENTIALS_ACCESS_TOKEN = get_client_credentials_token()
    if not CLIENT_CREDENTIALS_ACCESS_TOKEN:
        return []
    playlist_id = THEME_PLAYLISTS.get(theme)
    if not playlist_id:
        return []
    # Get played track IDs from MongoDB
    session = sessions.find_one({'_id': ObjectId(session_id)})
    played_track_ids = session.get('played_track_ids', []) if session else []
    try:
        response = requests.get(
            f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=50',
            headers={'Authorization': f'Bearer {CLIENT_CREDENTIALS_ACCESS_TOKEN}'}
        )
        response.raise_for_status()
        tracks = [item['track'] for item in response.json()['items'] if item['track'] and item['track']['id']]
        return [track for track in tracks if track['id'] not in played_track_ids]
    except requests.RequestException as e:
        print(f"Error fetching tracks for {theme}: {e}")
        return []

# Check for active Spotify devices
def get_active_device():
    if not AUTH_CODE_ACCESS_TOKEN:
        print("No access token available")
        return None
    try:
        response = requests.get(
            'https://api.spotify.com/v1/me/player/devices',
            headers={'Authorization': f'Bearer {AUTH_CODE_ACCESS_TOKEN}'}
        )
        if response.status_code == 401:
            if refresh_access_token():
                response = requests.get(
                    'https://api.spotify.com/v1/me/player/devices',
                    headers={'Authorization': f'Bearer {AUTH_CODE_ACCESS_TOKEN}'}
                )
            else:
                return None
        response.raise_for_status()
        devices = response.json().get('devices', [])
        for device in devices:
            if device['is_active']:
                return device['id']
        return devices[0]['id'] if devices else None
    except requests.RequestException as e:
        print(f"Error checking devices: {e}")
        return None

# Play a track
def play_track(track_id):
    if not AUTH_CODE_ACCESS_TOKEN:
        print("Error: No access token set")
        return False
    device_id = get_active_device()
    if not device_id:
        print("Error: No active Spotify device found")
        return False
    try:
        response = requests.put(
            'https://api.spotify.com/v1/me/player/play',
            headers={'Authorization': f'Bearer {AUTH_CODE_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
            json={'uris': [f'spotify:track:{track_id}'], 'device_id': device_id}
        )
        if response.status_code == 401:
            if refresh_access_token():
                response = requests.put(
                    'https://api.spotify.com/v1/me/player/play',
                    headers={'Authorization': f'Bearer {AUTH_CODE_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
                    json={'uris': [f'spotify:track:{track_id}'], 'device_id': device_id}
                )
            else:
                return False
        print(f"Play request status: {response.status_code}, Response: {response.text}")
        return response.status_code == 204
    except requests.RequestException as e:
        print(f"Error playing track {track_id}: {e}")
        return False

@app.route('/api/spotify/authorize')
def spotify_authorize():
    state = 'xyz123'  # Simple state for CSRF protection
    params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'state': state,
        'scope': 'streaming user-read-playback-state user-modify-playback-state'
    }
    auth_url = f"https://accounts.spotify.com/authorize?{urlencode(params)}"
    return redirect(auth_url)

@app.route('/api/spotify/callback')
def spotify_callback():
    global AUTH_CODE_ACCESS_TOKEN, REFRESH_TOKEN
    code = request.args.get('code')
    state = request.args.get('state')
    if not code or state != 'xyz123':
        return jsonify({'error': 'Invalid code or state'}), 400
    try:
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
        AUTH_CODE_ACCESS_TOKEN = data.get('access_token')
        REFRESH_TOKEN = data.get('refresh_token')
        # Create a new session in MongoDB
        session = sessions.insert_one({'played_track_ids': []})
        return jsonify({
            'message': 'Authorization successful',
            'access_token': AUTH_CODE_ACCESS_TOKEN,
            'session_id': str(session.inserted_id)
        }), 200
    except requests.RequestException as e:
        return jsonify({'error': f'Failed to exchange code: {str(e)}'}), 400

@app.route('/api/spotify/playlists')
def get_playlists():
    return jsonify(list(THEME_PLAYLISTS.keys()))

@app.route('/api/spotify/play-track/<theme>')
def play_next_song(theme):
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': 'Session ID required'}), 400
    if theme not in THEME_PLAYLISTS:
        return jsonify({'error': 'Invalid theme'}), 400
    tracks = get_playlist_tracks(theme, session_id)
    if not tracks:
        return jsonify({'error': 'No tracks available'}), 400
    random_track = random.choice(tracks)
    if play_track(random_track['id']):
        # Update session in MongoDB
        sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$push': {'played_track_ids': random_track['id']}}
        )
        return jsonify({
            'id': random_track['id'],
            'title': random_track['name'],
            'artist': random_track['artists'][0]['name'],
            'release_year': int(random_track['album']['release_date'].split('-')[0])
        })
    return jsonify({'error': 'Failed to play track. Ensure Spotify is open on a device and your account is Premium.'}), 400

@app.route('/api/spotify/reset', methods=['POST'])
def reset_game():
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': 'Session ID required'}), 400
    sessions.update_one(
        {'_id': ObjectId(session_id)},
        {'$set': {'played_track_ids': []}}
    )
    return jsonify({'message': 'Game session reset'}), 200

@app.route('/')
def index():
    return jsonify({'message': 'Hitster Song Randomizer Backend. Use the frontend to interact.'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))