import os
from flask import Flask, request, jsonify, redirect, g
from pymongo import MongoClient
import logging

# Initialize Flask app
app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Environment variables
MONGO_URI = os.getenv('MONGO_URI')
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI')
FRONTEND_URL = os.getenv('FRONTEND_URL', 'https://preview--tune-twist-7ca04c74.base44.app')

# MongoDB connection management
def get_mongo_client():
    """Initialize MongoDB client if not already present in the request context."""
    if 'mongo_client' not in g:
        try:
            g.mongo_client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=30000,
                connectTimeoutMS=30000,
                socketTimeoutMS=30000
            )
            g.mongo_client.admin.command('ping')  # Test connection
            logger.info("MongoDB connected successfully")
        except Exception as e:
            logger.error(f"MongoDB connection failed: {e}")
            raise
    return g.mongo_client

def get_db():
    """Get the database from the MongoDB client."""
    if 'db' not in g:
        client = get_mongo_client()
        g.db = client['hitster']
    return g.db

def get_collection(name):
    """Get a specific collection from the database."""
    db = get_db()
    return db[name]

@app.teardown_appcontext
def close_mongo_connection(exception):
    """Close MongoDB connection at the end of the request."""
    if 'mongo_client' in g:
        g.mongo_client.close()
        g.pop('mongo_client')
    if 'db' in g:
        g.pop('db')

# Spotify authorize endpoint
@app.route('/api/spotify/authorize')
def spotify_authorize():
    try:
        from bson import ObjectId
        sessions = get_collection('sessions')
        session_id = str(ObjectId())
        scope = 'user-read-private playlist-read-private user-read-email user-modify-playback-state'
        params = {
            'client_id': CLIENT_ID,
            'response_type': 'code',
            'redirect_uri': REDIRECT_URI,
            'scope': scope,
            'state': session_id
        }
        from urllib.parse import urlencode
        auth_url = f'https://accounts.spotify.com/authorize?{urlencode(params)}'
        logger.debug(f"Generated Spotify auth URL: {auth_url}")
        
        # Insert new session
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

# Other endpoints (abridged for brevity)
@app.route('/api/spotify/callback')
def spotify_callback():
    # Implementation here (use get_collection('sessions') similarly)
    pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
