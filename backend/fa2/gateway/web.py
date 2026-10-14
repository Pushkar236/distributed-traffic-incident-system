"""Distributed web-based system features: HTTP caching with validators.

GET /api/fa2/web/feed is a public, cacheable resource (current incident feed). It is served
with   ETag: "<sha256 of the body>"   and   Cache-Control: public, max-age=5.
A client (browser, proxy or CDN edge) that already holds a copy revalidates with
If-None-Match; when nothing changed the server answers 304 Not Modified with no body.
This is how web caches and CDNs (see the Cloudflare case study) cut bandwidth and load.
"""
import hashlib
import json


class WebCacheDemo:
    def __init__(self):
        self.stats = {"requests": 0, "full_responses": 0, "not_modified": 0, "bytes_sent": 0, "bytes_saved": 0}

    def respond(self, feed, if_none_match):
        body = json.dumps(feed, sort_keys=True, default=str).encode()
        etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        self.stats["requests"] += 1
        if if_none_match and etag in [t.strip() for t in if_none_match.split(",")]:
            self.stats["not_modified"] += 1
            self.stats["bytes_saved"] += len(body)
            return 304, etag, b""
        self.stats["full_responses"] += 1
        self.stats["bytes_sent"] += len(body)
        return 200, etag, body
