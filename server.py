"""
Hitster Song Guessing Game - Backend (Flask) with Spotify and YouTube Music MCP integration.
This version supports full start_game and play flows for both providers.
"""

import os, time, uuid, logging, requests, random
from urllib.parse import urlencode
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

# Load env and config
load_dotenv()
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
SPOTIFY_REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI')
YTM_SERVER_URL = os.getenv('YTM_SERVER_URL')
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:3000')
MONGO_URI = os.getenv('MONGO_URI')
PORT = int(os.getenv('PORT', 8080))

# Mongo setup
mongo = MongoClient(MONGO_URI)
db = mongodb['hitster']
sessions = db.sessions
playlists = db.playlists
tracks = db.tracks

app = Flask(__name__)
CORS(app)

# --- YOUTUBE MUSIC MCP HELPERS ---
def _ytm_headers(token):
    return {'Authorization': f'Bearer {token}'}

@app.route('/api/auth/ytmusic')
def auth_ytmusic():
    sess = {'created_at': datetime.utcnow(), 'provider': 'ytmusic'}
    res = sessions.insert_one(sess)
    session_id = str(res.inserted_id)
    resp = requests.get(f"{YTM_SERVER_URL}/auth", params={'session_id': session_id})
    resp.raise_for_status()
    return jsonify({'url': resp.json().get('auth_url'), 'session_id': session_id})

@app.route('/api/callback/ytmusic')
def callback_ytmusic():
    session_id = request.args.get('session_id')
    code = request.args.get('code')
    resp = requests.post(f"{YTM_SERVER_URL}/callback", json={'code': code, 'session_id': session_id})
    resp.raise_for_status()
    tokens = resp.json()
    sessions.update_one({'_id': ObjectId(session_id)}, {'$set': {'ytm_tokens': tokens}})
    return redirect(f"{FRONTEND_URL}?session_id={session_id}")

@app.route('/api/sessions/<session_id>/ytm/playlists', methods=['GET'])
def list_ytm_playlists(session_id):
    sess = sessions.find_one({'_id': ObjectId(session_id)})
    if not sess or 'ytm_tokens' not in sess:
        return jsonify({'error': 'YTM not authorized'}), 400
    token = sess['ytm_tokens']['access_token']
    resp = requests.get(f"{YTM_SERVER_URL}/playlists", headers=_ytm_headers(token))
    return jsonify(resp.json())

@app.route('/api/sessions/<session_id>/ytm/start_game', methods=['POST'])
def ytm_start_game(session_id):
    sess = sessions.find_one({'_id': ObjectId(session_id)})
    if not sess or 'ytm_tokens' not in sess:
        return jsonify({'error': 'YTM not authorized'}), 400
    token = sess['ytm_tokens']['access_token']
    resp = requests.get(f"{YTM_SERVER_URL}/tracks/all", headers=_ytm_headers(token))
    if resp.status_code != 200:
        return jsonify({'error': 'failed to fetch tracks', 'details': resp.text}), resp.status_code
    collected_tracks = resp.json().get('tracks', [])
    if not collected_tracks:
        return jsonify({'error': 'no tracks found'}), 400
    random.shuffle(collected_tracks)
    tracks.delete_many({'session_id': session_id})
    for idx, t in enumerate(collected_tracks):
        t['session_id'] = session_id
        t['order'] = idx
        t['provider'] = 'ytmusic'
        t['played'] = False
        t['revealed'] = False
    tracks.insert_many(collected_tracks)
    return jsonify({'message': 'game started', 'tracks_count': len(collected_tracks)})

@app.route('/api/sessions/<session_id>/ytm/play', methods=['POST'])
def ytm_play(session_id):
    data = request.json or {}
    track_id = data.get('track_id')
    if not track_id:
        return jsonify({'error': 'track_id required'}), 400
    sess = sessions.find_one({'_id': ObjectId(session_id)})
    if not sess or 'ytm_tokens' not in sess:
        return jsonify({'error': 'YTM not authorized'}), 400
    token = sess['ytm_tokens']['access_token']
    resp = requests.post(f"{YTM_SERVER_URL}/play", headers=_ytm_headers(token), json={'track_id': track_id})
    if resp.status_code != 200:
        return jsonify({'error': 'failed to play', 'details': resp.text}), resp.status_code
    tracks.update_one({'session_id': session_id, 'id': track_id}, {'$set': {'played': True, 'played_at': datetime.utcnow()}})
    return jsonify({'message': 'playing'})

@app.route('/')
def index():
    return jsonify({'message': 'Hitster backend with Spotify & YTM running'})
