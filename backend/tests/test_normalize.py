"""Unit tests for normalize.py against realistic IG JSON shapes.

Run from the backend/ dir (PYTHONPATH points at the code in server/):
    PYTHONPATH=server python3 -m tests.test_normalize
Asserts the normalized output uses the exact field names the Grayjay
plugin reads (pk, code, video_url, taken_at, image_versions2.candidates,
follower_count, next_cursor, ...).
"""

import normalize as N

passed = 0


def check(name, cond):
    global passed
    assert cond, "FAILED: " + name
    passed += 1
    print("  ok:", name)


# ---- /api/v1/clips/user/ (private-API media shape) -------------------------
CLIPS = {
    "items": [
        {"media": {
            "pk": 3872733397261451832,
            "id": "3872733397261451832_528817151",
            "code": "DW-toGVj4I4",
            "taken_at": 1744350028,
            "media_type": 2,
            "video_versions": [
                {"url": "https://cdn.example/v.mp4", "width": 720,
                 "height": 1280}
            ],
            "image_versions2": {"candidates": [
                {"url": "https://cdn.example/c1.jpg", "width": 480,
                 "height": 853}
            ]},
            "caption": {"text": "Hello world\nsecond"},
            "like_count": 1243539,
            "play_count": None,
            "view_count": 6981519,
            "comment_count": 20,
            "video_duration": 83.0,
            "user": {"pk": 528817151, "username": "nasa",
                     "full_name": "NASA",
                     "profile_pic_url": "https://cdn.example/n.jpg"},
        }}
    ],
    "paging_info": {"max_id": "CURSOR2", "more_available": True},
}

print("clips/user -> reels page:")
page = N.norm_clips_page(CLIPS)
m = page["items"][0]
check("next_cursor from max_id", page["next_cursor"] == "CURSOR2")
check("pk", m["pk"] == "3872733397261451832")
check("id", m["id"] == "3872733397261451832_528817151")
check("code", m["code"] == "DW-toGVj4I4")
check("taken_at unix", m["taken_at"] == 1744350028)
check("video_url from video_versions", m["video_url"]
      == "https://cdn.example/v.mp4")
check("video_duration", m["video_duration"] == 83.0)
check("viewCount falls to view_count", m["view_count"] == 6981519)
check("like_count", m["like_count"] == 1243539)
check("caption_text from caption.text",
      m["caption_text"] == "Hello world\nsecond")
check("candidate url", m["image_versions2"]["candidates"][0]["url"]
      == "https://cdn.example/c1.jpg")
check("candidate dims", m["image_versions2"]["candidates"][0]["width"] == 480)
check("thumbnail falls back to candidate",
      m["thumbnail_url"] == "https://cdn.example/c1.jpg")
check("author pk", m["user"]["pk"] == "528817151")
check("author name", m["user"]["full_name"] == "NASA")

# ---- web profile info (web GraphQL-ish user) -------------------------------
WEB_PROFILE = {"user": {
    "id": "528817151", "username": "nasa", "full_name": "NASA",
    "biography": "We explore space",
    "profile_pic_url": "https://cdn.example/n.jpg",
    "profile_pic_url_hd": "https://cdn.example/nhd.jpg",
    "edge_followed_by": {"count": 95000000},
    "is_private": False,
}}

print("web_profile_info -> user:")
u = N.norm_user(WEB_PROFILE["user"])
check("pk from id", u["pk"] == "528817151")
check("follower_count from edge_followed_by",
      u["follower_count"] == 95000000)
check("biography", u["biography"] == "We explore space")
check("profile_pic_url_hd", u["profile_pic_url_hd"]
      == "https://cdn.example/nhd.jpg")

# ---- private users/{pk}/info (follower_count direct) -----------------------
print("users/{pk}/info -> user:")
u2 = N.norm_user({"pk": "1", "username": "x", "full_name": "X",
                  "follower_count": 42,
                  "profile_pic_url": "https://cdn.example/x.jpg"})
check("follower_count direct", u2["follower_count"] == 42)

# ---- topsearch -> search users --------------------------------------------
SEARCH = {"users": [
    {"user": {"pk": 528817151, "username": "nasa", "full_name": "NASA",
              "profile_pic_url": "https://cdn.example/n.jpg",
              "is_verified": True}}
]}
print("topsearch -> [UserShort]:")
us = N.norm_search_users(SEARCH)
check("one result", len(us) == 1)
check("search pk", us[0]["pk"] == "528817151")
check("search username", us[0]["username"] == "nasa")

# ---- comments -------------------------------------------------------------
COMMENTS = {"comments": [
    {"pk": 18099703079286113, "text": "nice shot!",
     "user": {"pk": 5, "username": "bob", "full_name": "Bob",
              "profile_pic_url": "https://cdn.example/b.jpg"},
     "created_at_utc": 1744360000, "comment_like_count": 3,
     "child_comment_count": 4,
     "preview_child_comments": [
         {"pk": 18099703079286120, "text": "agreed!",
          "user": {"pk": 6, "username": "ann"},
          "created_at_utc": 1744360100, "comment_like_count": 1}
     ]}
], "next_min_id": "CM2"}
print("comments -> page:")
cp = N.norm_comments_page(COMMENTS)
c = cp["items"][0]
check("comment next_cursor", cp["next_cursor"] == "CM2")
check("comment message", c["text"] == "nice shot!")
check("comment date", c["created_at_utc"] == 1744360000)
check("comment like from comment_like_count", c["like_count"] == 3)
check("comment author", c["user"]["username"] == "bob")
check("reply_count from child_comment_count", c["reply_count"] == 4)
check("inline reply preview present", len(c["replies"]) == 1)
check("reply text", c["replies"][0]["text"] == "agreed!")
check("reply author", c["replies"][0]["user"]["username"] == "ann")
check("reply has no nested replies key", "replies" not in c["replies"][0])

# ---- child comments (on-demand reply thread) ------------------------------
CHILD = {"child_comments": [
    {"pk": 17973866334108128, "text": "first reply",
     "user": {"pk": 7, "username": "carl"},
     "created_at_utc": 1781633597, "comment_like_count": 0},
    {"pk": 17961995802128811, "text": "second reply",
     "user": {"pk": 8, "username": "dana"},
     "created_at_utc": 1781634782, "comment_like_count": 6},
], "has_more_head_child_comments": True, "next_min_child_cursor": "CHILD2"}
print("child_comments -> page:")
chp = N.norm_child_comments_page(CHILD)
check("child items count", len(chp["items"]) == 2)
check("child next_cursor from min cursor", chp["next_cursor"] == "CHILD2")
check("child reply text", chp["items"][0]["text"] == "first reply")
check("child reply like", chp["items"][1]["like_count"] == 6)
check("child has no replies key", "replies" not in chp["items"][0])
check("child no next_cursor when no more",
      N.norm_child_comments_page({"child_comments": []})["next_cursor"] == "")
check("child tail/max_id fallback",
      N.norm_child_comments_page({
          "child_comments": [],
          "has_more_tail_child_comments": True,
          "next_max_child_cursor": "TAIL2"
          })["next_cursor"] == "TAIL2")

# ---- timeline feed --------------------------------------------------------
TIMELINE = {"feed_items": [
    {"media_or_ad": {"pk": 111, "id": "111_5", "code": "AAA", "media_type": 2,
                     "video_versions": [{"url": "https://cdn.example/a.mp4"}],
                     "user": {"pk": 5, "username": "bob"}}},
    {"suggested_users": {"some": "thing"}},   # non-media -> skipped
    {"media_or_ad": {"pk": 222, "id": "222_6", "code": "BBB", "media_type": 1,
                     "user": {"pk": 6, "username": "ann"}}},
], "more_available": True, "next_max_id": "FEED2"}
print("timeline -> page:")
tp = N.norm_timeline_page(TIMELINE)
check("timeline skips non-media", len(tp["items"]) == 2)
check("timeline next_cursor", tp["next_cursor"] == "FEED2")
check("timeline media_or_ad unwrapped", tp["items"][0]["code"] == "AAA")
check("timeline video_url", tp["items"][0]["video_url"]
      == "https://cdn.example/a.mp4")
_no_more = N.norm_timeline_page({"feed_items": [], "next_max_id": "X"})
check("timeline no cursor when not more_available",
      _no_more["next_cursor"] == "")

# ---- keyword reel search (GraphQL SERP) -----------------------------------
SEARCH_REELS = {"data": {"xdt_fbsearch__top_serp_graphql": {
    "edges": [
        {"node": {"__typename": "XDTTopSerpHeaderUnit"}},          # skipped
        {"node": {"__typename": "XDTTopSerpAccountsHCMUnit"}},      # skipped
        {"node": {"__typename": "XDTTopSerpMediaGridUnit", "items": [
            {"pk": 1, "id": "1_5", "code": "RA", "media_type": 2,
             "video_versions": [{"url": "https://cdn.example/r.mp4"}],
             "user": {"pk": 5, "username": "bob"}},
            {"pk": 2, "id": "2_6", "code": "RB", "media_type": 1,
             "user": {"pk": 6, "username": "ann"}},
        ]}},
    ],
    "page_info": {"has_next_page": True, "end_cursor": "SERP2"},
}}}
print("keyword search -> page:")
sp = N.norm_keyword_search_page(SEARCH_REELS)
check("search grid items extracted", len(sp["items"]) == 2)
check("search skips header/account units", sp["items"][0]["code"] == "RA")
check("search video_url", sp["items"][0]["video_url"]
      == "https://cdn.example/r.mp4")
check("search next_cursor from end_cursor", sp["next_cursor"] == "SERP2")
check("search no cursor when no next page", N.norm_keyword_search_page(
    {"data": {"xdt_fbsearch__top_serp_graphql": {"edges": [],
     "page_info": {"has_next_page": False, "end_cursor": "X"}}}}
)["next_cursor"] == "")

# ---- saved collections (GraphQL) ------------------------------------------
COLLECTIONS = {"data": {"xdt_api__v1__collections__list_graphql_connection": {
    "edges": [
        {"node": {"collection_id": "ALL_MEDIA_AUTO_COLLECTION",
                  "collection_name": "All posts", "collection_media_count": 1015,
                  "cover_media": {"pk": 9, "code": "CV", "media_type": 2,
                                  "image_versions2": {"candidates": [
                                      {"url": "https://cdn.example/cover.jpg"}]},
                                  "user": {"pk": 1, "username": "me"}}}},
        {"node": {"collection_id": "17966305454427872",
                  "collection_name": "Cooking", "collection_media_count": 267,
                  "cover_media_list": [{"pk": 8, "code": "CK",
                                        "image_versions2": {"candidates": [
                                            {"url": "https://cdn.example/c.jpg"}]},
                                        "user": {"pk": 1}}]}},
        {"node": {"collection_id": "AUDIO_AUTO_COLLECTION",   # dropped
                  "collection_name": "Audio"}},
    ],
    "page_info": {"has_next_page": False, "end_cursor": "X"},
}}}
print("saved collections -> page:")
cl = N.norm_collections(COLLECTIONS)
check("collections drops AUDIO", len(cl["items"]) == 2)
check("collection id", cl["items"][0]["id"] == "ALL_MEDIA_AUTO_COLLECTION")
check("collection name", cl["items"][1]["name"] == "Cooking")
check("collection media_count", cl["items"][0]["media_count"] == 1015)
check("collection cover from cover_media", cl["items"][0]["cover"]
      == "https://cdn.example/cover.jpg")
check("collection cover from cover_media_list", cl["items"][1]["cover"]
      == "https://cdn.example/c.jpg")

# ---- saved feed (REST) ----------------------------------------------------
SAVED = {"items": [
    {"media": {"pk": 1, "id": "1_5", "code": "SA", "media_type": 2,
               "video_versions": [{"url": "https://cdn.example/s.mp4"}],
               "user": {"pk": 5, "username": "bob"}}},
], "more_available": True, "next_max_id": "SAVED2"}
print("saved feed -> page:")
sv = N.norm_saved_feed(SAVED)
check("saved feed item unwrapped from media", sv["items"][0]["code"] == "SA")
check("saved feed video_url", sv["items"][0]["video_url"]
      == "https://cdn.example/s.mp4")
check("saved feed next_cursor", sv["next_cursor"] == "SAVED2")
check("saved feed no cursor when not more", N.norm_saved_feed(
    {"items": [], "next_max_id": "Y"})["next_cursor"] == "")

# ---- media/{pk}/info ------------------------------------------------------
print("media/{pk}/info -> Media:")
INFO = {"items": [CLIPS["items"][0]["media"]]}
mi = N.norm_media_info(INFO)
check("media info video_url", mi["video_url"] == "https://cdn.example/v.mp4")
check("media info is video", mi["media_type"] == 2)

print("web_profile_info wrapped in {data:{user}}:")
WRAPPED = {"data": {"user": WEB_PROFILE["user"]}, "status": "ok"}
uw = N.norm_user(N.user_from_web_profile(WRAPPED))
check("unwraps data.user -> pk", uw["pk"] == "528817151")
check("unwraps data.user -> followers", uw["follower_count"] == 95000000)
check("unwrap falls back to bare {user}",
      N.user_from_web_profile(WEB_PROFILE)["username"] == "nasa")

print(f"\nALL {passed} CHECKS PASSED")
