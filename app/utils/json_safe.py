"""Bounded JSON parsing for routes accepting bodies from untrusted callers.

Round 5 A4-M4: Python's ``json`` module uses recursive descent and is
bounded only by sys.getrecursionlimit() (~1000). For public endpoints
that don't need deep structures (search query, support ticket,
internal report), we cap nesting at a low value and refuse deeper
payloads outright. Pairs naturally with a Flask ``MAX_CONTENT_LENGTH``
per-route cap.
"""
import json


_DEFAULT_MAX_DEPTH = 20


class JsonTooDeepError(ValueError):
    pass


def _depth(obj, current: int, limit: int) -> int:
    if current > limit:
        raise JsonTooDeepError(f"JSON nesting exceeds {limit}")
    if isinstance(obj, dict):
        return max(
            (_depth(v, current + 1, limit) for v in obj.values()),
            default=current,
        )
    if isinstance(obj, list):
        return max(
            (_depth(v, current + 1, limit) for v in obj),
            default=current,
        )
    return current


def parse_json_bounded(raw: bytes | str, max_depth: int = _DEFAULT_MAX_DEPTH):
    """Parse raw JSON bytes and refuse deeply-nested structures.

    Returns the decoded object or raises ``ValueError`` /
    ``JsonTooDeepError``. Callers should treat both as "bad request".
    """
    obj = json.loads(raw)
    _depth(obj, 0, max_depth)
    return obj
