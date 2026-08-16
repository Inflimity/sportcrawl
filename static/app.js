/**
 * SofaScore Football Dashboard Client App
 * Handles fixture loading, real-time WebSocket live score updates, filtering, and bookmarking.
 */

class FootballApp {
    constructor() {
        this.matches = [];
        this.currentFilter = 'all';
        this.searchQuery = '';
        this.ws = null;
        this.reconnectTimeout = null;

        this.initElements();
        this.bindEvents();
        this.initWebSocket();
        this.loadMatches();
        this.loadStatus();
    }

    initElements() {
        this.matchesViewport = document.getElementById('matches-viewport');
        this.filterTabs = document.querySelectorAll('.filter-tab');
        this.searchInput = document.getElementById('match-search');
        this.clearSearchBtn = document.getElementById('clear-search-btn');
        this.btnTriggerScrape = document.getElementById('btn-trigger-scrape');
        this.wsIndicator = document.getElementById('ws-indicator');
        this.wsStatusText = document.getElementById('ws-status-text');
        this.displayDate = document.getElementById('display-date');
        this.toastContainer = document.getElementById('toast-container');

        // Metric elements
        this.metricTotal = document.getElementById('metric-total-today');
        this.metricLive = document.getElementById('metric-live-now');
        this.metricFeatured = document.getElementById('metric-featured-count');
        this.metricFinished = document.getElementById('metric-finished-count');

        // Badges
        this.badgeAll = document.getElementById('badge-all');
        this.badgeLive = document.getElementById('badge-live');
        this.badgeTop = document.getElementById('badge-top');
        this.badgeUpcoming = document.getElementById('badge-upcoming');
        this.badgeFinished = document.getElementById('badge-finished');
        this.badgeSaved = document.getElementById('badge-saved');

        // Set today's date in header
        const today = new Date();
        this.displayDate.textContent = today.toLocaleDateString('en-US', {
            weekday: 'short',
            month: 'short',
            day: 'numeric',
            year: 'numeric'
        });
    }

    bindEvents() {
        // Tab Filtering
        this.filterTabs.forEach(tab => {
            tab.addEventListener('click', (e) => {
                const targetTab = e.currentTarget;
                this.filterTabs.forEach(t => t.classList.remove('active'));
                targetTab.classList.add('active');
                this.currentFilter = targetTab.dataset.filter;
                this.renderMatches();
            });
        });

        // Search Input
        this.searchInput.addEventListener('input', (e) => {
            this.searchQuery = e.target.value.trim().toLowerCase();
            this.clearSearchBtn.style.display = this.searchQuery ? 'block' : 'none';
            this.renderMatches();
        });

        this.clearSearchBtn.addEventListener('click', () => {
            this.searchInput.value = '';
            this.searchQuery = '';
            this.clearSearchBtn.style.display = 'none';
            this.renderMatches();
        });

        // On-demand scrape trigger
        this.btnTriggerScrape.addEventListener('click', () => {
            this.triggerScrape();
        });
    }

    async loadMatches() {
        try {
            const res = await fetch('/api/matches/today');
            if (!res.ok) throw new Error('Failed to load matches');
            this.matches = await res.json();
            this.updateMetrics();
            this.renderMatches();
        } catch (err) {
            console.error(err);
            this.showToast('⚠️ Could not connect to API server', 'error');
        }
    }

    async loadStatus() {
        try {
            const res = await fetch('/api/status');
            if (res.ok) {
                const data = await res.json();
                console.log('Bot status:', data);
            }
        } catch (e) {}
    }

    async triggerScrape() {
        this.btnTriggerScrape.classList.add('loading');
        this.btnTriggerScrape.disabled = true;
        this.showToast('🔄 Opening SofaScore to fetch today\'s fixtures...');

        try {
            const res = await fetch('/api/scrape/trigger', { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                this.showToast('✅ ' + data.message, 'success');
                await this.loadMatches();
            } else {
                this.showToast('❌ Scrape failed', 'error');
            }
        } catch (err) {
            this.showToast('❌ Error contacting scraper', 'error');
        } finally {
            this.btnTriggerScrape.classList.remove('loading');
            this.btnTriggerScrape.disabled = false;
        }
    }

    async toggleBookmark(matchId, buttonEl) {
        try {
            const res = await fetch(`/api/matches/${matchId}/bookmark`, { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                const match = this.matches.find(m => m.match_id === matchId);
                if (match) {
                    match.bookmarked = !match.bookmarked;
                    buttonEl.classList.toggle('bookmarked', match.bookmarked);
                    this.updateMetrics();
                    if (this.currentFilter === 'saved') {
                        this.renderMatches();
                    }
                    this.showToast(match.bookmarked ? '📌 Match pinned to watchlist' : 'Unpinned match');
                }
            }
        } catch (e) {
            this.showToast('Error updating bookmark', 'error');
        }
    }

    updateMetrics() {
        const total = this.matches.length;
        const live = this.matches.filter(m => m.status_type === 'inprogress').length;
        const featured = this.matches.filter(m => m.is_featured).length;
        const finished = this.matches.filter(m => m.status_type === 'finished').length;
        const upcoming = this.matches.filter(m => m.status_type === 'notstarted').length;
        const saved = this.matches.filter(m => m.bookmarked).length;

        this.metricTotal.textContent = total;
        this.metricLive.textContent = live;
        this.metricFeatured.textContent = featured;
        this.metricFinished.textContent = finished;

        this.badgeAll.textContent = total;
        this.badgeLive.textContent = live;
        this.badgeTop.textContent = featured;
        this.badgeUpcoming.textContent = upcoming;
        this.badgeFinished.textContent = finished;
        this.badgeSaved.textContent = saved;
    }

    getFilteredMatches() {
        return this.matches.filter(m => {
            // Tab filter
            if (this.currentFilter === 'live' && m.status_type !== 'inprogress') return false;
            if (this.currentFilter === 'top' && !m.is_featured) return false;
            if (this.currentFilter === 'upcoming' && m.status_type !== 'notstarted') return false;
            if (this.currentFilter === 'finished' && m.status_type !== 'finished') return false;
            if (this.currentFilter === 'saved' && !m.bookmarked) return false;

            // Search query
            if (this.searchQuery) {
                const searchCorpus = `${m.home_team} ${m.away_team} ${m.tournament_name} ${m.category_name}`.toLowerCase();
                if (!searchCorpus.includes(this.searchQuery)) return false;
            }

            return true;
        });
    }

    renderMatches() {
        const filtered = this.getFilteredMatches();

        if (filtered.length === 0) {
            this.matchesViewport.innerHTML = `
                <div class="empty-state">
                    <div class="empty-title">⚽ No matches found</div>
                    <div class="empty-desc">
                        ${this.searchQuery ? `No fixtures matched "${this.escapeHtml(this.searchQuery)}".` : 'No fixtures currently match the active filter.'}
                    </div>
                </div>
            `;
            return;
        }

        // Group by Tournament
        const groups = {};
        filtered.forEach(m => {
            const key = m.tournament_name;
            if (!groups[key]) {
                groups[key] = {
                    name: m.tournament_name,
                    category: m.category_name,
                    is_featured: m.is_featured,
                    matches: []
                };
            }
            groups[key].matches.push(m);
        });

        // Sort groups: featured first, then name
        const sortedGroups = Object.values(groups).sort((a, b) => {
            if (a.is_featured && !b.is_featured) return -1;
            if (!a.is_featured && b.is_featured) return 1;
            return a.name.localeCompare(b.name);
        });

        let html = '';
        sortedGroups.forEach(group => {
            html += `
                <div class="league-group">
                    <div class="league-header">
                        <div class="league-title-box">
                            <span class="league-icon">${group.is_featured ? '⭐' : '🏆'}</span>
                            <div>
                                <span class="league-name">${this.escapeHtml(group.name)}</span>
                                <span class="league-country"> • ${this.escapeHtml(group.category)}</span>
                            </div>
                        </div>
                        <span class="league-count">${group.matches.length} ${group.matches.length === 1 ? 'game' : 'games'}</span>
                    </div>

                    <div class="matches-grid">
                        ${group.matches.map(m => this.renderMatchCard(m)).join('')}
                    </div>
                </div>
            `;
        });

        this.matchesViewport.innerHTML = html;

        // Bind bookmark buttons
        this.matchesViewport.querySelectorAll('.btn-bookmark').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const matchId = parseInt(btn.dataset.matchId, 10);
                this.toggleBookmark(matchId, btn);
            });
        });
    }

    renderMatchCard(m) {
        const isLive = m.status_type === 'inprogress';
        let statusBadgeClass = m.status_type;
        let statusLabel = m.status_description;

        if (isLive) {
            statusLabel = m.minute ? `${m.minute}'` : 'LIVE';
        } else if (m.status_type === 'finished') {
            statusLabel = 'FT';
        }

        const homeScore = m.home_score !== null ? m.home_score : '-';
        const awayScore = m.away_score !== null ? m.away_score : '-';
        
        let kickTime = '';
        if (m.start_time) {
            const d = new Date(m.start_time);
            kickTime = d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false }) + ' UTC';
        }

        return `
            <div class="match-card ${isLive ? 'is-live' : ''}" id="card-${m.match_id}">
                <div class="match-header">
                    <div class="match-meta">
                        <span>${m.round_info || 'Fixture'}</span>
                    </div>
                    <div class="status-pill ${statusBadgeClass}">${statusLabel}</div>
                </div>

                <div class="teams-container">
                    <div class="team-row">
                        <div class="team-name-box">
                            <span class="team-name">${this.escapeHtml(m.home_team)}</span>
                        </div>
                        <span class="team-score">${homeScore}</span>
                    </div>

                    <div class="team-row">
                        <div class="team-name-box">
                            <span class="team-name">${this.escapeHtml(m.away_team)}</span>
                        </div>
                        <span class="team-score">${awayScore}</span>
                    </div>
                </div>

                <div class="match-footer">
                    <span class="kickoff-time">🕒 ${kickTime}</span>
                    <div class="card-actions">
                        <button class="btn-icon-action btn-bookmark ${m.bookmarked ? 'bookmarked' : ''}" data-match-id="${m.match_id}" title="Pin to Watchlist">
                            📌
                        </button>
                        ${m.sofascore_url ? `
                            <a href="${m.sofascore_url}" target="_blank" rel="noopener noreferrer" class="btn-icon-action" title="Open on SofaScore">
                                ↗
                            </a>
                        ` : ''}
                    </div>
                </div>
            </div>
        `;
    }

    initWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        try {
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                this.wsIndicator.classList.remove('offline');
                this.wsIndicator.classList.add('online');
                this.wsStatusText.textContent = 'Live Sync';
                console.log('WebSocket connected to SofaScore live stream');
            };

            this.ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
                    if (msg.type === 'match_alert') {
                        this.handleLiveMatchAlert(msg);
                    }
                } catch (e) {
                    console.error('Error handling WebSocket message', e);
                }
            };

            this.ws.onclose = () => {
                this.wsIndicator.classList.remove('online');
                this.wsIndicator.classList.add('offline');
                this.wsStatusText.textContent = 'Reconnecting...';
                this.reconnectTimeout = setTimeout(() => this.initWebSocket(), 3000);
            };

            this.ws.onerror = () => {
                this.ws.close();
            };
        } catch (e) {
            console.error('WebSocket init error', e);
        }
    }

    handleLiveMatchAlert(msg) {
        const alertData = msg.data;
        if (!alertData) return;

        // Show toast notification
        if (msg.alert_type === 'goal') {
            this.showToast(`⚽ ${msg.message}`, 'goal');
        } else {
            this.showToast(`📢 ${msg.message}`);
        }

        // Update in-memory match
        const idx = this.matches.findIndex(m => m.match_id === alertData.match_id);
        if (idx !== -1) {
            this.matches[idx] = { ...this.matches[idx], ...alertData };
        } else {
            this.matches.push(alertData);
        }

        this.updateMetrics();
        this.renderMatches();
    }

    showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;
        this.toastContainer.appendChild(toast);

        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(10px)';
            toast.style.transition = 'all 0.3s ease';
            setTimeout(() => toast.remove(), 300);
        }, 4000);
    }

    escapeHtml(str) {
        if (!str) return '';
        return str
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }
}

// Bootstrap on DOM ready
document.addEventListener('DOMContentLoaded', () => {
    window.app = new FootballApp();
});
