/*
 * Tests for the plugin's paging + request behaviour.  Run:  node plugin/test_pagers.js
 *
 * Instagram feeds are mixed media and this plugin only plays videos, so a
 * backend page can filter down to zero renderable items.  A pager that returns
 * an empty page while still claiming hasMore makes Grayjay request the next
 * page immediately, forever - an endless spinner and an unbounded burst of
 * real Instagram traffic, which is what gets an account flagged.  These tests
 * pin that behaviour down, plus the request-spacing header and 429 handling.
 *
 * The Grayjay runtime globals are stubbed here; only InstagramScript.js is
 * under test.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let passed = 0;
function check(name, cond) {
    if (!cond) { console.error("  FAILED: " + name); process.exitCode = 1; return; }
    passed++;
    console.log("  ok: " + name);
}

// --- fake Grayjay runtime -----------------------------------------------
function makeSandbox(responder) {
    const requests = [];
    class VideoPager {
        constructor(results, hasMore, context) {
            this.results = results; this.hasMore = hasMore; this.context = context;
        }
    }
    const sandbox = {
        console,
        source: {},
        VideoPager,
        CommentPager: VideoPager,
        ChannelPager: VideoPager,
        PlaylistPager: VideoPager,
        PlatformVideo: function (o) { Object.assign(this, o); },
        PlatformID: function (p, v, c) { this.value = v; },
        PlatformAuthorLink: function () {},
        Thumbnails: function (l) { this.list = l; },
        Thumbnail: function () {},
        VideoUrlSource: function (o) { Object.assign(this, o); },
        UnMuxVideoSourceDescriptor: function () {},
        VideoSourceDescriptor: function () {},
        PlatformVideoDetails: function (o) { Object.assign(this, o); },
        PlatformComment: function () {},
        PlatformPlaylist: function () {},
        PlatformPlaylistDetails: function () {},
        PlatformChannel: function () {},
        ScriptException: function (msg) { this.message = msg; this.name = "ScriptException"; },
        Type: { Order: {}, Feed: {} },
        http: {
            GET: function (url, headers) {
                requests.push({ url: url, headers: headers });
                return responder(url, headers, requests.length);
            }
        },
    };
    sandbox.global = sandbox;
    vm.createContext(sandbox);
    const code = fs.readFileSync(path.join(__dirname, "InstagramScript.js"), "utf8");
    vm.runInContext(code, sandbox);
    sandbox.requests = requests;
    return sandbox;
}

function ok(body) { return { isOk: true, code: 200, body: JSON.stringify(body) }; }
function photo(id) { return { id: String(id), pk: String(id), media_type: 1, code: "c" + id, user: { username: "u" }, taken_at: 0 }; }
function video(id) { return { id: String(id), pk: String(id), media_type: 2, video_url: "http://v/" + id, code: "c" + id, user: { username: "u" }, taken_at: 0 }; }

// Serves N pages, each built by pageFn(index). Cursor is the page index.
function feedServer(pageFn) {
    return function (url) {
        const m = /cursor=([^&]*)/.exec(url);
        const idx = m && m[1] ? parseInt(decodeURIComponent(m[1]), 10) : 0;
        return ok(pageFn(idx));
    };
}

// --- tests ---------------------------------------------------------------
console.log("empty-page guard (the runaway-pagination fix)");
{
    // Every page is photos, forever - the cold-start feed of a new account.
    const s = makeSandbox(feedServer(i => ({ items: [photo(i * 2), photo(i * 2 + 1)], next_cursor: String(i + 1) })));
    const pager = s.source.getHome();
    check("gives up instead of paging forever", s.requests.length <= 3);
    check("reports no more pages, so Grayjay stops asking", pager.hasMore === false);
    check("returns an empty (not broken) page", Array.isArray(pager.results) && pager.results.length === 0);
}
{
    // Endless photos AND an endless cursor: the exact runaway shape.
    const s = makeSandbox(feedServer(i => ({ items: [photo(i)], next_cursor: "c" + (i + 1) })));
    const before = s.requests.length;
    s.source.getHome();
    check("a single Grayjay page costs at most 3 backend calls", s.requests.length - before <= 3);
}

console.log("accumulating across sparse pages");
{
    // One video per page: keep going until we have something worth showing.
    const s = makeSandbox(feedServer(i => ({ items: [video(i * 10), photo(i * 10 + 1)], next_cursor: String(i + 1) })));
    const pager = s.source.getHome();
    check("follows the cursor to collect videos", pager.results.length >= 2);
    check("stops at the per-page request cap", s.requests.length === 3);
    check("still has more to give", pager.hasMore === true);
}
{
    // A full page of videos: must not waste a second request.
    const items = []; for (let i = 0; i < 8; i++) items.push(video(i));
    const s = makeSandbox(feedServer(() => ({ items: items, next_cursor: "next" })));
    const pager = s.source.getHome();
    check("a good first page costs exactly one request", s.requests.length === 1);
    check("all videos are returned", pager.results.length === 8);
    check("paging continues normally", pager.hasMore === true);
}
{
    // End of the feed: videos, but no cursor.
    const s = makeSandbox(feedServer(() => ({ items: [video(1), video(2)], next_cursor: "" })));
    const pager = s.source.getHome();
    check("no cursor ends the pager", pager.hasMore === false);
    check("the last items are still returned", pager.results.length === 2);
    check("no wasted request at the end", s.requests.length === 1);
}
{
    // Overlapping cursors (Instagram does repeat items across pages).
    const s = makeSandbox(feedServer(i => ({ items: [video(1), video(2), photo(3)], next_cursor: String(i + 1) })));
    const pager = s.source.getHome();
    const ids = pager.results.map(v => v.id.value);
    check("duplicate media are not shown twice", new Set(ids).size === ids.length);
}

console.log("request spacing");
{
    const s = makeSandbox(feedServer(() => ({ items: [video(1)], next_cursor: "" })));
    s.source.enable({}, {}, "");
    s.source.getHome();
    check("every request carries the spacing header",
        s.requests.every(r => r.headers["X-Min-Interval"] !== undefined));
    check("default spacing is the 'Normal' 2s", s.requests[0].headers["X-Min-Interval"] === "2");
}
{
    const s = makeSandbox(feedServer(() => ({ items: [video(1)], next_cursor: "" })));
    s.source.enable({}, { requestSpacing: "3" }, "");   // dropdown index -> "Very careful"
    s.source.getHome();
    check("the setting is honoured (by index)", s.requests[0].headers["X-Min-Interval"] === "8");
}
{
    const s = makeSandbox(feedServer(() => ({ items: [video(1)], next_cursor: "" })));
    s.source.enable({}, { requestSpacing: "Careful (4s)" }, "");
    s.source.getHome();
    check("the setting is honoured (by label)", s.requests[0].headers["X-Min-Interval"] === "4");
}
{
    const s = makeSandbox(feedServer(() => ({ items: [video(1)], next_cursor: "" })));
    s.source.enable({}, { requestSpacing: "nonsense" }, "");
    s.source.getHome();
    check("a bad setting value falls back to the default", s.requests[0].headers["X-Min-Interval"] === "2");
}

console.log("rate-limit handling");
{
    const s = makeSandbox(() => ({ isOk: false, code: 429, body: "rate limited" }));
    let threw = null;
    try { s.source.getHome(); } catch (e) { threw = e; }
    check("a 429 is never retried", s.requests.length === 1);
    // Home/search swallow transient errors into an empty pager; a rate-limit
    // must NOT be hidden that way, or the user just sees a blank feed and
    // keeps pulling to refresh - making it worse.
    check("a rate-limit surfaces to the user", threw !== null && /rate-limiting/.test(threw.message));
}
{
    const s = makeSandbox(() => ({ isOk: false, code: 429, body: "rate limited" }));
    let threw = null;
    try { s.source.search("q", null, null, null, null); } catch (e) { threw = e; }
    check("search surfaces a rate-limit too", threw !== null && /rate-limiting/.test(threw.message));
}
{
    const s = makeSandbox(() => ({ isOk: false, code: 500, body: "boom" }));
    let pager = null;
    try { pager = s.source.getHome(); } catch (e) { /* nothing */ }
    check("a non-rate-limit failure still falls back to an empty home",
        pager !== null && pager.hasMore === false);
}
{
    const s = makeSandbox(() => ({ isOk: false, code: 500, body: "boom" }));
    try { s.source.getHome(); } catch (e) { /* expected */ }
    check("a transient 5xx still retries", s.requests.length === 3);
}

console.log("\n" + passed + " checks passed");
