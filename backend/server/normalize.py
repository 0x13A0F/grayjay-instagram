"""Normalize Instagram web/private JSON into the shapes the Grayjay
plugin already consumes (so the plugin stays unchanged).

Target shapes (field names read by plugin/InstagramScript.js mappers):

UserShort: {pk, username, full_name, profile_pic_url}
User:      UserShort + {profile_pic_url_hd, biography, follower_count}
Media:     {pk, id, code, taken_at, media_type, video_url,
            video_duration, view_count, play_count, like_count,
            comment_count, caption_text, thumbnail_url,
            image_versions2: {candidates: [{url, width, height}]},
            user: UserShort}
Comment:   {pk, text, user: UserShort, created_at_utc, like_count}
Page:      {items: [...], next_cursor: str}

Shape notes (verified against real captured responses, see ig-dumps):
- MEDIA always arrives in the private /api/v1 shape, because every media
  endpoint we call is /api/v1 (clips/user, feed/timeline, media/info). So
  norm_media targets that shape directly - no web-GraphQL fallbacks.
- USERS arrive in TWO different shapes: web_profile_info is GraphQL-ish
  (edge_followed_by.count, "id" not "pk") while users/{id}/info is private
  (follower_count, pk). norm_user / norm_user_short handle both - keep them.
"""

from typing import Dict, List, Optional

from utils import deep_get, pk_str


def norm_user_short(u: Optional[Dict]) -> Dict:
    u = u or {}
    return {
        "pk": pk_str(u.get("pk") or u.get("id")),
        "username": u.get("username") or "",
        "full_name": u.get("full_name") or "",
        "profile_pic_url": u.get("profile_pic_url") or "",
        "profile_pic_url_hd": u.get("profile_pic_url_hd")
        or u.get("profile_pic_url") or "",
    }


def norm_user(u: Optional[Dict]) -> Dict:
    u = u or {}
    short = norm_user_short(u)
    followers = (
        u.get("follower_count")
        or deep_get(u, "edge_followed_by", "count")
        or 0
    )
    short.update({
        "biography": u.get("biography") or "",
        "follower_count": int(followers or 0),
        "media_count": int(
            u.get("media_count")
            or deep_get(u, "edge_owner_to_timeline_media", "count")
            or 0
        ),
        "is_private": bool(u.get("is_private")),
    })
    return short


def user_from_web_profile(resp: Optional[Dict]) -> Dict:
    """web_profile_info wraps the user as {data: {user}}; some variants
    return {user} directly. Return the inner user dict (or {})."""
    resp = resp or {}
    return deep_get(resp, "data", "user") or resp.get("user") or {}


def _candidates(media: Dict) -> List[Dict]:
    """Image candidates as [{url, width, height}] from the private
    /api/v1 image_versions2.candidates (present on every media we fetch)."""
    cands = deep_get(media, "image_versions2", "candidates")
    out: List[Dict] = []
    if isinstance(cands, list):
        for c in cands:
            url = c.get("url")
            if url:
                out.append({
                    "url": url,
                    "width": c.get("width") or 0,
                    "height": c.get("height") or 0,
                })
    return out


def _video_url(media: Dict) -> str:
    if media.get("video_url"):
        return media["video_url"]
    vv = media.get("video_versions") or []
    if vv and isinstance(vv, list):
        return vv[0].get("url") or ""
    return ""


def _caption_text(media: Dict) -> str:
    cap = deep_get(media, "caption", "text")
    if cap:
        return cap
    if isinstance(media.get("caption_text"), str):
        return media["caption_text"]
    return ""


def norm_media(media: Optional[Dict]) -> Dict:
    m = media or {}
    user = norm_user_short(m.get("user"))
    pk = pk_str(m.get("pk") or m.get("id"))
    user_pk = user["pk"]
    cands = _candidates(m)
    # Private media has no thumbnail_url; the cover is the first candidate.
    thumb = m.get("thumbnail_url") or (cands[0]["url"] if cands else "")
    return {
        "pk": pk,
        "id": m.get("id") or (f"{pk}_{user_pk}" if pk and user_pk else pk),
        "code": m.get("code") or "",
        "taken_at": m.get("taken_at") or 0,
        "media_type": m.get("media_type") or (2 if _video_url(m) else 1),
        "video_url": _video_url(m),
        "video_duration": m.get("video_duration") or 0,
        "view_count": m.get("view_count") or m.get("play_count") or 0,
        "play_count": m.get("play_count"),
        "like_count": int(m.get("like_count") or 0),
        "comment_count": int(m.get("comment_count") or 0),
        "caption_text": _caption_text(m),
        "thumbnail_url": thumb,
        "image_versions2": {"candidates": cands},
        "user": user,
    }


def _norm_comment_basic(c: Optional[Dict]) -> Dict:
    """A single comment WITHOUT its replies (used for both top-level
    comments and the inline reply previews, avoiding recursion)."""
    c = c or {}
    return {
        "pk": pk_str(c.get("pk") or c.get("id")),
        "text": c.get("text") or "",
        "user": norm_user_short(c.get("user")),
        "created_at_utc": c.get("created_at_utc") or c.get("created_at") or 0,
        "like_count": int(
            c.get("comment_like_count") or c.get("like_count") or 0
        ),
    }


def norm_comment(c: Optional[Dict]) -> Dict:
    """Top-level comment + any reply previews Instagram already inlines in
    the comments response (preview_child_comments). reply_count is the TRUE
    number of replies; `replies` holds only the previews IG sent for free
    (expanding the rest would need a separate request)."""
    c = c or {}
    out = _norm_comment_basic(c)
    previews = c.get("preview_child_comments") or []
    out["reply_count"] = int(c.get("child_comment_count") or 0)
    out["replies"] = [_norm_comment_basic(r) for r in previews]
    return out


# ---- list / page helpers ---------------------------------------------------

def norm_search_users(resp: Dict) -> List[Dict]:
    """fbsearch/account_serp -> [UserShort]. Entries in resp.users[] are the
    user objects directly; topsearch instead nests them under .user, so we
    handle both. Note: search results carry NO follower_count."""
    out = []
    for entry in (resp or {}).get("users") or []:
        user = entry.get("user") if isinstance(entry, dict) else None
        out.append(norm_user_short(user or entry))
    return out


def norm_keyword_search_page(resp: Dict) -> Dict:
    """GraphQL keyword search (xdt_fbsearch__top_serp_graphql) ->
    {items:[Media], next_cursor}. The SERP is a list of edges of mixed
    __typename; media live in the grid units' node.items[]. Pagination is
    page_info.{has_next_page, end_cursor}."""
    root = deep_get(resp, "data", "xdt_fbsearch__top_serp_graphql") or {}
    items = []
    for edge in root.get("edges") or []:
        node = (edge or {}).get("node") or {}
        for m in node.get("items") or []:
            items.append(norm_media(m))
    page = root.get("page_info") or {}
    nxt = page.get("end_cursor") if page.get("has_next_page") else ""
    return {"items": items, "next_cursor": nxt or ""}


def norm_collections(resp: Dict) -> Dict:
    """GraphQL saved-collections list
    (xdt_api__v1__collections__list_graphql_connection) -> {items:[Collection]}.
    Each Collection: {id, name, media_count, cover}. The AUDIO auto-collection
    is dropped (saved audio, not reels). Returned as a page for symmetry."""
    conn = deep_get(
        resp, "data", "xdt_api__v1__collections__list_graphql_connection") or {}
    out = []
    for edge in conn.get("edges") or []:
        node = (edge or {}).get("node") or {}
        cid = node.get("collection_id")
        if not cid or cid == "AUDIO_AUTO_COLLECTION":
            continue
        cover = node.get("cover_media") or (
            (node.get("cover_media_list") or [None])[0])
        out.append({
            "id": str(cid),
            "name": node.get("collection_name") or "",
            "media_count": int(node.get("collection_media_count") or 0),
            "cover": norm_media(cover).get("thumbnail_url") if cover else "",
        })
    page = conn.get("page_info") or {}
    nxt = page.get("end_cursor") if page.get("has_next_page") else ""
    return {"items": out, "next_cursor": nxt or ""}


def norm_saved_feed(resp: Dict) -> Dict:
    """/api/v1/feed/saved/posts/ and /api/v1/feed/collection/{id}/posts/ ->
    {items:[Media], next_cursor}. items[].media; next_max_id + more_available."""
    resp = resp or {}
    items = []
    for entry in resp.get("items") or []:
        media = entry.get("media") if isinstance(entry, dict) else None
        items.append(norm_media(media or entry))
    nxt = resp.get("next_max_id") if resp.get("more_available") else ""
    return {"items": items, "next_cursor": nxt or ""}


def norm_clips_page(resp: Dict) -> Dict:
    """/api/v1/clips/user/ -> {items:[Media], next_cursor}.
    items[].media, paging_info.max_id / more_available."""
    items = []
    for entry in (resp or {}).get("items") or []:
        media = entry.get("media") if isinstance(entry, dict) else None
        items.append(norm_media(media or entry))
    paging = (resp or {}).get("paging_info") or {}
    nxt = paging.get("max_id") if paging.get("more_available") else ""
    return {"items": items, "next_cursor": nxt or ""}


def norm_comments_page(resp: Dict) -> Dict:
    """/api/v1/media/{pk}/comments/ -> {items:[Comment], next_cursor}."""
    items = [norm_comment(c) for c in (resp or {}).get("comments") or []]
    nxt = (resp or {}).get("next_min_id") or ""
    if isinstance(nxt, dict):  # sometimes wrapped
        nxt = nxt.get("cursor") or ""
    return {"items": items, "next_cursor": nxt or ""}


def norm_timeline_page(resp: Dict) -> Dict:
    """/api/v1/feed/timeline/ -> {items:[Media], next_cursor}.
    feed_items[] is mixed: media posts wrapped in media_or_ad, plus
    non-media entries (suggested_users, ads, etc.) which are skipped."""
    resp = resp or {}
    entries = resp.get("feed_items")
    if entries is None:
        entries = resp.get("items") or []
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        media = entry.get("media_or_ad") or entry.get("media")
        if not media and ("pk" in entry or "id" in entry):
            media = entry  # already a bare media object
        if media:
            items.append(norm_media(media))
    nxt = resp.get("next_max_id") if resp.get("more_available") else ""
    return {"items": items, "next_cursor": nxt or ""}


def norm_child_comments_page(resp: Dict) -> Dict:
    """/api/v1/media/{pk}/comments/{comment_id}/child_comments/
    -> {items:[Comment], next_cursor}. Replies are flat (no further nesting).

    The web endpoint paginates by HEAD: has_more_head_child_comments +
    next_min_child_cursor (sent back as min_id). The tail/max_id variant
    is a fallback in case Instagram returns that shape instead."""
    resp = resp or {}
    items = [_norm_comment_basic(c) for c in resp.get("child_comments") or []]
    nxt = ""
    if resp.get("has_more_head_child_comments"):
        nxt = resp.get("next_min_child_cursor") or ""
    elif resp.get("has_more_tail_child_comments"):
        nxt = resp.get("next_max_child_cursor") or ""
    return {"items": items, "next_cursor": nxt or ""}


def norm_media_info(resp: Dict) -> Dict:
    """/api/v1/media/{pk}/info/ -> Media (resp.items[0])."""
    items = (resp or {}).get("items") or []
    return norm_media(items[0] if items else {})
