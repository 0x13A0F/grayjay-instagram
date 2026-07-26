from typing import Any

# ---- Generic helpers ----


def pk_str(value: Any) -> str:
    """Coerce an id to a string and strip any "{pk}_{userid}" suffix,
    returning just the pk part. None -> "".

    Instagram ids come in both forms ("123" and "123_456"); this keeps the
    media/user pk. Used by the normalizers and by media-id route params.
    """
    if value is None:
        return ""
    s = str(value).strip()
    return s.split("_")[0] if "_" in s else s


def deep_get(obj: Any, *keys: Any, default: Any = None) -> Any:
    """Walk nested dicts/lists by key/index, returning `default` if any step
    is missing. Integer keys index lists (negative indices allowed)."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int) and \
                -len(cur) <= k < len(cur):
            cur = cur[k]
        else:
            return default
    return cur if cur is not None else default


def clip_text(text: str, n: int = 180) -> str:
    """Collapse to a single line and truncate - used for error snippets."""
    text = (text or "").strip().replace("\n", " ")
    return text[:n]


# ---- Instagram shortcode <-> media pk codec
# A reel/post URL (instagram.com/reel/CBJu3Kis4vq/) encodes the media pk in the
# shortcode using a url-safe base64 alphabet. We need the pk to call
# /api/v1/media/{pk}/info/.

_SHORTCODE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789-_"
)
_SHORTCODE_INDEX = {c: i for i, c in enumerate(_SHORTCODE_ALPHABET)}


def shortcode_to_pk(shortcode: str) -> int:
    """Decode an Instagram shortcode to its numeric media pk."""
    pk = 0
    for ch in shortcode:
        # Stop at the first char outside the alphabet (some codes carry
        # a trailing suffix that is not part of the pk).
        if ch not in _SHORTCODE_INDEX:
            break
        pk = pk * 64 + _SHORTCODE_INDEX[ch]
    return pk


def pk_to_shortcode(pk: int) -> str:
    """Encode a numeric media pk back to its shortcode (inverse of
    shortcode_to_pk; used to round-trip-test the decoder)."""
    if pk == 0:
        return _SHORTCODE_ALPHABET[0]
    chars = []
    while pk > 0:
        pk, rem = divmod(pk, 64)
        chars.append(_SHORTCODE_ALPHABET[rem])
    return "".join(reversed(chars))
