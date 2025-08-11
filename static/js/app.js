class SongamizerApp {
    constructor() {
        this.currentScreen = 'connect';
        this.selectedPlaylist = null;
        this.currentTrack = null;
        this.tracksPlayed = 0;
        this.audioPlayer = document.getElementById('audioPlayer');
        this.cardColors = [
            'card-color-1', 'card-color-2', 'card-color-3', 'card-color-4',
            'card-color-5', 'card-color-6', 'card-color-7', 'card-color-8'
        ];
        this.currentColorIndex = 0;
        
        this.init();
    }

    init() {
        this.bindEvents();
        this.checkAuthStatus();
    }

    bindEvents() {
        // Connect Screen
        document.getElementById('connectSpotifyBtn').addEventListener('click', () => {
            this.connectToSpotify();
        });

        // Playlist Screen
        document.getElementById('addPlaylistBtn').addEventListener('click', () => {
            this.addPlaylist();
        });

        document.getElementById('startPlayingBtn').addEventListener('click', () => {
            this.startGame();
        });

        // Game Screen
        document.getElementById('revealBtn').addEventListener('click', () => {
            this.revealCard();
        });

        // Reveal Screen
        document.getElementById('playNextBtn').addEventListener('click', () => {
            this.playNextSong();
        });

        document.getElementById('spotifyBtn').addEventListener('click', () => {
            this.openInSpotify();
        });

        // Reset buttons
        document.getElementById('resetBtn').addEventListener('click', () => {
            this.resetGame();
        });

        document.getElementById('revealResetBtn').addEventListener('click', () => {
            this.resetGame();
        });

        // Header buttons
        document.getElementById('playBtn').addEventListener('click', () => {
            this.showScreen('playlist');
        });
    }

    async checkAuthStatus() {
        const urlParams = new URLSearchParams(window.location.search);
        const sessionId = urlParams.get('session_id');
        
        if (sessionId) {
            localStorage.setItem('spotify_session', sessionId);
            this.loadPlaylists();
            this.showScreen('playlist');
        } else if (localStorage.getItem('spotify_session')) {
            this.loadPlaylists();
            this.showScreen('playlist');
        } else {
            this.showScreen('connect');
        }
    }

    async connectToSpotify() {
        try {
            const response = await fetch('/api/spotify/authorize');
            const data = await response.json();
            
            if (data.auth_url) {
                window.location.href = data.auth_url;
            }
        } catch (error) {
            console.error('Error connecting to Spotify:', error);
            alert('Failed to connect to Spotify. Please try again.');
        }
    }

    async loadPlaylists() {
        try {
            const response = await fetch('/api/playlists');
            const data = await response.json();
            
            if (data.playlists) {
                this.renderPlaylists(data.playlists);
            }
        } catch (error) {
            console.error('Error loading playlists:', error);
        }
    }

    renderPlaylists(playlists) {
        const playlistList = document.getElementById('playlistList');
        playlistList.innerHTML = '';

        playlists.forEach((playlist, index) => {
            const playlistItem = document.createElement('div');
            playlistItem.className = 'playlist-item';
            playlistItem.innerHTML = `
                <span class="playlist-number">${index + 1}</span>
                <span class="playlist-name">${playlist.name}</span>
                <button class="playlist-remove" onclick="app.removePlaylist('${playlist.id}')">
                    <i class="fas fa-times"></i>
                </button>
            `;
            
            playlistItem.addEventListener('click', (e) => {
                if (!e.target.classList.contains('playlist-remove')) {
                    this.selectPlaylist(playlist, playlistItem);
                }
            });
            
            playlistList.appendChild(playlistItem);
        });
    }

    selectPlaylist(playlist, element) {
        // Remove previous selection
        document.querySelectorAll('.playlist-item').forEach(item => {
            item.classList.remove('selected');
        });
        
        // Select current playlist
        element.classList.add('selected');
        this.selectedPlaylist = playlist;
        
        // Enable start button
        document.getElementById('startPlayingBtn').disabled = false;
    }

    async addPlaylist() {
        const url = prompt('Enter Spotify playlist URL:');
        if (url) {
            try {
                const response = await fetch('/api/playlists', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ url: url })
                });
                
                if (response.ok) {
                    this.loadPlaylists();
                } else {
                    alert('Failed to add playlist. Please check the URL and try again.');
                }
            } catch (error) {
                console.error('Error adding playlist:', error);
                alert('Failed to add playlist. Please try again.');
            }
        }
    }

    async removePlaylist(playlistId) {
        try {
            const response = await fetch(`/api/playlists/${playlistId}`, {
                method: 'DELETE'
            });
            
            if (response.ok) {
                this.loadPlaylists();
            }
        } catch (error) {
            console.error('Error removing playlist:', error);
        }
    }

    async startGame() {
        if (!this.selectedPlaylist) {
            alert('Please select a playlist first.');
            return;
        }
        
        this.showScreen('game');
        this.playRandomTrack();
    }

    async playRandomTrack() {
        try {
            const response = await fetch(`/api/play-track/${this.selectedPlaylist.id}`);
            const data = await response.json();
            
            if (data.track) {
                this.currentTrack = data.track;
                this.tracksPlayed++;
                this.updateTrackCounter();
                
                // Play audio if stream URL is available
                if (data.track.stream_url) {
                    this.audioPlayer.src = data.track.stream_url;
                    this.audioPlayer.play().catch(error => {
                        console.log('Audio autoplay prevented:', error);
                    });
                }
            }
        } catch (error) {
            console.error('Error playing track:', error);
            alert('Failed to load track. Please try again.');
        }
    }

    revealCard() {
        if (!this.currentTrack) {
            alert('No track loaded. Please try again.');
            return;
        }
        
        // Update reveal screen with track info
        document.getElementById('artistName').textContent = this.currentTrack.artist;
        document.getElementById('releaseYear').textContent = this.currentTrack.year || 'Unknown';
        document.getElementById('songName').textContent = this.currentTrack.name;
        document.getElementById('revealTracksPlayed').textContent = this.tracksPlayed;
        
        // Apply random color to reveal card
        const revealCard = document.getElementById('revealCard');
        revealCard.className = `reveal-card ${this.getNextCardColor()}`;
        
        this.showScreen('reveal');
    }

    getNextCardColor() {
        const color = this.cardColors[this.currentColorIndex];
        this.currentColorIndex = (this.currentColorIndex + 1) % this.cardColors.length;
        return color;
    }

    playNextSong() {
        this.showScreen('game');
        this.playRandomTrack();
    }

    openInSpotify() {
        if (this.currentTrack && this.currentTrack.spotify_url) {
            window.open(this.currentTrack.spotify_url, '_blank');
        }
    }

    async resetGame() {
        try {
            await fetch('/api/reset', { method: 'POST' });
            this.tracksPlayed = 0;
            this.currentTrack = null;
            this.updateTrackCounter();
            this.audioPlayer.pause();
            this.audioPlayer.src = '';
            this.showScreen('playlist');
        } catch (error) {
            console.error('Error resetting game:', error);
        }
    }

    updateTrackCounter() {
        document.getElementById('tracksPlayed').textContent = this.tracksPlayed;
        document.getElementById('revealTracksPlayed').textContent = this.tracksPlayed;
    }

    showScreen(screenName) {
        // Hide all screens
        document.querySelectorAll('.screen').forEach(screen => {
            screen.classList.remove('active');
        });
        
        // Show target screen
        document.getElementById(`${screenName}Screen`).classList.add('active');
        this.currentScreen = screenName;
    }
}

// Initialize app when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.app = new SongamizerApp();
});