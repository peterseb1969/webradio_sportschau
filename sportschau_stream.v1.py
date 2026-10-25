#!/usr/bin/env python3
"""
sportschau_stream.py — extract ARD Sportschau audio stream URLs.

Two strategies:

  1. Page mode (default): fetch a sportschau.de match/audiostream page,
     derive its ~player.html variant, scan raw HTML + embedded JSON
     for stream URLs (.m3u8 / .mp3 / .aac / ard-mcdn hosts).

       python3 sportschau_stream.py "https://www.sportschau.de/fussball/bundesliga/...,audiostream-...-104.html"

  2. Probe mode: ignore pages entirely and probe the fixed CDN slot
     channels (liga1spiel1..N etc.) discovered on ard-mcdn.de for a
     live playlist. Heuristic: playlist filenames are guessed.

       python3 sportschau_stream.py --probe

Add --mpv to print a ready-to-run mpv command for the best hit.
Stdlib only, no dependencies.
"""

import argparse
import html as htmllib
import json
import re
import sys
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MPV_FLAGS = ("--cache=yes --cache-secs=900 --demuxer-max-bytes=300MiB "
             "--demuxer-max-back-bytes=300MiB --force-seekable=yes")

# URL-ish patterns that indicate a stream
RE_STREAM = re.compile(
    r'https?://[^\s"\'<>\\]+?\.(?:m3u8|mp3|aac)(?:\?[^\s"\'<>\\]*)?',
    re.IGNORECASE)
RE_MCDN = re.compile(
    r'https?://[^\s"\'<>\\]*ard-mcdn\.de[^\s"\'<>\\]*', re.IGNORECASE)
RE_JSON_SCRIPT = re.compile(
    r'<script[^>]*type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)


def fetch(url: str, timeout: int = 15) -> tuple[bytes, str]:
    """GET url, return (body, content_type). Raises on network errors."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": "https://www.sportschau.de/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        # playlists/pages are small; cap defensively at 5 MB
        return resp.read(5 * 1024 * 1024), ctype


def derive_player_url(page_url: str) -> str | None:
    """https://.../<slug>,audiostream-x-104.html -> .../audiostream-x-104~player.html"""
    if "~player.html" in page_url:
        return page_url
    m = re.match(r"^(.*/)[^/]*?,([^,/]+)\.html(?:[?#].*)?$", page_url)
    if m:
        return f"{m.group(1)}{m.group(2)}~player.html"
    return None


def unescape_blob(text: str) -> str:
    """Undo the usual JSON/HTML escapings so URL regexes can match."""
    text = htmllib.unescape(text)
    text = text.replace("\\/", "/")
    text = re.sub(r"\\u002[fF]", "/", text)
    return text


def walk_json(node, hits: set):
    """Recursively collect stream-looking strings from parsed JSON."""
    if isinstance(node, dict):
        for v in node.values():
            walk_json(v, hits)
    elif isinstance(node, list):
        for v in node:
            walk_json(v, hits)
    elif isinstance(node, str):
        for rx in (RE_STREAM, RE_MCDN):
            hits.update(rx.findall(node))


def scan_html(raw: str) -> set:
    """All stream-URL candidates found anywhere in a page's raw HTML."""
    hits: set = set()
    text = unescape_blob(raw)
    hits.update(RE_STREAM.findall(text))
    hits.update(RE_MCDN.findall(text))
    for blob in RE_JSON_SCRIPT.findall(raw):
        try:
            walk_json(json.loads(htmllib.unescape(blob)), hits)
        except (json.JSONDecodeError, ValueError):
            hits.update(RE_STREAM.findall(unescape_blob(blob)))
    return hits


def looks_like_playlist(body: bytes, ctype: str) -> bool:
    return (b"#EXTM3U" in body[:64]
            or "mpegurl" in ctype.lower()
            or ctype.lower().startswith("audio/"))


def verify(url: str) -> bool:
    """True if the URL currently serves a playlist/audio."""
    try:
        body, ctype = fetch(url)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False
    return looks_like_playlist(body, ctype)


def rank(url: str) -> int:
    """Lower = better. Prefer masters over seglists, m3u8 over raw files."""
    u = url.lower()
    score = 50
    if "seglist" in u:
        score += 20
    if any(k in u for k in ("master", "manifest", "index", "playlist")):
        score -= 20
    if ".m3u8" in u:
        score -= 5
    if "_hls_skip" in u:
        score += 5
    return score


def strip_llhls(url: str) -> str:
    """Remove LL-HLS delta parameters like _HLS_skip=YES."""
    url = re.sub(r"([?&])_HLS_[^=&]+=[^&]*&?", r"\1", url)
    return url.rstrip("?&")


def page_mode(page_url: str) -> list[str]:
    candidates: set = set()
    targets = []
    player = derive_player_url(page_url)
    if player:
        targets.append(player)
    targets.append(page_url)
    for t in targets:
        print(f"[*] fetching {t}", file=sys.stderr)
        try:
            body, _ = fetch(t)
        except Exception as e:
            print(f"[!]   failed: {e}", file=sys.stderr)
            continue
        found = scan_html(body.decode("utf-8", errors="replace"))
        print(f"[*]   {len(found)} candidate(s) in raw HTML/JSON", file=sys.stderr)
        candidates.update(found)
        if found:
            break  # player page hit is enough
    return sorted({strip_llhls(u) for u in candidates}, key=rank)


def probe_mode() -> list[str]:
    """No page needed: probe the fixed ard-mcdn slot channels."""
    base = "https://sportschau-radio.ard-mcdn.de/sportschau/radio"
    slots = ([f"liga1spiel{i}" for i in range(1, 11)]
             + [f"liga2spiel{i}" for i in range(1, 11)]
             + ["konferenz", "liga1konferenz", "liga2konferenz"])
    names = ["master.m3u8", "manifest.m3u8", "index.m3u8", "playlist.m3u8"]
    live = []
    for slot in slots:
        for name in names:
            for path in (f"{base}/{slot}/hls/{name}",
                         f"{base}/{slot}/hls/192/{name}"):
                try:
                    body, ctype = fetch(path, timeout=6)
                except Exception:
                    continue
                if looks_like_playlist(body, ctype):
                    print(f"[+] LIVE: {path}", file=sys.stderr)
                    live.append(path)
                    break  # next slot, one hit is enough
            else:
                continue
            break
    return live


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract sportschau audio stream URLs")
    ap.add_argument("url", nargs="?", help="sportschau.de match/audiostream page URL")
    ap.add_argument("--probe", action="store_true",
                    help="skip pages, probe ard-mcdn slot channels (heuristic)")
    ap.add_argument("--mpv", action="store_true",
                    help="print a ready mpv command for the best hit")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip live-checking the found URLs")
    args = ap.parse_args()

    if args.probe:
        results = probe_mode()
    elif args.url:
        results = page_mode(args.url)
        if not args.no_verify and results:
            print("[*] verifying candidates...", file=sys.stderr)
            verified = [u for u in results if verify(u)]
            if verified:
                results = verified
            else:
                print("[!] none verified live (pre-match? expired?), "
                      "showing unverified candidates", file=sys.stderr)
    else:
        ap.print_help()
        return 2

    if not results:
        print("[!] nothing found. If the match is live, the config is likely "
              "fetched from a separate API endpoint not present in the HTML — "
              "use browser DevTools (Network tab, filter: m3u8) and note that "
              "request's URL.", file=sys.stderr)
        return 1

    for u in results:
        print(u)
    if args.mpv:
        print()
        print(f'mpv {MPV_FLAGS} "{results[0]}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
