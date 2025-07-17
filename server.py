import os
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
import requests
import random
from urllib.parse import urlencode
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})  # Allow Base44 domain

# Configuration from environment variables
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID', '2c46aa652c2b4da797b7bd26f4e436d0')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET', 'a65c11eca47346e0bee9ba261d7e3126')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI', 'https://hitster-randomizer.onrender.com/api/spotify/callback')
MONGO_URI = os.getenv('MONGO_URI')
if not MONGO_URI:
    raise ValueError("MONGO_URI environment variable not set")

# MongoDB setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client['hitster']
sessions = db['sessions']

# Playlist configuration
THEME_PLAYLISTS = {
    'hitster_uk': '2hZhVv7z6cpGcRBEgvlXLz',
    'hebrew_hits': '6q2dtkU2I1tR8ZVQI8mian'
}

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
        print(f"Error getting client credentials token: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return None

# Refresh Authorization Code access token
def refresh_access_token(session_id):
    session = sessions.find_one({'_id': ObjectId(session_id)})
    if not session or not session.get('spotify_refresh_token'):
        print("Error: No refresh token available")
        return False
    try:
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
        sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {
                'spotify_access_token': data.get('access_token'),
                'token_expires_at': datetime.utcnow() + timedelta(seconds=expires_in)
            }}
        )
        print(f"Refreshed access token for session {session_id}")
        return True
    except requests.RequestException as e:
        print(f"Error refreshing access token: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return False

# Fetch tracks from a playlist with pagination
def get_playlist_tracks(theme, session_id):
    token = get_client_credentials_token()
    if not token:
        print(f"Error: No client credentials token for {theme}")
        return []
    playlist_id = THEME_PLAYLISTS.get(theme)
    if not playlist_id:
        print(f"Error: Invalid playlist theme {theme}")
        return []
    session = sessions.find_one({'_id': ObjectId(session_id)})
    played_track_ids = session.get('tracks_played', []) if session else []
    tracks = []
    offset = 0
    limit = 50
    while True:
        try:
            response = requests.get(
                f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit={limit}&offset={offset}',
                headers={'Authorization': f'Bearer {token}'}
            )
            if response.status_code == 401:
                token = get_client_credentials_token()
                if not token:
                    print(f"Error: Failed to refresh client credentials token for {theme}")
                    return []
                response = requests.get(
                    f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit={limit}&offset={offset}',
                    headers={'Authorization': f'Bearer {token}'}
                )
            response.raise_for_status()
            data = response.json()
            if 'items' not in data:
                print(f"Error: No 'items' in response for {theme}, Response: {data}")
                return []
            new_tracks = [item['track'] for item in data['items'] if item['track'] and item['track']['id']]
            tracks.extend(new_tracks)
            if len(data['items']) < limit:
                break
            offset += limit
        except requests.RequestException as e:
            print(f"Error fetching tracks for {theme} at offset {offset}: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
            return []
    unplayed_tracks = [track for track in tracks if track['id'] not in played_track_ids]
    if not unplayed_tracks and tracks:
        # Reset tracks_played if all tracks have been played
        sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {'tracks_played': []}}
        )
        print(f"Reset tracks_played for session {session_id} as all tracks were played for {theme}")
        return tracks
    if not unplayed_tracks:
        print(f"No unplayed tracks for {theme}, Total tracks: {len(tracks)}, Played tracks: {len(played_track_ids)}")
    return unplayed_tracks

# Check for active Spotify devices
def get_active_device(session_id):
    session = sessions.find_one({'_id': ObjectId(session_id)})
    if not session or not session.get('spotify_access_token'):
        return None, "No access token available"
    try:
        response = requests.get(
            'https://api.spotify.com/v1/me/player/devices',
            headers={'Authorization': f'Bearer {session["spotify_access_token"]}'}
        )
        if response.status_code == 401:
            if refresh_access_token(session_id):
                session = sessions.find_one({'_id': ObjectId(session_id)})
                response = requests.get(
                    'https://api.spotify.com/v1/me/player/devices',
                    headers={'Authorization': f'Bearer {session["spotify_access_token"]}'}
                )
            else:
                return None, "Failed to refresh access token"
        response.raise_for_status()
        devices = response.json().get('devices', [])
        for device in devices:
            if device['is_active']:
                return device['id'], None
        return devices[0]['id'] if devices else None, "No active devices found. Open Spotify and play/pause a track."
    except requests.RequestException as e:
        print(f"Error checking devices: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return None, f"Error checking devices: {str(e)}"

# Play a track
def play_track(track_id, session_id):
    session = sessions.find_one({'_id': ObjectId(session_id)})
    if not session or not session.get('spotify_access_token'):
        return False, "Error: No access token set"
    device_id, error = get_active_device(session_id)
    if not device_id:
        return False, error or "Error: No active Spotify device found"
    try:
        response = requests.put(
            'https://api.spotify.com/v1/me/player/play',
            headers={'Authorization': f'Bearer {session["spotify_access_token"]}', 'Content-Type': 'application/json'},
            json={'uris': [f'spotify:track:{track_id}'], 'device_id': device_id}
        )
        if response.status_code == 401:
            if refresh_access_token(session_id):
                session = sessions.find_one({'_id': ObjectId(session_id)})
                response = requests.put(
                    'https://api.spotify.com/v1/me/player/play',
                    headers={'Authorization': f'Bearer {session["spotify_access_token"]}', 'Content-Type': 'application/json'},
                    json={'uris': [f'spotify:track:{track_id}'], 'device_id': device_id}
                )
            else:
                return False, "Failed to refresh access token"
        print(f"Play request status: {response.status_code}, Response: {response.text}")
        return response.status_code == 204, None
    except requests.RequestException as e:
        print(f"Error playing track {track_id}: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return False, f"Error playing track {track_id}: {str(e)}"

@app.route('/api/spotify/authorize')
def spotify_authorize():
    state = 'xyz123'
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
        expires_in = data.get('expires_in', 3600)
        session = sessions.insert_one({
            'spotify_access_token': data.get('access_token'),
            'spotify_refresh_token': data.get('refresh_token'),
            'token_expires_at': datetime.utcnow() + timedelta(seconds=expires_in),
            'tracks_played': [],
            'is_active': True,
            'playlist_theme': None
        })
        return jsonify({
            'message': 'Authorization successful',
            'session_id': str(session.inserted_id)
        }), 200
    except requests.RequestException as e:
        print(f"Error in spotify_callback: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return jsonify({'error': f'Failed to exchange code: {str(e)}'}), 400

@app.route('/api/spotify/playlists')
def get_playlists():
    return jsonify(list(THEME_PLAYLISTS.keys()))

@app.route('/api/spotify/session')
def get_session():
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': 'Session ID required'}), 400
    session = sessions.find_one({'_id': ObjectId(session_id)})
    if not session:
        return jsonify({'error': 'Invalid session_id'}), 400
    return jsonify({
        'session_id': str(session['_id']),
        'playlist_theme': session.get('playlist_theme'),
        'tracks_played': session.get('tracks_played', []),
        'is_active': session.get('is_active', True)
    })

@app.route('/api/spotify/play-track/<theme>')
def play_next_song(theme):
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': 'Session ID required'}), 400
    session = sessions.find_one({'_id': ObjectId(session_id)})
    if not session:
        return jsonify({'error': 'Invalid session_id'}), 400
    if theme not in THEME_PLAYLISTS:
        return jsonify({'error': f'Invalid theme: {theme}'}), 400
    sessions.update_one(
        {'_id': ObjectId(session_id)},
        {'$set': {'playlist_theme': theme}}
    )
    tracks = get_playlist_tracks(theme, session_id)
    if not tracks:
        return jsonify({'error': f'No tracks available for {theme}. Playlist may be empty or inaccessible.'}), 400
    random_track = random.choice(tracks)
    success, error = play_track(random_track['id'], session_id)
    if success:
        sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$push': {'tracks_played': random_track['id']}}
        )
        return jsonify({
            'spotify_id': random_track['id'],
            'title': random_track['name'],
            'artist': random_track['artists'][0]['name'],
            'release_year': int(random_track['album']['release_date'].split('-')[0]),
            'album': random_track['album']['name'],
            'playlist_theme': theme,
            'played_at': datetime.utcnow().isoformat()
        })
    return jsonify({'error': error or 'Failed to play track. Ensure Spotify is open on a device and your account is Premium.'}), 400

@app.route('/api/spotify/reset', methods=['POST'])
def reset_game():
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': 'Session ID required'}), 400
    sessions.update_one(
        {'_id': ObjectId(session_id)},
        {'$set': {'tracks_played': [], 'playlist_theme': None}}
    )
    return jsonify({'message': 'Game session reset'}), 200

@app.route('/')
def index():
    return jsonify({'message': 'Hitster Song Randomizer Backend. Use the frontend to interact.'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))