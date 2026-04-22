"""Tiny static + YouTube-search proxy server for Musically.

Endpoints:
  GET /                  -> serves index.html and other static files
  GET /api/search?q=     -> scrapes YouTube search results, returns JSON
                            { items: [{ videoId, title, artist, thumb, durationSecs }] }
  GET /api/artist?name=  -> returns one good artist photo (channel avatar if found,
                            otherwise the first music video thumbnail)
                            { thumb: str|null }
No API key required.
"""
import json
import re
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

YT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=YT_HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _yt_initial_data(html: str):
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html)
    if not m:
        m = re.search(r"ytInitialData\s*=\s*(\{.*?\});", html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def parse_duration(text: str) -> int:
    if not text:
        return 0
    parts = text.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    return parts[0]


def yt_search(query: str, limit: int = 25):
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
    data = _yt_initial_data(_fetch(url))
    if not data:
        return []
    results = []
    try:
        sections = (
            data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]
            ["sectionListRenderer"]["contents"]
        )
    except (KeyError, TypeError):
        return []
    for section in sections:
        items = section.get("itemSectionRenderer", {}).get("contents", [])
        for item in items:
            v = item.get("videoRenderer")
            if not v:
                continue
            vid = v.get("videoId")
            title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", [])).strip()
            artist = "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", [])).strip()
            thumbs = v.get("thumbnail", {}).get("thumbnails", [])
            thumb = thumbs[-1]["url"] if thumbs else ""
            length_text = v.get("lengthText", {}).get("simpleText", "")
            if not vid or not title:
                continue
            results.append({
                "videoId": vid,
                "title": title,
                "artist": artist or "Unknown",
                "thumb": thumb,
                "durationSecs": parse_duration(length_text),
            })
            if len(results) >= limit:
                return results
    return results


def yt_artist_photo(name: str):
    """Try to find a channel avatar for an artist. Falls back to a video thumbnail."""
    url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote(name + " official")
        + "&sp=EgIQAg%253D%253D"  # filter: channels
    )
    try:
        data = _yt_initial_data(_fetch(url))
    except Exception:
        data = None
    if data:
        try:
            sections = (
                data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]
                ["sectionListRenderer"]["contents"]
            )
            for section in sections:
                for item in section.get("itemSectionRenderer", {}).get("contents", []):
                    ch = item.get("channelRenderer")
                    if not ch:
                        continue
                    thumbs = ch.get("thumbnail", {}).get("thumbnails", [])
                    if thumbs:
                        t = thumbs[-1]["url"]
                        if t.startswith("//"):
                            t = "https:" + t
                        return t
        except (KeyError, TypeError):
            pass
    # Fallback: first music video thumbnail
    items = yt_search(name + " official music video", limit=1)
    if items:
        return items[0]["thumb"]
    return None


class Handler(SimpleHTTPRequestHandler):
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/search":
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q", [""])[0] or "").strip()
            if not q:
                self._json({"items": []})
                return
            try:
                self._json({"items": yt_search(q)})
            except Exception as e:
                self._json({"items": [], "error": str(e)}, status=502)
            return
        if parsed.path == "/api/img":
            qs = urllib.parse.parse_qs(parsed.query)
            target = (qs.get("u", [""])[0] or "").strip()
            if not target.startswith("https://") or "googleusercontent.com" not in target and "ytimg.com" not in target:
                self.send_response(400); self.end_headers(); return
            try:
                req = urllib.request.Request(target, headers=YT_HEADERS)
                with urllib.request.urlopen(req, timeout=10) as r:
                    body = r.read()
                    ctype = r.headers.get("Content-Type", "image/jpeg")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_response(502); self.end_headers()
            return
        if parsed.path == "/api/artist":
            qs = urllib.parse.parse_qs(parsed.query)
            name = (qs.get("name", [""])[0] or "").strip()
            if not name:
                self._json({"thumb": None})
                return
            try:
                self._json({"thumb": yt_artist_photo(name)})
            except Exception as e:
                self._json({"thumb": None, "error": str(e)}, status=502)
            return
        return super().do_GET()

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def main():
    port = 5000
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Musically server running on http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
