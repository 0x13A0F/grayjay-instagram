const PLATFORM = "Instagram";
const DOMAIN = "https://www.instagram.com";

let API_BASE = "http://192.168.1.10:8000"; // Camoufox backend (no trailing /)
// Shared secret for the backend (must equal the server's IG_API_KEY). Leave
// "" only when the backend is unauthenticated (e.g. a private LAN setup).
let API_KEY = "";
let PAGE_SIZE = 24;

// Instagram's "All posts" saved bucket (everything you've saved).
const ALL_SAVED = "ALL_MEDIA_AUTO_COLLECTION";

// Usernames that look like a profile path but are not channels.
const RESERVED = [
    "reel", "reels", "p", "tv", "explore", "accounts", "stories", "direct",
    "about", "developer", "legal", "privacy", "press", "api", "web", "ar",
    "your_activity", "lite", "challenge", "session", "saved"
];

let config = {}
let plug_settings = {}

// =============================================================================
// Lifecycle
// =============================================================================
source.enable = function (conf, settings, savedState) {
    config = conf
    plug_settings = settings || {};
};

source.saveState = function () { return ""; };

// =============================================================================
// Backend HTTP layer  (the ONLY place that touches http / the session)
// =============================================================================
// Shown verbatim when Instagram rate-limits us. Home/search catch transient
// errors and fall back to an empty pager, which would silently hide this -
// the one error the user most needs to see - so they re-throw on this exact
// message (see isRateLimit).
const RATE_LIMIT_MSG = "Instagram is rate-limiting this account - the backend is backing off. Wait a minute, then try again; if it keeps happening, raise \"Request spacing\" in the plugin settings.";

function isRateLimit(e) {
    return !!e && e.message === RATE_LIMIT_MSG;
}

function apiGet(path, params) {
    if (!API_BASE) throw new ScriptException("Set the Backend API URL in plugin settings.");
    let url = API_BASE + path;
    const qs = [];
    for (const k in (params || {})) {
        const v = params[k];
        if (v === undefined || v === null || v === "") continue;
        qs.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    }
    if (qs.length) url += "?" + qs.join("&");

    const headers = { "Accept": "application/json" };
    if (API_KEY) headers["X-API-Key"] = API_KEY;
    // Ask the backend to cache this response for a bit (see the "Cache
    // duration" setting). Repeated identical requests - flipping pages,
    // re-opening a channel - then come straight from Redis, so navigation is
    // instant and Instagram is hit less. 0 / off -> header omitted (no cache).
    const cacheTtl = cacheTtlSeconds();
    if (cacheTtl > 0) headers["X-Cache-TTL"] = String(cacheTtl);
    // Minimum gap the backend must leave between two real Instagram calls
    // (see "Request spacing"). The backend clamps this to its own safe floor,
    // so this can only ever ask it to go slower - never faster.
    headers["X-Min-Interval"] = String(requestSpacingSeconds());

    // Retry transient backend errors (5xx / network blips) a few times.
    let lastMsg = "";
    for (let attempt = 0; attempt < 3; attempt++) {
        const resp = http.GET(url, headers, false);

        if (resp.isOk) {
            try {
                return JSON.parse(resp.body);
            } catch (e) {
                throw new ScriptException("Backend returned invalid JSON for " + path);
            }
        }

        if (resp.code === 429) {
            // Instagram rate-limited us and the backend is already sitting
            // out a cooldown. Retrying now only deepens it.
            throw new ScriptException(RATE_LIMIT_MSG);
        }

        if (resp.code === 401 || resp.code === 403) {
            // 401 here is either the backend's API-key gate or an expired IG
            // session; the body distinguishes them.
            if (/API key/i.test(resp.body || "")) {
                throw new ScriptException("Backend rejected the API key - set API_KEY in the plugin to match the server's IG_API_KEY.");
            }
            throw new ScriptException("Instagram session expired - re-run the backend login (login.py).");
        }

        lastMsg = "Backend request failed (" + resp.code + ") for " + path;
        const body = resp.body || "";
        const transient = resp.code >= 500 || /Proxy|Timeout|Connect|Temporar/i.test(body);
        if (!transient) throw new ScriptException(lastMsg);
        // transient -> retry
    }
    throw new ScriptException(lastMsg + " - transient backend error after 3 tries.");
}

// Backend endpoint wrappers ----------------------------------------------------
function apiSearchUsers(query) {
    return apiGet("/search/users", { query: query }); // -> [UserShort]
}
function apiGetUser(username) {
    return apiGet("/user", { username: username }); // -> User (GraphQL path; can fail)
}
function apiGetUserById(userId) {
    return apiGet("/user", { user_id: userId }); // -> User (private v1 path; reliable)
}
function apiGetUserReels(username, cursor) {
    return apiGet("/user/reels", { username: username, amount: PAGE_SIZE, cursor: cursor || "" }); // -> {items, next_cursor}
}
function apiGetMediaByCode(code) {
    return apiGet("/media", { code: code }); // -> Media
}
function apiGetComments(mediaId, cursor) {
    return apiGet("/media/comments", { media_id: mediaId, amount: PAGE_SIZE, cursor: cursor || "" }); // -> {items, next_cursor}
}
function apiGetReplies(mediaId, commentId, cursor) {
    return apiGet("/media/comments/replies", { media_id: mediaId, comment_id: commentId, amount: PAGE_SIZE, cursor: cursor || "" }); // -> {items, next_cursor}
}
function apiGetFeed(cursor) {
    return apiGet("/feed", { amount: PAGE_SIZE, cursor: cursor || "" }); // -> {items, next_cursor} (logged-in home timeline)
}
function apiSearchReels(query, cursor, sessionId) {
    return apiGet("/search/reels", { query: query, cursor: cursor || "", session_id: sessionId }); // -> {items, next_cursor}
}
function apiGetCollections() {
    return apiGet("/saved/collections", {}); // -> {items:[{id,name,media_count,cover}], next_cursor}
}
function apiGetSavedReels(collectionId, cursor) {
    return apiGet("/saved/reels", { collection_id: collectionId, cursor: cursor || "" }); // -> {items, next_cursor}
}
function apiGetFollowing(cursor) {
    return apiGet("/following", { cursor: cursor || "" }); // -> {items:[UserShort], next_cursor}
}

// RFC4122-ish v4 id; ties a keyword search's pages to one session so the
// backend's paginated GraphQL query stays consistent across pages.
function newSessionId() {
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
        const r = Math.random() * 16 | 0;
        const v = c === "x" ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    });
}

// True if the user enabled on-demand reply fetching in plugin settings.
function repliesEnabled() {
    const v = plug_settings.fetchReplies;
    return v === true || v === "true";
}

// "Cache duration" setting -> seconds sent as X-Cache-TTL. Grayjay Dropdowns
// hand back the selected option's INDEX (as a string); we also accept the
// label itself, defensively, in case a build passes that instead. Keep this
// list in sync with the "options" in InstagramConfig.json.
const CACHE_OPTIONS = [
    { label: "Off", seconds: 0 },
    { label: "1 minute", seconds: 60 },
    { label: "3 minutes", seconds: 180 },
    { label: "5 minutes", seconds: 300 },
    { label: "10 minutes", seconds: 600 },
];
const CACHE_DEFAULT_SECONDS = 180; // "3 minutes" - matches the setting default

function cacheTtlSeconds() {
    const raw = plug_settings.cacheDuration;
    if (raw === undefined || raw === null || raw === "") return CACHE_DEFAULT_SECONDS;
    for (const o of CACHE_OPTIONS) {           // exact label match first
        if (raw === o.label) return o.seconds;
    }
    const idx = parseInt(raw, 10);             // otherwise treat it as an index
    if (!isNaN(idx) && idx >= 0 && idx < CACHE_OPTIONS.length) {
        return CACHE_OPTIONS[idx].seconds;
    }
    return CACHE_DEFAULT_SECONDS;
}

// "Request spacing" setting -> seconds sent as X-Min-Interval. Instagram
// flags accounts on request BURSTS, so the backend enforces a minimum gap
// between outbound calls; this picks how conservative to be. Keep in sync
// with the "options" in InstagramConfig.json.
const SPACING_OPTIONS = [
    { label: "Fast (1s)", seconds: 1 },
    { label: "Normal (2s)", seconds: 2 },
    { label: "Careful (4s)", seconds: 4 },
    { label: "Very careful (8s)", seconds: 8 },
];
const SPACING_DEFAULT_SECONDS = 2; // "Normal" - matches the setting default

function requestSpacingSeconds() {
    const raw = plug_settings.requestSpacing;
    if (raw === undefined || raw === null || raw === "") return SPACING_DEFAULT_SECONDS;
    for (const o of SPACING_OPTIONS) {         // exact label match first
        if (raw === o.label) return o.seconds;
    }
    const idx = parseInt(raw, 10);             // otherwise treat it as an index
    if (!isNaN(idx) && idx >= 0 && idx < SPACING_OPTIONS.length) {
        return SPACING_OPTIONS[idx].seconds;
    }
    return SPACING_DEFAULT_SECONDS;
}

// =============================================================================
// URL helpers
// =============================================================================
function channelUrl(username) { return DOMAIN + "/" + username + "/"; }
function reelUrl(code) { return DOMAIN + "/reel/" + code + "/"; }

function usernameFromUrl(url) {
    const m = /^https?:\/\/(?:www\.)?instagram\.com\/([A-Za-z0-9_.]+)\/?(?:\?.*)?$/.exec(url || "");
    if (!m) return null;
    const name = m[1];
    if (RESERVED.indexOf(name.toLowerCase()) !== -1) return null;
    return name;
}

function codeFromUrl(url) {
    // Matches /reel|reels|p|tv/CODE, optionally prefixed with a username
    // (Instagram emits both instagram.com/reel/CODE and
    // instagram.com/{user}/reel/CODE). Trailing slash / query is ignored.
    const m = /^https?:\/\/(?:www\.)?instagram\.com\/(?:[A-Za-z0-9_.]+\/)?(?:reel|reels|p|tv)\/([A-Za-z0-9_-]+)/.exec(url || "");
    return m ? m[1] : null;
}

// Saved-collection ("playlist") URLs are minted by the plugin. We carry the
// collection name + count in the query string so getPlaylist needs no extra
// request: instagram.com/saved/{collectionId}/?n=<name>&vc=<count>
function savedPlaylistUrl(col) {
    return DOMAIN + "/saved/" + col.id + "/?n=" + encodeURIComponent(col.name || "") +
        "&vc=" + (col.media_count || 0);
}
function collectionIdFromUrl(url) {
    const m = /^https?:\/\/(?:www\.)?instagram\.com\/saved\/([A-Za-z0-9_]+)/.exec(url || "");
    return m ? m[1] : null;
}
function urlParam(url, key) {
    const m = new RegExp("[?&]" + key + "=([^&]*)").exec(url || "");
    return m ? decodeURIComponent(m[1]) : null;
}

// =============================================================================
// Mappers: Instagram JSON -> Grayjay Platform* objects
// =============================================================================
function toUnix(value) {
    if (value === undefined || value === null) return 0;
    if (typeof value === "number") return Math.floor(value > 1e12 ? value / 1000 : value);
    const t = Date.parse(value);
    return isNaN(t) ? 0 : Math.floor(t / 1000);
}

function authorThumb(user) {
    return user.profile_pic_url_hd || user.profile_pic_url || "";
}

function toAuthor(user) {
    const id = new PlatformID(PLATFORM, String(user.pk), config.id);
    const name = user.full_name && user.full_name.length ? user.full_name : ("@" + (user.username || ""));
    return new PlatformAuthorLink(id, name, channelUrl(user.username), authorThumb(user));
}

// Collect thumbnails from a Media (cover + image_versions2 candidates).
function mediaThumbnails(media) {
    const list = [];
    const seen = {};
    function push(u, h) {
        if (!u || seen[u]) return;
        seen[u] = true;
        list.push(new Thumbnail(u, h || 0));
    }
    const iv = media.image_versions2;
    if (iv && iv.candidates) {
        for (let i = 0; i < iv.candidates.length; i++) {
            push(iv.candidates[i].url, iv.candidates[i].height);
        }
    }
    push(media.thumbnail_url, 0);
    if (!list.length) push(authorThumb(media.user || {}), 0);
    return new Thumbnails(list);
}

// Largest known image candidate (used as a proxy for video dimensions).
function bestDimensions(media) {
    let w = 0, h = 0;
    const iv = media.image_versions2;
    if (iv && iv.candidates) {
        for (let i = 0; i < iv.candidates.length; i++) {
            const c = iv.candidates[i];
            if ((c.width || 0) * (c.height || 0) > w * h) { w = c.width || 0; h = c.height || 0; }
        }
    }
    return { width: w, height: h };
}

function isVideoMedia(media) {
    return media && (media.media_type === 2 || !!media.video_url);
}

function mediaTitle(media) {
    const cap = (media.caption_text || media.title || "").trim();
    if (cap) {
        const firstLine = cap.split("\n")[0];
        return firstLine.length > 100 ? firstLine.substring(0, 100) + "..." : firstLine;
    }
    const uname = (media.user && media.user.username) || "";
    return "Reel by @" + uname;
}

function toPlatformVideo(media) {
    return new PlatformVideo({
        id: new PlatformID(PLATFORM, String(media.id || media.pk), config.id),
        name: mediaTitle(media),
        thumbnails: mediaThumbnails(media),
        author: toAuthor(media.user || {}),
        uploadDate: toUnix(media.taken_at),
        duration: Math.round(media.video_duration || 0),
        viewCount: media.play_count || media.view_count || 0,
        url: reelUrl(media.code),
        shareUrl: reelUrl(media.code),
        isLive: false
    });
}

function toPlatformVideoDetails(media) {
    if (!isVideoMedia(media) || !media.video_url) {
        throw new ScriptException("This Instagram post is not a playable video.");
    }
    const dim = bestDimensions(media);
    const source = new VideoUrlSource({
        name: "Source",
        url: media.video_url,
        width: dim.width,
        height: dim.height,
        duration: Math.round(media.video_duration || 0),
        container: "video/mp4",
        codec: "h264",
        bitrate: 0
    });
    return new PlatformVideoDetails({
        id: new PlatformID(PLATFORM, String(media.id || media.pk), config.id),
        name: mediaTitle(media),
        thumbnails: mediaThumbnails(media),
        author: toAuthor(media.user || {}),
        uploadDate: toUnix(media.taken_at),
        duration: Math.round(media.video_duration || 0),
        viewCount: media.play_count || media.view_count || 0,
        url: reelUrl(media.code),
        shareUrl: reelUrl(media.code),
        isLive: false,
        description: media.caption_text || "",
        video: new VideoSourceDescriptor([source]),
        rating: new RatingLikes(media.like_count || 0),
        subtitles: []
    });
}

function toPlatformChannel(user) {
    return new PlatformChannel({
        id: new PlatformID(PLATFORM, String(user.pk), config.id),
        name: user.full_name && user.full_name.length ? user.full_name : ("@" + user.username),
        thumbnail: authorThumb(user),
        banner: null,
        subscribers: user.follower_count || 0,
        description: user.biography || "",
        url: channelUrl(user.username),
        links: {}
    });
}

// Author shown on the saved-collection playlists (the logged-in account).
function savedAuthor() {
    const id = new PlatformID(PLATFORM, "saved", config.id);
    return new PlatformAuthorLink(id, "Saved", DOMAIN + "/", "");
}

// A saved collection -> a Grayjay playlist (search-result form). media_count is
// the collection's TOTAL saved items (the plugin shows only the video ones).
function collectionName(col) {
    if (col.id === ALL_SAVED) return "All saved reels";
    return col.name && col.name.length ? col.name : "Saved";
}
function toPlatformPlaylist(col) {
    const name = collectionName(col);
    return new PlatformPlaylist({
        id: new PlatformID(PLATFORM, String(col.id), config.id),
        name: name,
        thumbnail: col.cover || "",
        author: savedAuthor(),
        datetime: 0,
        url: savedPlaylistUrl({ id: col.id, name: name, media_count: col.media_count }),
        videoCount: col.media_count || -1
    });
}

function toPlatformComment(contextUrl, mediaId, c) {
    // Replies: when the "Load comment replies" setting is ON, replyCount is the
    // TRUE reply count and getSubComments fetches the full thread on demand
    // (one request per expanded comment). When OFF, we only surface the reply
    // previews Instagram inlines for free, so the count matches what we can show.
    const replies = c.replies || [];
    const replyCount = repliesEnabled() ? (c.reply_count || 0) : replies.length;
    return new PlatformComment({
        contextUrl: contextUrl,
        author: toAuthor(c.user || {}),
        message: c.text || "",
        rating: new RatingLikes(c.like_count || 0),
        date: toUnix(c.created_at_utc),
        replyCount: replyCount,
        // context is a Map<String,String> on mobile (Kotlin) - values MUST be
        // strings, so the inline reply previews are JSON-encoded, not stored raw.
        context: {
            commentId: String(c.pk),
            mediaId: mediaId || "",
            contextUrl: contextUrl,
            repliesJson: JSON.stringify(replies)
        }
    });
}

// =============================================================================
// Pagers
// =============================================================================
// Instagram's feeds are MIXED media and this plugin only plays videos, so a
// backend page of 24 items can filter down to 0 renderable ones - very common
// on a new account, whose "cold start" timeline is mostly photos.
//
// A pager that returns 0 items while still claiming hasMore makes Grayjay
// immediately ask for the next page, and the next, forever: an endless
// spinner in the UI and an unbounded burst of real Instagram requests, which
// is exactly what gets an account flagged for automated behaviour.
//
// So: follow the cursor here, server-side, until we have something worth
// showing - and if a few pages in a row yield nothing, report hasMore=false
// and STOP rather than handing Grayjay an empty page to spin on.
const MAX_PAGES_PER_FETCH = 3;   // hard cap on requests per Grayjay page
const MIN_ITEMS_PER_FETCH = 5;   // stop early once we have a usable page

// fetchPage(cursor) -> { items, next_cursor }
function fetchVideoPage(fetchPage, cursor) {
    const videos = [];
    const seenIds = {};
    let next = cursor || "";
    let pages = 0;

    do {
        const page = fetchPage(next) || {};
        next = page.next_cursor || "";
        pages++;
        const items = (page.items || []).filter(isVideoMedia);
        for (const media of items) {
            const id = String(media.id || media.pk || "");
            if (id && seenIds[id]) continue;   // cursors can overlap
            if (id) seenIds[id] = true;
            videos.push(toPlatformVideo(media));
        }
    } while (videos.length < MIN_ITEMS_PER_FETCH && next && pages < MAX_PAGES_PER_FETCH);

    // Nothing playable after several pages: end the pager instead of letting
    // Grayjay spin. The user sees what we have (possibly nothing) and can
    // pull to refresh, rather than the app hammering the backend on its own.
    const hasMore = videos.length > 0 && !!next;
    return { items: videos, cursor: next, hasMore: hasMore };
}

class ReelPager extends VideoPager {
    constructor(username, cursor) {
        const page = fetchVideoPage(c => apiGetUserReels(username, c), cursor);
        super(page.items, page.hasMore, { username: username, cursor: page.cursor });
    }
    nextPage() {
        return new ReelPager(this.context.username, this.context.cursor);
    }
    hasMorePagers() { return this.hasMore; }
}

// Logged-in home timeline. Instagram's feed is mixed media; we keep only the
// playable videos (this plugin can't open photo/carousel posts).
class FeedPager extends VideoPager {
    constructor(cursor) {
        const page = fetchVideoPage(c => apiGetFeed(c), cursor);
        super(page.items, page.hasMore, { cursor: page.cursor });
    }
    nextPage() {
        return new FeedPager(this.context.cursor);
    }
    hasMorePagers() { return this.hasMore; }
}

// Keyword reel search. Mixed media comes back; keep only playable videos.
class SearchReelPager extends VideoPager {
    constructor(query, cursor, sessionId) {
        const page = fetchVideoPage(c => apiSearchReels(query, c, sessionId), cursor);
        super(page.items, page.hasMore, { query: query, cursor: page.cursor, sessionId: sessionId });
    }
    nextPage() {
        return new SearchReelPager(this.context.query, this.context.cursor, this.context.sessionId);
    }
    hasMorePagers() { return this.hasMore; }
}

// Reels saved in one Instagram collection ("playlist"). Videos only.
class SavedReelPager extends VideoPager {
    constructor(collectionId, cursor) {
        const page = fetchVideoPage(c => apiGetSavedReels(collectionId, c), cursor);
        super(page.items, page.hasMore, { collectionId: collectionId, cursor: page.cursor });
    }
    nextPage() {
        return new SavedReelPager(this.context.collectionId, this.context.cursor);
    }
    hasMorePagers() { return this.hasMore; }
}

class IGCommentPager extends CommentPager {
    constructor(contextUrl, mediaId, cursor) {
        const page = apiGetComments(mediaId, cursor);
        const items = (page.items || []).map(function (c) { return toPlatformComment(contextUrl, mediaId, c); });
        super(items, !!page.next_cursor, { contextUrl: contextUrl, mediaId: mediaId, cursor: page.next_cursor || "" });
    }
    nextPage() {
        return new IGCommentPager(this.context.contextUrl, this.context.mediaId, this.context.cursor);
    }
    hasMorePagers() { return this.hasMore; }
}

// On-demand reply thread for a single comment (used when the setting is on).
class IGReplyPager extends CommentPager {
    constructor(contextUrl, mediaId, commentId, cursor) {
        const page = apiGetReplies(mediaId, commentId, cursor);
        const items = (page.items || []).map(function (c) { return toPlatformComment(contextUrl, mediaId, c); });
        super(items, !!page.next_cursor, { contextUrl: contextUrl, mediaId: mediaId, commentId: commentId, cursor: page.next_cursor || "" });
    }
    nextPage() {
        return new IGReplyPager(this.context.contextUrl, this.context.mediaId, this.context.commentId, this.context.cursor);
    }
    hasMorePagers() { return this.hasMore; }
}

// =============================================================================
// Home / Search
// =============================================================================
source.getHome = function (continuationToken) {
    // The backend is logged in, so Home shows the account's real timeline
    // (videos from followed creators). A transient feed/timeline failure
    // must not break the homepage, so fall back to an empty pager.
    try {
        return new FeedPager("");
    } catch (e) {
        if (isRateLimit(e)) throw e;   // never hide a rate-limit as "no videos"
        return new VideoPager([], false, {});
    }
};

source.searchSuggestions = function (query) {
    return [];
};

source.getSearchCapabilities = function () {
    return { types: [Type.Feed.Videos], sorts: [], filters: [] };
};

source.search = function (query, type, order, filters, continuationToken) {
    // Keyword reel search via the backend's GraphQL SERP. A fresh session id
    // per search keeps the paginated pages consistent. A transient failure
    // returns an empty pager rather than throwing the whole search.
    try {
        return new SearchReelPager(query, "", newSessionId());
    } catch (e) {
        if (isRateLimit(e)) throw e;   // never hide a rate-limit as "no results"
        return new VideoPager([], false, {});
    }
};

source.searchChannels = function (query, continuationToken) {
    const users = apiSearchUsers(query) || [];
    const channels = users.map(function (u) {
        return new PlatformChannel({
            id: new PlatformID(PLATFORM, String(u.pk), config.id),
            name: u.full_name && u.full_name.length ? u.full_name : ("@" + u.username),
            thumbnail: authorThumb(u),
            banner: null,
            subscribers: 0,
            description: "",
            url: channelUrl(u.username),
            links: {}
        });
    });
    return new ChannelPager(channels, false, {});
};

// =============================================================================
// Playlists (your Instagram "saved" collections)
// =============================================================================
// Grayjay's search has a Playlists section; we expose your saved collections
// there. "All posts" (ALL_MEDIA_AUTO_COLLECTION) is your full saved feed.
source.searchPlaylists = function (query, type, order, filters, continuationToken) {
    let cols;
    try {
        cols = (apiGetCollections().items) || [];
    } catch (e) {
        return new PlaylistPager([], false, {});
    }
    const q = (query || "").toLowerCase().trim();
    const matched = [];
    let all = null;
    let hasAll = false;
    for (let i = 0; i < cols.length; i++) {
        const c = cols[i];
        if (c.id === ALL_SAVED) all = c;
        // Match on the display name (so "all"/"saved"/"reels" finds the aggregate).
        const name = collectionName(c).toLowerCase();
        if (!q || name.indexOf(q) !== -1) {
            matched.push(c);
            if (c.id === ALL_SAVED) hasAll = true;
        }
    }
    // Always surface "All saved reels" so your saved reels are reachable from
    // any search, even when the query matches no collection name.
    if (!hasAll && all) matched.unshift(all);
    return new PlaylistPager(matched.map(toPlatformPlaylist), false, {});
};

source.isPlaylistUrl = function (url) {
    return collectionIdFromUrl(url) !== null;
};

source.getPlaylist = function (url) {
    const cid = collectionIdFromUrl(url);
    if (!cid) throw new ScriptException("Not an Instagram saved-collection URL: " + url);
    // name + count are carried in the URL (minted in searchPlaylists), so no
    // extra request is needed just to title the playlist.
    const name = urlParam(url, "n") || "Saved";
    const vc = parseInt(urlParam(url, "vc"), 10);
    return new PlatformPlaylistDetails({
        id: new PlatformID(PLATFORM, cid, config.id),
        name: name,
        thumbnail: "",
        author: savedAuthor(),
        datetime: 0,
        url: url,
        videoCount: isNaN(vc) ? -1 : vc,
        contents: new SavedReelPager(cid, "")
    });
};

// =============================================================================
// Channel (creator)
// =============================================================================
source.isChannelUrl = function (url) {
    return usernameFromUrl(url) !== null;
};

// Resolve a username to its numeric pk + basic info via search (private API path).
function findUserByUsername(username) {
    const results = apiSearchUsers(username) || [];
    const lc = username.toLowerCase();
    let found = null;
    for (let i = 0; i < results.length; i++) {
        if ((results[i].username || "").toLowerCase() === lc) { found = results[i]; break; }
    }
    if (!found && results.length) found = results[0];
    return found;
}

source.getChannel = function (url) {
    const username = usernameFromUrl(url);
    if (!username) throw new ScriptException("Not an Instagram profile URL: " + url);

    // Prefer the full profile (bio + follower count) so the About tab is
    // populated. Falls back to the search result, then a minimal channel,
    // so the page never hard-fails.
    let user = null;
    try { user = apiGetUser(username); } catch (e) { user = null; }
    if (!user) {
        try { user = findUserByUsername(username); } catch (e) { user = null; }
    }
    if (!user) {
        return new PlatformChannel({
            id: new PlatformID(PLATFORM, username, config.id),
            name: "@" + username,
            thumbnail: "",
            banner: null,
            subscribers: 0,
            description: "",
            url: channelUrl(username),
            links: {}
        });
    }
    return toPlatformChannel(user);
};

source.getChannelCapabilities = function () {
    return { types: [Type.Feed.Videos], sorts: [], filters: [] };
};

source.getChannelContents = function (url, type, order, filters, continuationToken) {
    const username = usernameFromUrl(url);
    if (!username) throw new ScriptException("Not an Instagram profile URL: " + url);
    return new ReelPager(username, "");
};

// =============================================================================
// Content (Reel) details + comments
// =============================================================================
source.isContentDetailsUrl = function (url) {
    return codeFromUrl(url) !== null;
};

source.getContentDetails = function (url) {
    const code = codeFromUrl(url);
    if (!code) throw new ScriptException("Not an Instagram post/reel URL: " + url);
    const media = apiGetMediaByCode(code);
    return toPlatformVideoDetails(media);
};

source.getComments = function (url, continuationToken) {
    const code = codeFromUrl(url);
    if (!code) throw new ScriptException("Not an Instagram post/reel URL: " + url);
    const media = apiGetMediaByCode(code);
    const mediaId = String(media.id || media.pk);
    return new IGCommentPager(url, mediaId, "");
};

source.getSubComments = function (comment) {
    const ctx = comment.context || {};
    const contextUrl = ctx.contextUrl || comment.contextUrl || "";

    // Setting ON: fetch the full reply thread on demand (one request per
    // expanded comment), with pagination.
    if (repliesEnabled() && ctx.mediaId && ctx.commentId) {
        return new IGReplyPager(contextUrl, ctx.mediaId, ctx.commentId, "");
    }

    // Setting OFF: serve only the reply previews Instagram inlined for free
    // (JSON-encoded into context during getComments) - no additional request.
    let replies = [];
    try { replies = JSON.parse(ctx.repliesJson || "[]"); } catch (e) { replies = []; }
    const items = replies.map(function (c) { return toPlatformComment(contextUrl, ctx.mediaId, c); });
    return new CommentPager(items, false, {});
};

// =============================================================================
// Import subscriptions
// =============================================================================
// Grayjay: Sources -> Instagram -> Import -> Subscriptions. Grayjay calls this,
// gets back the channel URLs of everyone the logged-in account follows, then
// resolves each and lets you pick which to import. (No Grayjay-side login is
// required for this - the backend holds the Instagram session.) We page through
// the backend's /following until it's exhausted, with a hard cap so an
// enormous follow list can't loop forever. Runs synchronously; http.GET is
// blocking in Grayjay, so a plain loop is fine here.
source.getUserSubscriptions = function () {
    const urls = [];
    const seen = {};
    let cursor = "";
    for (let page = 0; page < 200; page++) {   // cap: 200 pages * 200 = 40k
        const res = apiGetFollowing(cursor);
        const items = (res && res.items) || [];
        for (const u of items) {
            if (u && u.username && !seen[u.username]) {
                seen[u.username] = true;
                urls.push(channelUrl(u.username));
            }
        }
        cursor = (res && res.next_cursor) || "";
        if (!cursor) break;
    }
    return urls;
};
