#!/usr/bin/env python3
"""Fetch today's sports games from ESPN and generate an HTML dashboard."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
import yaml

SCRIPT_DIR = Path(__file__).parent
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
STREAMED_API = "https://streamed.pk/api/matches/all-today"
STREAMED_WATCH = "https://streamed.pk/watch"

SPORT_SLUG_MAP = {
    "basketball": "basketball",
    "football": "american-football",
    "baseball": "baseball",
    "hockey": "hockey",
    "soccer": "football",
    "racing": "motor-sports",
}

SPORT_ICON = {
    "basketball": "🏀",
    "football":   "🏈",
    "baseball":   "⚾",
    "hockey":     "🏒",
    "soccer":     "⚽",
    "racing":     "🏎",
}

ESPN_STANDINGS_URL = {
    "nfl": "https://www.espn.com/nfl/standings",
    "nba": "https://www.espn.com/nba/standings",
    "mlb": "https://www.espn.com/mlb/standings",
    "nhl": "https://www.espn.com/nhl/standings",
    "mens-college-basketball": "https://www.espn.com/mens-college-basketball/standings",
    "college-football": "https://www.espn.com/college-football/standings",
    "cfl": "https://www.espn.com/cfl/standings",
}

# All available ESPN leagues — superset of what's in config
ALL_LEAGUES = [
    # US pro
    ("nfl", "football", "nfl", "NFL"),
    ("nba", "basketball", "nba", "NBA"),
    ("mlb", "baseball", "mlb", "MLB"),
    ("nhl", "hockey", "nhl", "NHL"),
    ("wnba", "basketball", "wnba", "WNBA"),
    ("mls", "soccer", "usa.1", "MLS"),
    ("cfl", "football", "cfl", "CFL"),
    ("ufl", "football", "ufl", "UFL"),
    # College
    ("mens-college-basketball", "basketball", "mens-college-basketball", "NCAAM"),
    ("womens-college-basketball", "basketball", "womens-college-basketball", "NCAAW"),
    ("college-football", "football", "college-football", "NCAAF"),
    # Soccer
    ("eng.1", "soccer", "eng.1", "EPL"),
    ("esp.1", "soccer", "esp.1", "La Liga"),
    ("ger.1", "soccer", "ger.1", "Bundesliga"),
    ("ita.1", "soccer", "ita.1", "Serie A"),
    ("fra.1", "soccer", "fra.1", "Ligue 1"),
    ("aut.1", "soccer", "aut.1", "Austrian BL"),
    ("uefa.champions", "soccer", "uefa.champions", "UCL"),
    ("uefa.europa", "soccer", "uefa.europa", "Europa"),
    ("fifa.friendly", "soccer", "fifa.friendly", "Intl Friendly"),
    ("fifa.worldq.uefa", "soccer", "fifa.worldq.uefa", "WC Qual UEFA"),
    ("fifa.world", "soccer", "fifa.world", "World Cup"),
    ("uefa.euro", "soccer", "uefa.euro", "Euros"),
    ("uefa.nations", "soccer", "uefa.nations", "Nations League"),
    # Motorsport
    ("f1", "racing", "f1", "F1"),
]


def load_config():
    config_path = SCRIPT_DIR / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def collect_leagues(config):
    """Return deduplicated set of (league_key, sport, league) tuples to fetch."""
    seen = set()
    result = []
    for rule in config["rules"]:
        lk = rule["league"]
        if lk not in seen:
            seen.add(lk)
            mapping = config["league_map"].get(lk)
            if mapping:
                result.append((lk, mapping["sport"], mapping["league"]))
    return result


def fetch_scoreboard(sport, league, date_str):
    """Fetch ESPN scoreboard for a sport/league on a given date."""
    url = ESPN_SCOREBOARD.format(sport=sport, league=league)
    try:
        resp = requests.get(url, params={"dates": date_str}, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  Warning: failed to fetch {sport}/{league}: {e}", file=sys.stderr)
        return None


def fetch_teams_for_league(sport, league_key, league_path):
    """Fetch all teams for a league from ESPN teams endpoint."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league_path}/teams"
    try:
        resp = requests.get(url, params={"limit": 200}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        teams = []
        for item in data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
            t = item.get("team", {})
            abbr = t.get("abbreviation", "")
            name = t.get("displayName", t.get("name", abbr))
            logo = ""
            logos = t.get("logos", [])
            if logos:
                logo = logos[0].get("href", "")
            if abbr:
                teams.append({
                    "abbr": abbr,
                    "name": name,
                    "logo": logo,
                    "league": league_key,
                    "leagueName": "",  # filled in by caller
                })
        return teams
    except Exception:
        return []


def fetch_all_teams(all_leagues):
    """Fetch teams for all non-international, non-F1 leagues in parallel."""
    # Skip leagues where team lists aren't meaningful
    skip = {"f1", "fifa.friendly", "fifa.worldq.uefa", "fifa.world",
            "uefa.euro", "uefa.nations", "uefa.champions", "uefa.europa"}
    to_fetch = [(lk, sport, league, display)
                for lk, sport, league, display in all_leagues
                if lk not in skip]

    results = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_teams_for_league, sport, lk, league): (lk, display)
                   for lk, sport, league, display in to_fetch}
        for f in as_completed(futures):
            lk, display = futures[f]
            teams = f.result()
            for t in teams:
                t["leagueName"] = display
            results[lk] = teams

    # Merge all, deduplicate by abbr+league
    seen = set()
    merged = []
    for lk, display, *_ in [(lk, display) for lk, _, _, display in to_fetch]:
        for t in results.get(lk, []):
            key = t["abbr"] + ":" + t["league"]
            if key not in seen:
                seen.add(key)
                merged.append(t)
    return merged


def fetch_streams():
    """Fetch today's streams from streamed.su API."""
    try:
        resp = requests.get(STREAMED_API, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  Warning: failed to fetch streams: {e}", file=sys.stderr)
        return []


def match_stream(game, streams):
    """Try to find a matching stream for a game. Returns stream URL or None."""
    if game.get("is_f1"):
        gp = game.get("gp_name", "").lower()
        for s in streams:
            cat = (s.get("category") or "").lower()
            if cat != "motor-sports":
                continue
            title = (s.get("title") or "").lower()
            # Must contain "f1" or "formula 1", and match the GP location
            if "f1" not in title and "formula 1" not in title:
                continue
            gp_words = [w for w in gp.split() if len(w) > 3]
            if any(word in title for word in gp_words):
                return _stream_url(s)
        return None

    home = game.get("home_name", "").lower()
    away = game.get("away_name", "").lower()
    if not home or not away:
        return None

    def normalize(name):
        parts = name.lower().split()
        return parts[-1] if parts else ""

    home_nick = normalize(home)
    away_nick = normalize(away)

    for s in streams:
        title = (s.get("title") or "").lower()
        teams = s.get("teams") or {}
        t_home = ((teams.get("home") or {}).get("name") or "").lower()
        t_away = ((teams.get("away") or {}).get("name") or "").lower()

        if t_home and t_away:
            if (home_nick in t_home or home_nick in t_away) and \
               (away_nick in t_home or away_nick in t_away):
                return _stream_url(s)

        if home_nick in title and away_nick in title:
            return _stream_url(s)

    return None


def _stream_url(stream_obj):
    """Build a watch URL from a stream object."""
    slug = stream_obj.get("id", "")
    if slug:
        return f"{STREAMED_WATCH}/{slug}"
    return None


def _streamed_match_id(stream_url):
    try:
        parsed = urlparse(stream_url or "")
    except Exception:
        return None
    if parsed.netloc not in ("streamed.pk", "www.streamed.pk"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] != "watch":
        return None
    return parts[1]


def parse_curl_command(curl_cmd):
    """Extract URL (and headers, if present) from a curl command."""
    if not curl_cmd or not curl_cmd.strip():
        raise ValueError("Empty cURL command")

    try:
        tokens = shlex.split(curl_cmd)
    except Exception as exc:
        raise ValueError(f"Invalid cURL syntax: {exc}") from exc

    if not tokens:
        raise ValueError("Empty cURL command")

    if tokens[0] == "curl":
        tokens = tokens[1:]

    url = None
    headers = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header") and i + 1 < len(tokens):
            raw = tokens[i + 1]
            if ":" in raw:
                k, v = raw.split(":", 1)
                headers[k.strip()] = v.strip()
            i += 2
            continue
        if t in ("--url",) and i + 1 < len(tokens):
            url = tokens[i + 1]
            i += 2
            continue
        if t.startswith("http://") or t.startswith("https://"):
            url = t
        i += 1

    if not url:
        raise ValueError("No URL found in cURL command")

    return {"url": url, "headers": headers}


def _viewer_count_from_row(text):
    nums = re.findall(r"\d[\d,]*", text or "")
    vals = []
    for n in nums:
        try:
            vals.append(int(n.replace(",", "")))
        except Exception:
            pass
    return max(vals) if vals else 0


def resolve_best_stream_urls(
    stream_urls,
    timeout_sec=5,
    settle_ms=1200,
    max_matches=None,
    verbose=False,
):
    """
    For streamed.pk watch links, choose the highest-viewer concrete stream link.
    Returns mapping: match_id -> best_stream_url
    """
    match_ids = []
    seen = set()
    for u in stream_urls:
        mid = _streamed_match_id(u)
        if mid and mid not in seen:
            seen.add(mid)
            match_ids.append(mid)
    if not match_ids:
        return {}
    if max_matches is not None:
        match_ids = match_ids[: max(0, int(max_matches))]

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {}

    results = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path="/usr/bin/chromium-browser",
                args=["--no-sandbox"],
            )
            page = browser.new_page()

            total = len(match_ids)
            for idx, mid in enumerate(match_ids, start=1):
                if verbose:
                    print(f"    [best-stream] {idx}/{total}: {mid}", flush=True)
                page_url = f"{STREAMED_WATCH}/{mid}"
                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=int(timeout_sec * 1000))
                    page.wait_for_timeout(max(0, int(settle_ms)))
                    rows = page.evaluate(
                        """
                        () => {
                          const out = [];
                          const links = [...document.querySelectorAll('a[href*="/watch/"]')];
                          for (const a of links) {
                            const href = a.getAttribute('href') || '';
                            const txt = (a.innerText || '').replace(/\\s+/g, ' ').trim();
                            out.push({ href, text: txt });
                          }
                          return out;
                        }
                        """
                    )
                except Exception:
                    continue

                ranked = []
                for row in rows or []:
                    href = row.get("href") or ""
                    full = urljoin(page_url, href)
                    if f"/watch/{mid}/" not in full:
                        continue
                    ranked.append((_viewer_count_from_row(row.get("text", "")), full))

                if ranked:
                    ranked.sort(key=lambda x: x[0], reverse=True)
                    results[mid] = ranked[0][1]

            browser.close()
    except Exception:
        return {}

    return results


def _infer_content_type(url):
    lowered = (url or "").lower()
    if ".m3u8" in lowered:
        return "application/x-mpegURL"
    if ".mpd" in lowered:
        return "application/dash+xml"
    return "video/mp4"


def _content_type_candidates(url):
    primary = _infer_content_type(url)
    candidates = [primary]
    # Some receivers are picky; these fallbacks improve compatibility.
    if primary == "application/x-mpegURL":
        candidates.extend(["application/vnd.apple.mpegurl", "video/mp2t"])
    candidates.extend(["video/mp4", "application/octet-stream"])
    seen = set()
    uniq = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _is_direct_media_url(url):
    lowered = (url or "").lower()
    if any(x in lowered for x in (".m3u8", ".mpd", ".mp4", ".m4v", ".webm", ".ts.m3u8")):
        return True
    if "streamed.pk/watch/" in lowered:
        return False
    return False


class CastManager:
    """Resolve stream URLs and cast them to a Chromecast device."""

    def __init__(
        self,
        device_name,
        resolve_timeout=20,
        source_probe_limit=4,
        use_browser_rank=True,
        browser_timeout=10,
        browser_executable="/usr/bin/chromium-browser",
        use_browser_extract=True,
        browser_extract_limit=3,
    ):
        self.device_name = device_name
        self.resolve_timeout = resolve_timeout
        self.source_probe_limit = source_probe_limit
        self.use_browser_rank = use_browser_rank
        self.browser_timeout = browser_timeout
        self.browser_executable = browser_executable
        self.use_browser_extract = use_browser_extract
        self.browser_extract_limit = browser_extract_limit
        self._lock = threading.Lock()
        self._cc = None

    def _is_streamed_watch_url(self, url):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        if parsed.netloc not in ("streamed.pk", "www.streamed.pk"):
            return None
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2 or parts[0] != "watch":
            return None
        match_id = parts[1]
        source = parts[2] if len(parts) >= 3 else None
        stream_no = parts[3] if len(parts) >= 4 else None
        return {
            "match_id": match_id,
            "source": source,
            "stream_no": stream_no,
        }

    def _extract_embedsports_url(self, html_text):
        m = re.search(r"https://embedsports\.top/embed/[^\"'<>\\s]+", html_text)
        if not m:
            return None
        url = m.group(0)
        # Invalid placeholders look like /embed/admin//4 and should be ignored.
        if re.search(r"/embed/[^/]+//\d+$", url):
            return None
        return url

    def _extract_iframe_srcs(self, base_url, html_text):
        srcs = []
        for src in re.findall(r"<iframe[^>]+src=['\"]([^'\"]+)['\"]", html_text, flags=re.I):
            full = urljoin(base_url, src)
            if full not in srcs:
                srcs.append(full)
        return srcs

    def _discover_streamed_source_pages(self, match_id, preferred_source=None):
        pages = []
        try:
            all_today = requests.get(STREAMED_API, timeout=self.resolve_timeout).json()
        except Exception:
            return pages

        match = None
        for item in all_today:
            if item.get("id") == match_id:
                match = item
                break
        if not match:
            return pages

        sources = [s.get("source") for s in (match.get("sources") or []) if s.get("source")]
        if preferred_source and preferred_source in sources:
            sources.remove(preferred_source)
            sources.insert(0, preferred_source)

        for source in sources:
            for idx in range(1, self.source_probe_limit + 1):
                watch_url = f"{STREAMED_WATCH}/{match_id}/{source}/{idx}"
                try:
                    resp = requests.get(watch_url, timeout=self.resolve_timeout)
                except Exception:
                    continue
                if resp.status_code != 200:
                    continue
                embed_url = self._extract_embedsports_url(resp.text)
                if embed_url:
                    if watch_url not in pages:
                        pages.append(watch_url)
        return pages

    def _discover_streamed_pages_via_browser(self, match_id):
        """Use a real browser render to collect stream links and viewer counts."""
        if not self.use_browser_rank:
            return []

        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return []

        page_url = f"{STREAMED_WATCH}/{match_id}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    executable_path=self.browser_executable,
                    args=["--no-sandbox"],
                )
                page = browser.new_page()
                page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=int(self.browser_timeout * 1000),
                )
                page.wait_for_timeout(3000)
                rows = page.evaluate(
                    """
                    () => {
                      const out = [];
                      const links = [...document.querySelectorAll('a[href*="/watch/"]')];
                      for (const a of links) {
                        const href = a.getAttribute('href') || '';
                        const txt = (a.innerText || '').replace(/\\s+/g, ' ').trim();
                        out.push({ href, text: txt });
                      }
                      return out;
                    }
                    """
                )
                browser.close()
        except Exception:
            return []

        ranked = []
        seen = set()

        def parse_viewers(text):
            nums = re.findall(r"\d[\d,]*", text or "")
            vals = []
            for n in nums:
                try:
                    vals.append(int(n.replace(",", "")))
                except Exception:
                    pass
            return max(vals) if vals else 0

        for r in rows or []:
            href = r.get("href") or ""
            if not href:
                continue
            full = urljoin(page_url, href)
            parsed = self._is_streamed_watch_url(full)
            if not parsed or parsed.get("match_id") != match_id:
                continue
            if full in seen:
                continue
            seen.add(full)
            ranked.append((parse_viewers(r.get("text", "")), full))

        ranked.sort(key=lambda x: x[0], reverse=True)
        return [u for _, u in ranked]

    def _extract_media_urls_via_browser(self, page_url):
        """Open a player page and capture manifest URLs from network activity."""
        if not self.use_browser_extract:
            return []
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return []

        found = []

        def maybe_add(url):
            lowered = (url or "").lower()
            if any(x in lowered for x in (".m3u8", ".mpd", "/playlist", "manifest")):
                if url not in found:
                    found.append(url)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    executable_path=self.browser_executable,
                    args=["--no-sandbox"],
                )
                page = browser.new_page()
                page.on("request", lambda req: maybe_add(req.url))
                page.on("response", lambda resp: maybe_add(resp.url))
                page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=int(self.browser_timeout * 1000),
                )
                page.wait_for_timeout(3000)

                for sel in (
                    'button[aria-label*="Play" i]',
                    'button[title*="Play" i]',
                    ".jw-icon-playback",
                    ".vjs-big-play-button",
                    "video",
                ):
                    loc = page.locator(sel)
                    if loc.count() > 0:
                        try:
                            loc.first.click(timeout=1500)
                            break
                        except Exception:
                            pass

                page.evaluate(
                    """
                    () => {
                      const v = document.querySelector('video');
                      if (v) {
                        v.muted = true;
                        v.play().catch(() => {});
                      }
                    }
                    """
                )
                page.wait_for_timeout(10000)
                browser.close()
        except Exception:
            return []

        return found

    def _expand_streamed_candidates(self, stream_url):
        """Expand a Streamed watch URL into source-specific candidate pages."""
        info = self._is_streamed_watch_url(stream_url)
        if not info:
            return [stream_url], "non-streamed URL"

        match_id = info["match_id"]
        source = info["source"]
        candidates = []
        # If this is already a specific source/stream URL, try it first.
        if source and info.get("stream_no"):
            candidates.append(stream_url)

        browser_ranked = self._discover_streamed_pages_via_browser(match_id)
        for url in browser_ranked:
            if url not in candidates:
                candidates.append(url)

        discovered = self._discover_streamed_source_pages(match_id, preferred_source=source)
        for url in discovered:
            if url not in candidates:
                candidates.append(url)

        # Keep original URL as a final fallback.
        if stream_url not in candidates:
            candidates.append(stream_url)

        note = (
            f"browser-ranked {len(browser_ranked)} stream link(s); "
            f"api-discovered {len(discovered)} source page(s)"
        )
        return candidates, note

    def _resolve_media_urls(self, stream_url):
        """Resolve a stream URL into one or more likely playable URLs."""
        candidate_pages, source_note = self._expand_streamed_candidates(stream_url)

        resolved = []
        debug_notes = [source_note]

        # First add page-level derived URLs (embed and nested iframe URLs).
        for page_url in candidate_pages:
            if page_url not in resolved:
                resolved.append(page_url)
            try:
                page = requests.get(page_url, timeout=self.resolve_timeout)
                if page.status_code != 200:
                    continue
                embed_url = self._extract_embedsports_url(page.text)
                if embed_url and embed_url not in resolved:
                    resolved.append(embed_url)
                    try:
                        embed_page = requests.get(
                            embed_url,
                            timeout=self.resolve_timeout,
                            headers={"Referer": page_url},
                        )
                        if embed_page.status_code == 200:
                            for nested in self._extract_iframe_srcs(embed_url, embed_page.text):
                                if nested not in resolved:
                                    resolved.append(nested)
                    except Exception:
                        pass
            except Exception:
                continue

        # Browser extraction: capture manifest URLs from top ranked candidates.
        extracted_total = 0
        for page_url in candidate_pages[: self.browser_extract_limit]:
            for media_url in self._extract_media_urls_via_browser(page_url):
                if media_url not in resolved:
                    resolved.append(media_url)
                    extracted_total += 1
        debug_notes.append(f"browser-extracted {extracted_total} media URL(s)")

        # Then try yt-dlp on each candidate URL and append direct media URLs it finds.
        ytdlp = shutil.which("yt-dlp")
        if not ytdlp:
            return resolved[:12], "; ".join(debug_notes + ["yt-dlp not found"])

        ytdlp_hits = 0
        for candidate in list(resolved):
            cmd = [
                ytdlp,
                "-J",
                "--no-playlist",
                "--no-warnings",
                candidate,
            ]
            try:
                out = subprocess.check_output(
                    cmd,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=self.resolve_timeout,
                )
                info = json.loads(out)
            except Exception:
                continue

            direct = []
            if isinstance(info.get("url"), str):
                direct.append(info["url"])
            for fmt in (info.get("formats") or []):
                u = fmt.get("url")
                if isinstance(u, str):
                    direct.append(u)
            for u in direct:
                if u and u not in resolved:
                    resolved.append(u)
                    ytdlp_hits += 1

        debug_notes.append(f"yt-dlp discovered {ytdlp_hits} direct URL(s)")
        return resolved[:20], "; ".join(debug_notes)

    def _get_chromecast(self):
        """Connect/reconnect to the configured Chromecast."""
        try:
            import pychromecast
        except ImportError as exc:
            raise RuntimeError(
                "pychromecast is not installed. Install it with: pip install pychromecast"
            ) from exc

        if self._cc is not None:
            return self._cc

        chromecasts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=[self.device_name]
        )
        if not chromecasts:
            if browser:
                pychromecast.discovery.stop_discovery(browser)
            raise RuntimeError(f"Chromecast '{self.device_name}' not found")
        self._cc = chromecasts[0]
        self._cc.wait(timeout=15)
        if browser:
            pychromecast.discovery.stop_discovery(browser)
        return self._cc

    def cast(self, stream_url, title):
        if not stream_url:
            raise RuntimeError("Missing stream URL")

        with self._lock:
            media_urls, resolution_note = self._resolve_media_urls(stream_url)
            media_urls = sorted(
                media_urls,
                key=lambda u: (not _is_direct_media_url(u), len(u)),
            )

            cc = self._get_chromecast()
            mc = cc.media_controller

            def wait_for_stable(timeout_sec=10):
                deadline = time.time() + timeout_sec
                last_state_local = "UNKNOWN"
                while time.time() < deadline:
                    mc.update_status()
                    status = mc.status
                    state = (status.player_state if status else "") or "UNKNOWN"
                    last_state_local = state
                    if state in ("PLAYING", "BUFFERING", "PAUSED"):
                        return True, state
                    time.sleep(0.6)
                return False, last_state_local

            def verify_not_idle(duration_sec=10):
                deadline = time.time() + duration_sec
                last = "UNKNOWN"
                saw_playing = False
                while time.time() < deadline:
                    mc.update_status()
                    status = mc.status
                    state = (status.player_state if status else "") or "UNKNOWN"
                    last = state
                    if state == "PLAYING":
                        saw_playing = True
                    if state in ("IDLE", "UNKNOWN"):
                        return False, state, saw_playing
                    time.sleep(0.6)
                # Avoid reporting success when receiver is stuck buffering forever.
                return saw_playing, last, saw_playing

            attempt_notes = []
            last_state = "UNKNOWN"
            chosen = None

            for media_url in media_urls:
                for content_type in _content_type_candidates(media_url):
                    try:
                        mc.play_media(
                            media_url,
                            content_type,
                            title=title or "Sports Stream",
                            stream_type="LIVE",
                            autoplay=True,
                        )
                        mc.block_until_active(timeout=8)
                        stable, state = wait_for_stable(timeout_sec=8)
                        last_state = state
                        if stable:
                            verified, vstate, saw_playing = verify_not_idle(duration_sec=10)
                            last_state = vstate
                            if verified:
                                chosen = (media_url, content_type)
                                break
                            attempt_notes.append(
                                f"{content_type} -> no sustained PLAYING ({vstate}, playing={saw_playing})"
                            )
                        attempt_notes.append(
                            f"{content_type} -> {state}"
                        )
                    except Exception as exc:
                        attempt_notes.append(f"{content_type} -> error: {exc}")
                if chosen:
                    break

            if not chosen:
                hint = ""
                if "streamed.pk/watch" in stream_url and "yt-dlp not found" in resolution_note:
                    hint = " Install yt-dlp so watch pages can be resolved to direct media URLs."
                detail = "; ".join(attempt_notes[-4:]) if attempt_notes else "no attempts recorded"
                raise RuntimeError(
                    f"Cast did not become stable (player_state={last_state}). {resolution_note}. {detail}.{hint}"
                )

            media_url, content_type = chosen
            return {
                "media_url": media_url,
                "content_type": content_type,
                "resolution_note": resolution_note,
                "player_state": last_state,
            }

    def cast_from_curl(self, curl_cmd, title):
        parsed = parse_curl_command(curl_cmd)
        # Chromecast receiver can't use arbitrary request headers directly;
        # we cast the extracted media URL.
        result = self.cast(parsed["url"], title)
        result["parsed_header_count"] = len(parsed["headers"])
        return result


class CastRequestHandler(BaseHTTPRequestHandler):
    """Tiny local API used by the dashboard cast button."""

    server_version = "SportsCast/1.0"

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send_json(200, {"ok": True})

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"ok": True, "status": "ready"})
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        route = self.path.rstrip("/")
        if route not in ("/cast", "/cast-curl"):
            self._send_json(404, {"ok": False, "error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "Invalid JSON payload"})
            return

        title = (payload.get("title") or "Sports Stream").strip()

        try:
            if route == "/cast-curl":
                curl_cmd = (payload.get("curl_cmd") or "").strip()
                if not curl_cmd:
                    self._send_json(400, {"ok": False, "error": "curl_cmd is required"})
                    return
                result = self.server.cast_manager.cast_from_curl(curl_cmd, title)  # type: ignore[attr-defined]
            else:
                stream_url = (payload.get("stream_url") or "").strip()
                if not stream_url:
                    self._send_json(400, {"ok": False, "error": "stream_url is required"})
                    return
                result = self.server.cast_manager.cast(stream_url, title)  # type: ignore[attr-defined]
            self._send_json(200, {"ok": True, "result": result})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def log_message(self, fmt, *args):
        # Keep terminal output clean; cast status is printed by caller.
        return


def run_cast_server(config):
    cast_cfg = config.get("cast", {})
    device_name = cast_cfg.get("device_name", "").strip()
    if not device_name:
        raise RuntimeError("config.cast.device_name is required for cast server")

    host = cast_cfg.get("server_host", "127.0.0.1")
    port = int(cast_cfg.get("server_port", 8765))
    resolve_timeout = int(cast_cfg.get("resolve_timeout", 20))
    source_probe_limit = int(cast_cfg.get("source_probe_limit", 4))
    use_browser_rank = bool(cast_cfg.get("use_browser_rank", True))
    browser_timeout = int(cast_cfg.get("browser_timeout", 10))
    browser_executable = cast_cfg.get("browser_executable", "/usr/bin/chromium-browser")
    use_browser_extract = bool(cast_cfg.get("use_browser_extract", True))
    browser_extract_limit = int(cast_cfg.get("browser_extract_limit", 3))

    cast_manager = CastManager(
        device_name=device_name,
        resolve_timeout=resolve_timeout,
        source_probe_limit=source_probe_limit,
        use_browser_rank=use_browser_rank,
        browser_timeout=browser_timeout,
        browser_executable=browser_executable,
        use_browser_extract=use_browser_extract,
        browser_extract_limit=browser_extract_limit,
    )
    server = ThreadingHTTPServer((host, port), CastRequestHandler)
    server.cast_manager = cast_manager  # type: ignore[attr-defined]

    print(f"Cast server listening on http://{host}:{port} (device: {device_name})")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Cast server stopped.")


def extract_games(league_key, sport, data, local_tz):
    """Extract game dicts from ESPN scoreboard JSON."""
    games = []
    if not data:
        return games

    league_name = league_key.upper()
    leagues = data.get("leagues", [])
    if leagues:
        league_name = leagues[0].get("abbreviation", league_name)

    events = data.get("events", [])
    for event in events:
        season_info = event.get("season", {})
        season_type = season_info.get("type", 0)
        season_slug = season_info.get("slug", "")

        competitions = event.get("competitions", [])
        for comp in competitions:
            game = _extract_competition(
                comp, event, league_key, sport, league_name,
                season_type, season_slug, local_tz
            )
            if game:
                games.append(game)

    return games


def _extract_f1_competition(comp, event, league_key, league_name,
                            season_type, season_slug, local_tz):
    """Extract an F1 session."""
    comp_type = comp.get("type", {})
    event_type = comp_type.get("abbreviation", "")

    date_str = comp.get("date", event.get("date", ""))
    game_time = None
    try:
        game_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        game_time = game_time.astimezone(local_tz)
    except (ValueError, TypeError):
        pass

    status = comp.get("status", {})
    status_type = status.get("type", {})
    state = status_type.get("state", "pre")
    short_detail = status_type.get("shortDetail", "")

    if state == "pre" and game_time:
        time_display = game_time.strftime("%-I:%M %p")
    elif state == "post":
        time_display = "Final"
    else:
        time_display = short_detail or "Live"

    top_drivers = []
    competitors = comp.get("competitors", [])
    sorted_comps = sorted(competitors, key=lambda c: c.get("order") or 999)
    for c in sorted_comps[:3]:
        ath = c.get("athlete", {})
        name = ath.get("shortName") or ath.get("displayName", "?")
        top_drivers.append(name)

    broadcasts = comp.get("broadcasts", [])
    broadcast = ""
    if broadcasts:
        names = broadcasts[0].get("names", [])
        if names:
            broadcast = names[0]

    return {
        "league_key": league_key,
        "league_name": league_name,
        "sport": "racing",
        "is_f1": True,
        "gp_name": event.get("name", "Grand Prix"),
        "event_type": event_type,
        "top_drivers": top_drivers,
        "game_time": game_time,
        "time_display": time_display,
        "state": state,
        "season_type": season_type,
        "season_slug": season_slug,
        "broadcast": broadcast,
        "espn_link": "",
        "espn_boxscore": "",
        "espn_standings": "",
        "stream_url": None,
        "home_abbr": "", "away_abbr": "",
        "home_name": "", "away_name": "",
        "home_score": "", "away_score": "",
        "home_record": "", "away_record": "",
        "home_color": "666666", "away_color": "666666",
        "home_logo": "", "away_logo": "",
    }


def _extract_competition(comp, event, league_key, sport, league_name,
                         season_type, season_slug, local_tz):
    """Extract a single game/competition dict."""
    if league_key == "f1":
        return _extract_f1_competition(
            comp, event, league_key, league_name,
            season_type, season_slug, local_tz
        )

    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return None

    home = away = None
    for c in competitors:
        if c.get("homeAway") == "home":
            home = c
        else:
            away = c
    if not home or not away:
        home, away = competitors[0], competitors[1]

    def team_info(c):
        team = c.get("team", {})
        records = c.get("records", [])
        record_str = ""
        for r in records:
            if r.get("type") == "total" or r.get("name") == "overall":
                record_str = r.get("summary", "")
                break
        if not record_str and records:
            record_str = records[0].get("summary", "")
        return {
            "name": team.get("displayName", team.get("name", "?")),
            "abbr": team.get("abbreviation", "?"),
            "score": c.get("score", ""),
            "record": record_str,
            "color": team.get("color", "666666"),
            "logo": team.get("logo", ""),
        }

    home_info = team_info(home)
    away_info = team_info(away)

    date_str = event.get("date", comp.get("date", ""))
    game_time = None
    try:
        game_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        game_time = game_time.astimezone(local_tz)
    except (ValueError, TypeError):
        pass

    status = comp.get("status", event.get("status", {}))
    status_type = status.get("type", {})
    state = status_type.get("state", "pre")
    short_detail = status_type.get("shortDetail", "")

    if state == "pre" and game_time:
        time_display = game_time.strftime("%-I:%M %p")
    elif state == "post":
        time_display = short_detail or "Final"
    else:
        time_display = short_detail or "Live"

    comp_type = comp.get("type", {})
    event_type = comp_type.get("abbreviation", "")

    broadcasts = comp.get("broadcasts", [])
    broadcast = ""
    if broadcasts:
        names = broadcasts[0].get("names", [])
        if names:
            broadcast = names[0]

    # ESPN links
    event_id = event.get("id", "")
    espn_link = ""
    espn_boxscore = ""
    event_links = event.get("links", [])
    for link in event_links:
        rels = link.get("rel", [])
        if "summary" in rels and "desktop" in rels:
            espn_link = link.get("href", "")
            break
    if event_id and sport:
        league_slug = league_key
        if sport == "soccer":
            league_slug = "soccer"
        espn_boxscore = f"https://www.espn.com/{league_slug}/boxscore/_/gameId/{event_id}"
        if not espn_link:
            espn_link = f"https://www.espn.com/{league_slug}/game/_/gameId/{event_id}"

    espn_standings = ESPN_STANDINGS_URL.get(league_key, "")

    return {
        "league_key": league_key,
        "league_name": league_name,
        "sport": sport,
        "is_f1": False,
        "home_name": home_info["name"],
        "home_abbr": home_info["abbr"],
        "home_score": home_info["score"],
        "home_record": home_info["record"],
        "home_color": home_info["color"],
        "home_logo": home_info["logo"],
        "away_name": away_info["name"],
        "away_abbr": away_info["abbr"],
        "away_score": away_info["score"],
        "away_record": away_info["record"],
        "away_color": away_info["color"],
        "away_logo": away_info["logo"],
        "game_time": game_time,
        "time_display": time_display,
        "state": state,
        "season_type": season_type,
        "season_slug": season_slug,
        "broadcast": broadcast,
        "event_type": event_type,
        "espn_link": espn_link,
        "espn_boxscore": espn_boxscore,
        "espn_standings": espn_standings,
        "stream_url": None,
        "event_id": event_id,
    }


def matches_rule(game, rule):
    """Check if a game matches a classification rule."""
    if rule["league"] != game["league_key"]:
        return False
    if "teams" in rule:
        teams = rule["teams"]
        if game["home_abbr"] not in teams and game["away_abbr"] not in teams:
            return False
    if "season" in rule:
        wanted = rule["season"]
        if wanted == "postseason" and game["season_type"] not in (3, 4):
            return False
        if wanted == "regular" and game["season_type"] != 2:
            return False
    if "event_types" in rule:
        if game["event_type"] not in rule["event_types"]:
            return False
    return True


def classify_games(all_games, rules):
    """Assign each game to a tier based on rules. First match wins."""
    tiered = {"S": [], "A": [], "B": []}
    for game in all_games:
        for rule in rules:
            if matches_rule(game, rule):
                tier = rule["tier"]
                game["is_favorite"] = "teams" in rule
                tiered.setdefault(tier, []).append(game)
                break
    for tier in tiered:
        tiered[tier].sort(key=lambda g: (
            not g.get("is_favorite", False),
            g["game_time"] or datetime.max.replace(tzinfo=timezone.utc),
        ))
    return tiered


# ─── HTML Rendering ───────────────────────────────────────────────

def _esc(text):
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _esc_js(text):
    return (str(text)
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace('"', '\\"')
            .replace("\n", "\\n"))


def _state_indicator(state):
    if state == "in":
        return '<span class="state-dot live"></span>'
    elif state == "post":
        return '<span class="state-dot final"></span>'
    return ""


def _logo_img(logo_url, abbr, size=24):
    if logo_url:
        return f'<img src="{_esc(logo_url)}" alt="{_esc(abbr)}" class="team-logo" width="{size}" height="{size}">'
    return ""


NCAA_BRACKET_URL = {
    "mens-college-basketball": "https://www.espn.com/mens-college-basketball/bracket",
    "college-football": "https://www.espn.com/college-football/bracket",
}


def _detail_links(game, cast_enabled=False):
    """Render the expandable detail panel content."""
    links = []
    if game.get("espn_link"):
        links.append(f'<a href="{_esc(game["espn_link"])}" target="_blank" class="detail-link">Gamecast</a>')
    if game.get("espn_boxscore"):
        links.append(f'<a href="{_esc(game["espn_boxscore"])}" target="_blank" class="detail-link">Box Score</a>')
    # NCAA postseason: link to bracket instead of standings
    lk = game.get("league_key", "")
    if lk in NCAA_BRACKET_URL and game.get("season_type") in (3, 4):
        links.append(f'<a href="{NCAA_BRACKET_URL[lk]}" target="_blank" class="detail-link">Bracket</a>')
    elif game.get("espn_standings"):
        links.append(f'<a href="{_esc(game["espn_standings"])}" target="_blank" class="detail-link">Standings</a>')
    if game.get("stream_url"):
        links.append(f'<a href="{_esc(game["stream_url"])}" target="_blank" class="detail-link stream-link">Stream</a>')

    if not links:
        return ""
    return '<div class="detail-panel">' + " ".join(links) + '</div>'


def _render_large(game, show_logos, cast_enabled):
    fav_class = " favorite" if game.get("is_favorite") else ""
    fav_color = game["home_color"] if game.get("is_favorite") else "444"
    border_style = f"border-left: 4px solid #{_esc(fav_color)};" if game.get("is_favorite") else ""

    logo_away = _logo_img(game["away_logo"], game["away_abbr"], 32) if show_logos else ""
    logo_home = _logo_img(game["home_logo"], game["home_abbr"], 32) if show_logos else ""

    show_score = game["state"] != "pre"
    score_away = f'<span class="score score-away">{_esc(game["away_score"])}</span>' if show_score and game["away_score"] else '<span class="score score-away" style="display:none"></span>'
    score_home = f'<span class="score score-home">{_esc(game["home_score"])}</span>' if show_score and game["home_score"] else '<span class="score score-home" style="display:none"></span>'

    broadcast = f'<span class="broadcast">{_esc(game["broadcast"])}</span>' if game["broadcast"] else ""
    event_type = f'<span class="event-type">{_esc(game["event_type"])}</span>' if game["event_type"] else ""

    details = _detail_links(game, cast_enabled)

    return f"""<div class="game-card large{fav_class} expandable" data-league="{_esc(game['league_key'])}" data-home-abbr="{_esc(game['home_abbr'])}" data-away-abbr="{_esc(game['away_abbr'])}" data-game-id="{_esc(game.get('event_id',''))}" data-sport="{_esc(game.get('sport',''))}" data-state="{_esc(game['state'])}" data-orig-tier="S" style="{border_style}" onclick="toggleDetail(this, event)">
  <div class="game-status"><span class="status-text">{_state_indicator(game["state"])} {_esc(game["time_display"])}</span> {broadcast} {event_type}</div>
  <div class="team-row">
    {logo_away}
    <span class="team-name">{_esc(game["away_name"])}</span>
    <span class="record">{_esc(game["away_record"])}</span>
    {score_away}
  </div>
  <div class="team-row">
    {logo_home}
    <span class="team-name">{_esc(game["home_name"])}</span>
    <span class="record">{_esc(game["home_record"])}</span>
    {score_home}
  </div>
  {details}
</div>"""


def _render_medium(game, show_logos, cast_enabled):
    fav_class = " favorite" if game.get("is_favorite") else ""
    fav_color = game["home_color"] if game.get("is_favorite") else "444"
    border_style = f"border-left: 3px solid #{_esc(fav_color)};" if game.get("is_favorite") else ""

    logo_away = _logo_img(game["away_logo"], game["away_abbr"], 20) if show_logos else ""
    logo_home = _logo_img(game["home_logo"], game["home_abbr"], 20) if show_logos else ""

    scores = ""
    if game["state"] != "pre" and (game["away_score"] or game["home_score"]):
        scores = f'<span class="score-away">{_esc(game["away_score"])}</span> - <span class="score-home">{_esc(game["home_score"])}</span>'
    else:
        scores = '<span class="score-away" style="display:none"></span><span class="score-home" style="display:none"></span>'

    broadcast = f' &middot; {_esc(game["broadcast"])}' if game["broadcast"] else ""
    event_type = f' &middot; {_esc(game["event_type"])}' if game["event_type"] else ""

    details = _detail_links(game, cast_enabled)

    return f"""<div class="game-card medium{fav_class} expandable" data-league="{_esc(game['league_key'])}" data-home-abbr="{_esc(game['home_abbr'])}" data-away-abbr="{_esc(game['away_abbr'])}" data-game-id="{_esc(game.get('event_id',''))}" data-sport="{_esc(game.get('sport',''))}" data-state="{_esc(game['state'])}" data-orig-tier="A" style="{border_style}" onclick="toggleDetail(this, event)">
  <div class="medium-status"><span class="status-text">{_state_indicator(game["state"])} {_esc(game["time_display"])}</span>{broadcast}{event_type}</div>
  <div class="medium-matchup">
    {logo_away} <span class="abbr">{_esc(game["away_abbr"])}</span>
    <span class="record-sm">{_esc(game["away_record"])}</span>
    <span class="at">@</span>
    {logo_home} <span class="abbr">{_esc(game["home_abbr"])}</span>
    <span class="record-sm">{_esc(game["home_record"])}</span>
    <span class="medium-score">{scores}</span>
  </div>
  {details}
</div>"""


def _render_compact(game, show_logos, cast_enabled):
    fav_class = " favorite" if game.get("is_favorite") else ""

    scores = ""
    if game["state"] != "pre" and (game["away_score"] or game["home_score"]):
        scores = f'<span class="score-away">{_esc(game["away_score"])}</span>-<span class="score-home">{_esc(game["home_score"])}</span>'
    else:
        scores = '<span class="score-away" style="display:none"></span><span class="score-home" style="display:none"></span>'

    broadcast = _esc(game["broadcast"]) if game["broadcast"] else ""
    event_type = _esc(game["event_type"]) if game["event_type"] else ""
    extra = " &middot; ".join(filter(None, [broadcast, event_type]))

    details = _detail_links(game, cast_enabled)

    return f"""<div class="compact-row{fav_class} expandable" data-league="{_esc(game['league_key'])}" data-home-abbr="{_esc(game['home_abbr'])}" data-away-abbr="{_esc(game['away_abbr'])}" data-game-id="{_esc(game.get('event_id',''))}" data-sport="{_esc(game.get('sport',''))}" data-state="{_esc(game['state'])}" data-orig-tier="B" onclick="toggleDetail(this, event)">
  <span class="compact-status"><span class="status-text">{_state_indicator(game["state"])}{_esc(game["time_display"])}</span></span>
  <span class="compact-matchup">{_esc(game["away_abbr"])} @ {_esc(game["home_abbr"])}</span>
  <span class="compact-score">{scores}</span>
  <span class="compact-extra">{extra}</span>
  {details}
</div>"""


def _render_f1(game, tier, cast_enabled):
    gp = game["gp_name"]
    session = game["event_type"]
    broadcast = game["broadcast"]
    top = game.get("top_drivers", [])

    result_html = ""
    if game["state"] == "post" and top:
        podium = " &middot; ".join(f"{i+1}. {_esc(d)}" for i, d in enumerate(top))
        result_html = f'<div class="f1-podium">{podium}</div>'

    broadcast_html = f' &middot; {_esc(broadcast)}' if broadcast else ""
    details = _detail_links(game, cast_enabled)

    if tier == "B":
        return f"""<div class="compact-row expandable" data-league="{_esc(game['league_key'])}" onclick="toggleDetail(this, event)">
  <span class="compact-status">{_state_indicator(game["state"])}{_esc(game["time_display"])}</span>
  <span class="compact-matchup">{_esc(gp)} — {_esc(session)}</span>
  <span class="compact-score"></span>
  <span class="compact-extra">{_esc(broadcast)}</span>
  {result_html}
  {details}
</div>"""
    else:
        return f"""<div class="game-card {'large' if tier == 'S' else 'medium'} expandable" data-league="{_esc(game['league_key'])}" onclick="toggleDetail(this, event)">
  <div class="game-status">{_state_indicator(game["state"])} {_esc(game["time_display"])}{broadcast_html}</div>
  <div class="f1-event">{_esc(gp)}</div>
  <div class="f1-session">{_esc(session)}</div>
  {result_html}
  {details}
</div>"""


def _render_calendar_event(game, cast_enabled=False):
    """Render a game as a calendar timeline block."""
    if game.get("is_f1"):
        label = f'{_esc(game["gp_name"])} — {_esc(game["event_type"])}'
    else:
        label = f'{_esc(game["away_abbr"])} @ {_esc(game["home_abbr"])}'

    league = _esc(game["league_name"])
    state_class = game["state"]
    fav_class = " cal-fav" if game.get("is_favorite") else ""
    color = game.get("home_color", "666666") if game.get("is_favorite") else "93a1a1"

    scores = ""
    if not game.get("is_f1") and game["state"] != "pre" and (game["away_score"] or game["home_score"]):
        scores = f' ({_esc(game["away_score"])}-{_esc(game["home_score"])})'

    stream_icon = ' <span class="cal-stream" title="Stream available">&#9654;</span>' if game.get("stream_url") else ""
    actions = []
    if game.get("stream_url"):
        actions.append(
            f'<a href="{_esc(game["stream_url"])}" target="_blank" class="cal-action cal-action-stream" '
            f'onclick="event.stopPropagation()">Stream</a>'
        )
    actions_html = f'<span class="cal-actions">{" ".join(actions)}</span>' if actions else ""

    home_abbr = _esc(game.get("home_abbr", ""))
    away_abbr = _esc(game.get("away_abbr", ""))
    league_key = _esc(game.get("league_key", ""))
    return {
        "html": f"""<div class="cal-event{fav_class} cal-{state_class}" style="border-left-color: #{_esc(color)};" data-home-abbr="{home_abbr}" data-away-abbr="{away_abbr}" data-league="{league_key}">
  <span class="cal-league">{league}</span>
  <span class="cal-label">{label}{scores}{stream_icon}</span>
  <span class="cal-time">{_esc(game["time_display"])}</span>
  {actions_html}
</div>""",
        "hour": game["game_time"].hour if game.get("game_time") else 0,
    }


def render_html(tiered_games, config, generated_at, errors, all_games_flat,
                extra_games=None, league_info=None, all_teams=None):
    """Render the full HTML dashboard with tabs."""
    refresh = config.get("refresh_interval", 300)
    show_logos = config.get("show_logos", True)
    cast_cfg = config.get("cast", {})
    cast_enabled = bool(cast_cfg.get("enabled", False))
    cast_host = cast_cfg.get("server_host", "127.0.0.1")
    cast_port = int(cast_cfg.get("server_port", 8765))
    time_str = generated_at.strftime("%-I:%M %p %Z")
    today_str = generated_at.strftime("%A, %B %-d")
    today_iso = generated_at.strftime("%Y-%m-%d")

    # ── Teams data for settings panel ──
    teams_data_js = json.dumps(all_teams or [], ensure_ascii=True).replace("</", "<\\/")

    # ── League list for settings panel ──
    _league_sport_map = {lk: sport for lk, sport, _, _ in ALL_LEAGUES}
    all_league_keys_js = json.dumps([
        {"key": li["key"], "name": li["display"], "sport": _league_sport_map.get(li["key"], "")}
        for li in (league_info or [])
    ], ensure_ascii=True).replace("</", "<\\/")

    # ── Config defaults for settings panel pre-seed ──
    _default_teams = {"S": [], "A": [], "B": []}
    _default_leagues = {"S": [], "A": [], "B": []}
    _seen_team_keys = set()
    for rule in config.get("rules", []):
        tier = rule.get("tier", "B")
        lk = rule.get("league", "")
        teams = rule.get("teams", [])
        if teams:
            for abbr in teams:
                key = f"{abbr}:{lk}"
                if key not in _seen_team_keys:
                    _seen_team_keys.add(key)
                    _default_teams.setdefault(tier, []).append(key)
        else:
            if lk and lk not in sum(_default_leagues.values(), []):
                _default_leagues.setdefault(tier, []).append(lk)
    config_defaults_js = json.dumps(
        {"teams": _default_teams, "leagues": _default_leagues}, ensure_ascii=True
    ).replace("</", "<\\/")

    # ── Dashboard tab ──
    def group_by_league(games):
        groups = []
        seen = {}
        for g in games:
            lk = g["league_key"]
            if lk not in seen:
                seen[lk] = []
                groups.append((g["league_name"], g.get("sport", ""), seen[lk]))
            seen[lk].append(g)
        return groups

    def league_label_html(league_name, sport):
        icon = SPORT_ICON.get(sport, "")
        prefix = f'<span class="league-icon">{icon}</span> ' if icon else ""
        return f'<div class="league-label">{prefix}{_esc(league_name)}</div>'

    sections_html = []
    for tier in ("S", "A", "B"):
        games = tiered_games.get(tier, [])
        if not games:
            continue
        league_groups = group_by_league(games)
        cards_html = []
        for league_name, sport, league_games in league_groups:
            cards_html.append(league_label_html(league_name, sport))
            cards_html.append(f'<div class="games-grid tier-{tier.lower()}-grid">')
            for g in league_games:
                if g.get("is_f1"):
                    cards_html.append(_render_f1(g, tier, cast_enabled))
                elif tier == "B":
                    cards_html.append(_render_compact(g, show_logos, cast_enabled))
                elif tier == "A":
                    cards_html.append(_render_medium(g, show_logos, cast_enabled))
                else:
                    cards_html.append(_render_large(g, show_logos, cast_enabled))
            cards_html.append('</div>')
        sections_html.append(
            f'<section class="tier-section tier-{tier.lower()}">'
            + "\n".join(cards_html)
            + '</section>'
        )

    error_html = ""
    if errors:
        error_items = "".join(
            f'<div class="error-msg">Could not load {_esc(lk)}</div>'
            for lk in errors
        )
        error_html = f'<div class="errors">{error_items}</div>'

    if not sections_html and not errors:
        sections_html.append('<div class="no-games">No games scheduled for today</div>')

    dashboard_body = error_html + "\n".join(sections_html)

    # ── Calendar tab ──
    # Build hourly timeline — only games from today's date
    today_date = generated_at.date()
    timed_games = [
        g for g in all_games_flat
        if g.get("game_time") and g["game_time"].date() == today_date
    ]
    timed_games.sort(key=lambda g: g["game_time"])

    # Determine hour range
    if timed_games:
        hours = [g["game_time"].hour for g in timed_games]
        min_hour = max(0, min(hours) - 1)
        max_hour = min(23, max(hours) + 1)
    else:
        min_hour, max_hour = 8, 23

    hour_slots = []
    for h in range(min_hour, max_hour + 1):
        ampm = "AM" if h < 12 else "PM"
        display_h = h % 12 or 12
        hour_label = f"{display_h} {ampm}"
        events_in_hour = []
        for g in timed_games:
            if g["game_time"].hour == h:
                events_in_hour.append(_render_calendar_event(g, cast_enabled))
        events_html = "\n".join(e["html"] for e in events_in_hour)
        now_class = " current-hour" if h == generated_at.hour else ""
        hour_slots.append(f"""<div class="cal-hour{now_class}">
  <div class="cal-hour-label">{hour_label}</div>
  <div class="cal-hour-events">{events_html}</div>
</div>""")

    # Games without times or from other dates
    other_games = [
        g for g in all_games_flat
        if not g.get("game_time") or g["game_time"].date() != today_date
    ]
    other_html = ""
    if other_games:
        ot_events = "\n".join(_render_calendar_event(g, cast_enabled)["html"] for g in other_games)
        other_html = f'<div class="cal-hour"><div class="cal-hour-label">Other</div><div class="cal-hour-events">{ot_events}</div></div>'

    calendar_body = f"""<div class="cal-nav">
  <button onclick="changeDate(-1)" class="cal-nav-btn">&larr;</button>
  <input type="date" id="cal-date" value="{today_iso}" onchange="goToDate(this.value)" class="cal-date-input">
  <button onclick="changeDate(1)" class="cal-nav-btn">&rarr;</button>
</div>
<div class="cal-timeline" id="cal-timeline">
{"".join(hour_slots)}
{other_html}
</div>"""

    # ── Extra games section (non-config leagues) ──
    extra_html = ""
    if extra_games:
        def group_extra_by_league(games):
            groups = []
            seen = {}
            for g in games:
                lk = g["league_key"]
                if lk not in seen:
                    seen[lk] = []
                    groups.append((g["league_name"], lk, g.get("sport", ""), seen[lk]))
                seen[lk].append(g)
            return groups

        extra_cards = []
        for league_name, lk, sport, league_games in group_extra_by_league(extra_games):
            extra_cards.append(league_label_html(league_name, sport))
            extra_cards.append(f'<div class="games-grid tier-b-grid">')
            for g in league_games:
                if g.get("is_f1"):
                    extra_cards.append(_render_f1(g, "B", cast_enabled))
                else:
                    extra_cards.append(_render_compact(g, show_logos, cast_enabled))
            extra_cards.append('</div>')
        extra_html = '<section class="tier-section tier-extra" id="extra-games" data-extra="1" style="display:none;">' + "\n".join(extra_cards) + '</section>'

    # ── Sidebar ──
    sidebar_items = []
    for li in (league_info or []):
        checked = "checked" if li["mine"] else ""
        mine_class = " sidebar-mine" if li["mine"] else ""
        count = f' <span class="sidebar-count">{li["count"]}</span>' if li["count"] else ""
        sidebar_items.append(
            f'<label class="sidebar-league{mine_class}">'
            f'<input type="checkbox" value="{_esc(li["key"])}" {checked} onchange="filterLeagues()">'
            f' {_esc(li["display"])}{count}</label>'
        )
    sidebar_html = "\n".join(sidebar_items)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>Sports Today</title>
<style>
{CSS}
</style>
</head>
<body>

<div class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <div class="sidebar-tabs">
      <button class="sidebar-tab active" onclick="switchSidebarTab('favorites', this)">Favorites</button>
      <button class="sidebar-tab" onclick="switchSidebarTab('leagues', this)">Leagues</button>
    </div>
    <button class="sidebar-close" onclick="toggleSidebar()">&times;</button>
  </div>

  <div class="sidebar-panel" id="sidebar-panel-favorites">
    <div class="sidebar-section">
      <div class="sidebar-section-label">Favorite Teams</div>
      <input class="settings-search" id="team-search" type="text" placeholder="Search teams..." oninput="renderTeamSearch()">
      <div class="team-search-results" id="team-search-results"></div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-label">My Tiers &mdash; Teams</div>
      <p class="settings-hint">Drag your favorite teams into a tier. Games involving those teams will appear in that tier on the dashboard. S = must-watch, A = interested, B = keep an eye on.</p>
      <div class="tier-label tier-s-label">S</div>
      <div class="tier-drop-zone" id="tier-teams-S" ondragover="event.preventDefault()" ondrop="dropTeam(event,'S')"></div>
      <div class="tier-label tier-a-label">A</div>
      <div class="tier-drop-zone" id="tier-teams-A" ondragover="event.preventDefault()" ondrop="dropTeam(event,'A')"></div>
      <div class="tier-label tier-b-label">B</div>
      <div class="tier-drop-zone" id="tier-teams-B" ondragover="event.preventDefault()" ondrop="dropTeam(event,'B')"></div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-label">My Tiers &mdash; Leagues</div>
      <p class="settings-hint">Assign entire leagues to a tier to show all their games at that level. Unassigned leagues are hidden in My View.</p>
      <div class="tier-label tier-s-label">S</div>
      <div class="tier-drop-zone" id="tier-leagues-S" ondragover="event.preventDefault()" ondrop="dropLeague(event,'S')"></div>
      <div class="tier-label tier-a-label">A</div>
      <div class="tier-drop-zone" id="tier-leagues-A" ondragover="event.preventDefault()" ondrop="dropLeague(event,'A')"></div>
      <div class="tier-label tier-b-label">B</div>
      <div class="tier-drop-zone" id="tier-leagues-B" ondragover="event.preventDefault()" ondrop="dropLeague(event,'B')"></div>
      <div class="tier-label" style="color:var(--text-dim)">Unassigned</div>
      <div class="tier-drop-zone tier-unassigned" id="tier-leagues-none" ondragover="event.preventDefault()" ondrop="dropLeague(event,'none')"></div>
    </div>
    <div class="settings-actions">
      <button class="settings-btn settings-btn-primary" onclick="applyAndUpdate()">Save &amp; Update</button>
      <button class="settings-btn settings-btn-reset" onclick="resetToDefaults()">Reset</button>
    </div>
  </div>

  <div class="sidebar-panel" id="sidebar-panel-leagues" style="display:none">
    <div class="sidebar-section">
      <div class="sidebar-section-label">My Leagues</div>
      {chr(10).join(s for s, li in zip(sidebar_items, league_info or []) if li.get("mine"))}
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-label">Other Leagues</div>
      {chr(10).join(s for s, li in zip(sidebar_items, league_info or []) if not li.get("mine"))}
    </div>
  </div>
</div>
<div class="sidebar-overlay" id="sidebar-overlay" onclick="toggleSidebar()"></div>

<header>
  <div class="header-left">
    <button class="burger" onclick="toggleSidebar()" title="Filter leagues">&#9776;</button>
    <h1>Sports Today</h1>
    <span class="header-date">{_esc(today_str)}</span>
  </div>
  <div class="view-toggle" title="Switch between your personalised view and all games for checked leagues">
    <span class="view-toggle-label">My View</span>
    <label class="toggle-switch">
      <input type="checkbox" id="view-all-toggle" onchange="switchView(this.checked)">
      <span class="toggle-track"></span>
    </label>
    <span class="view-toggle-label">All</span>
  </div>
  <div class="tabs">
    <button class="tab active" onclick="switchTab('dashboard', event)">Dashboard</button>
    <button class="tab" onclick="switchTab('calendar', event)">Calendar</button>
  </div>
  <span class="updated">Updated {_esc(time_str)}</span>
</header>
<div id="tab-dashboard" class="tab-content active">
{dashboard_body}
{extra_html}
</div>

<div id="tab-calendar" class="tab-content">
{calendar_body}
</div>

<footer>Data from ESPN &middot; Auto-refreshes every {refresh // 60} min</footer>

<script>
window.CAST_CONFIG = {{
  enabled: {str(cast_enabled).lower()},
  endpoint: "http://{_esc_js(cast_host)}:{cast_port}"
}};
window.ALL_TEAMS = {teams_data_js};
window.ALL_LEAGUES_LIST = {all_league_keys_js};
window.CONFIG_DEFAULTS = {config_defaults_js};
{JS}
</script>
</body>
</html>"""


# ─── CSS ──────────────────────────────────────────────────────────

CSS = """
:root {
  --bg: #fdf6e3;
  --surface: #eee8d5;
  --surface-hover: #e6dfca;
  --text: #657b83;
  --text-dim: #93a1a1;
  --text-bright: #073642;
  --accent-s: #b58900;
  --accent-a: #268bd2;
  --accent-b: #93a1a1;
  --live-green: #859900;
  --final-gray: #93a1a1;
  --error-yellow: #cb4b16;
  --link-blue: #268bd2;
  --stream-red: #dc322f;
  --border: #d3cbb7;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  padding: 20px;
  max-width: 1200px;
  margin: 0 auto;
}

header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
  gap: 8px;
}
.header-left { display: flex; align-items: baseline; gap: 12px; }
.header-date { font-size: 0.85rem; color: var(--text-dim); }

h1 {
  font-size: 1.6rem;
  font-weight: 600;
  color: var(--text-bright);
}

.updated {
  font-size: 0.8rem;
  color: var(--text-dim);
}

/* Tabs */
.tabs { display: flex; gap: 4px; }
.tab {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 5px 14px;
  font-size: 0.8rem;
  color: var(--text-dim);
  cursor: pointer;
  font-family: inherit;
}
.tab:hover { background: var(--surface); }
.tab.active {
  background: var(--surface);
  color: var(--text-bright);
  font-weight: 600;
}
.tab-content { display: none; }
.tab-content.active { display: block; }

/* League labels */
.league-label {
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--text-dim);
  margin: 16px 0 8px 4px;
}

.tier-section { margin-bottom: 8px; }
.league-icon { font-style: normal; opacity: 0.75; font-size: 0.85em; }
.tier-s .league-label { color: var(--accent-s); }
.tier-a .league-label { color: var(--accent-a); }
.tier-b .league-label { color: var(--accent-b); }

/* Grids */
.games-grid { display: grid; gap: 8px; }
.tier-s-grid { grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }
.tier-a-grid { grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); }
.tier-b-grid { grid-template-columns: 1fr; max-width: 700px; }

/* Expandable cards */
.expandable { cursor: pointer; }
.detail-panel {
  display: none;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
  width: 100%;
  flex-basis: 100%;
}
.expanded .detail-panel { display: flex; gap: 8px; flex-wrap: wrap; }
.detail-link {
  display: inline-block;
  font-size: 0.75rem;
  padding: 3px 10px;
  border-radius: 4px;
  background: var(--surface-hover);
  color: var(--link-blue);
  text-decoration: none;
  border: 1px solid var(--border);
}
.detail-link:hover { background: var(--border); }
.detail-link.stream-link {
  background: rgba(220, 50, 47, 0.1);
  color: var(--stream-red);
  border-color: rgba(220, 50, 47, 0.3);
  font-weight: 600;
}
.detail-link.stream-link:hover { background: rgba(220, 50, 47, 0.2); }
/* Large cards (Tier S) */
.game-card.large {
  background: var(--surface);
  border-radius: 8px;
  padding: 14px 16px;
  border-left: 4px solid transparent;
}
.game-card.large:hover { background: var(--surface-hover); }
.game-card.large .game-status {
  font-size: 0.75rem;
  color: var(--text-dim);
  margin-bottom: 10px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.team-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}
.team-name { flex: 1; font-weight: 500; font-size: 0.95rem; }
.record { font-size: 0.75rem; color: var(--text-dim); }
.score {
  font-size: 1.3rem;
  font-weight: 700;
  color: var(--text-bright);
  min-width: 30px;
  text-align: right;
}
.broadcast, .event-type {
  font-size: 0.7rem;
  color: var(--text-bright);
  background: #d3cbb7;
  padding: 1px 6px;
  border-radius: 3px;
}

/* Medium cards (Tier A) */
.game-card.medium {
  background: var(--surface);
  border-radius: 6px;
  padding: 10px 12px;
  border-left: 3px solid transparent;
}
.game-card.medium:hover { background: var(--surface-hover); }
.medium-status {
  font-size: 0.7rem;
  color: var(--text-dim);
  margin-bottom: 4px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.medium-matchup {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.85rem;
}
.abbr { font-weight: 600; }
.at { color: var(--text-dim); }
.record-sm { font-size: 0.7rem; color: var(--text-dim); }
.medium-score {
  margin-left: auto;
  font-weight: 700;
  color: var(--text-bright);
}

/* Compact rows (Tier B) */
.compact-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 10px;
  font-size: 0.8rem;
  border-radius: 4px;
  flex-wrap: wrap;
}
.compact-row:nth-child(even) { background: rgba(0,0,0,0.03); }
.compact-row:hover { background: var(--surface-hover); }
.compact-status {
  min-width: 90px;
  color: var(--text-dim);
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 0.75rem;
}
.compact-matchup { min-width: 100px; font-weight: 500; }
.compact-score {
  font-weight: 600;
  color: var(--text-bright);
  min-width: 50px;
}
.compact-extra { color: var(--text-dim); font-size: 0.7rem; }

/* Favorites */
.favorite { background: rgba(0,0,0,0.04) !important; }
.compact-row.favorite { font-weight: 600; }

/* State dots */
.state-dot {
  display: inline-block;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--text-dim);
}
.state-dot.live {
  background: var(--live-green);
  animation: pulse 1.5s ease-in-out infinite;
}
.state-dot.final { background: var(--final-gray); }

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.team-logo { border-radius: 2px; flex-shrink: 0; }

.no-games {
  text-align: center;
  color: var(--text-dim);
  padding: 60px 20px;
  font-size: 1.1rem;
}
.errors { margin-bottom: 16px; }
.error-msg {
  background: rgba(203, 75, 22, 0.1);
  border-left: 3px solid var(--error-yellow);
  color: var(--error-yellow);
  padding: 8px 12px;
  margin-bottom: 4px;
  border-radius: 4px;
  font-size: 0.8rem;
}

/* F1 */
.f1-event { font-weight: 600; font-size: 0.95rem; color: var(--text-bright); }
.f1-session { font-size: 0.8rem; color: var(--text-dim); margin-top: 2px; }
.f1-podium { font-size: 0.75rem; color: var(--text); margin-top: 4px; }

/* Calendar tab */
.cal-nav {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}
.cal-nav-btn {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 1rem;
  color: var(--text-bright);
  font-family: inherit;
}
.cal-nav-btn:hover { background: var(--surface-hover); }
.cal-date-input {
  font-family: inherit;
  font-size: 0.85rem;
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text-bright);
}

.cal-timeline { }
.cal-hour {
  display: flex;
  min-height: 48px;
  border-top: 1px solid var(--border);
}
.cal-hour.current-hour {
  background: rgba(38, 139, 210, 0.06);
}
.cal-hour-label {
  width: 70px;
  flex-shrink: 0;
  font-size: 0.75rem;
  color: var(--text-dim);
  padding: 8px 8px 8px 0;
  text-align: right;
  font-weight: 500;
}
.cal-hour-events {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px;
}
.cal-event {
  background: var(--surface);
  border-radius: 5px;
  padding: 5px 10px;
  border-left: 3px solid var(--text-dim);
  font-size: 0.78rem;
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}
.cal-event:hover { background: var(--surface-hover); }
.cal-fav { font-weight: 600; }
.cal-in { border-left-width: 3px; }
.cal-post { opacity: 0.6; }
.cal-league {
  font-size: 0.65rem;
  font-weight: 600;
  text-transform: uppercase;
  color: var(--text-dim);
  letter-spacing: 0.05em;
}
.cal-label { color: var(--text-bright); }
.cal-time { font-size: 0.7rem; color: var(--text-dim); }
.cal-stream { color: var(--stream-red); font-size: 0.65rem; }
.cal-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: 2px;
}
.cal-action {
  font-size: 0.68rem;
  line-height: 1;
  padding: 3px 7px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--surface-hover);
  color: var(--link-blue);
  text-decoration: none;
  cursor: pointer;
}
.cal-action.cal-action-stream {
  color: var(--stream-red);
  border-color: rgba(220, 50, 47, 0.3);
  background: rgba(220, 50, 47, 0.08);
}
.cal-action.cal-action-cast {
  color: var(--live-green);
  border-color: rgba(133, 153, 0, 0.35);
  background: rgba(133, 153, 0, 0.12);
}

/* View toggle */
.view-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
  color: var(--text-dim);
}
.view-toggle-label { user-select: none; }
.toggle-switch { position: relative; display: inline-block; width: 34px; height: 18px; }
.toggle-switch input { opacity: 0; width: 0; height: 0; }
.toggle-track {
  position: absolute;
  inset: 0;
  background: var(--border);
  border-radius: 18px;
  cursor: pointer;
  transition: background 0.2s;
}
.toggle-track::before {
  content: '';
  position: absolute;
  width: 12px; height: 12px;
  left: 3px; top: 3px;
  background: white;
  border-radius: 50%;
  transition: transform 0.2s;
}
.toggle-switch input:checked + .toggle-track { background: var(--accent-a); }
.toggle-switch input:checked + .toggle-track::before { transform: translateX(16px); }

/* Burger + Sidebar */
.burger {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 1.2rem;
  cursor: pointer;
  color: var(--text-bright);
  line-height: 1;
  font-family: inherit;
}
.burger:hover { background: var(--surface); }

.sidebar-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.2);
  z-index: 99;
}
.sidebar-overlay.open { display: block; }

.sidebar {
  position: fixed;
  top: 0;
  left: -360px;
  width: 340px;
  height: 100vh;
  background: var(--bg);
  border-right: 1px solid var(--border);
  z-index: 100;
  transition: left 0.2s ease;
  overflow-y: auto;
  padding: 16px;
}
.sidebar.open { left: 0; }

.sidebar-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}
.sidebar-title {
  font-weight: 600;
  font-size: 1rem;
  color: var(--text-bright);
}
.sidebar-close {
  background: none;
  border: none;
  font-size: 1.4rem;
  cursor: pointer;
  color: var(--text-dim);
  padding: 0 4px;
}
.sidebar-close:hover { color: var(--text-bright); }

.sidebar-section { margin-bottom: 16px; }
.sidebar-section-label {
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-dim);
  margin-bottom: 6px;
}

.sidebar-league {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  font-size: 0.82rem;
  color: var(--text);
  cursor: pointer;
}
.sidebar-league:hover { color: var(--text-bright); }
.sidebar-league input[type="checkbox"] {
  accent-color: var(--accent-a);
}
.sidebar-mine { font-weight: 500; color: var(--text-bright); }
.sidebar-count {
  font-size: 0.65rem;
  color: var(--text-dim);
  background: var(--surface);
  padding: 0 5px;
  border-radius: 8px;
  margin-left: auto;
}

.tier-extra .league-label { color: var(--text-dim); }

/* Sidebar tabs */
.sidebar-tabs {
  display: flex;
  gap: 4px;
  flex: 1;
}
.sidebar-tab {
  background: none;
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 3px 10px;
  font-size: 0.78rem;
  cursor: pointer;
  color: var(--text-dim);
  font-family: inherit;
}
.sidebar-tab:hover { background: var(--surface); color: var(--text-bright); }
.sidebar-tab.active {
  background: var(--surface);
  color: var(--text-bright);
  font-weight: 600;
}

/* Settings panel */
.settings-hint {
  font-size: 0.72rem;
  color: var(--text-dim);
  margin-bottom: 8px;
  line-height: 1.4;
}
.settings-search {
  width: 100%;
  padding: 5px 8px;
  font-size: 0.8rem;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text-bright);
  font-family: inherit;
  margin-bottom: 6px;
}
.settings-search:focus { outline: 1px solid var(--accent-a); }

.team-search-results {
  max-height: 180px;
  overflow-y: auto;
}
.team-search-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 2px;
  font-size: 0.8rem;
  cursor: pointer;
  border-radius: 4px;
}
.team-search-row:hover { background: var(--surface); }
.team-search-row img { width: 18px; height: 18px; object-fit: contain; }
.team-search-name { flex: 1; color: var(--text-bright); }
.team-search-league { font-size: 0.65rem; color: var(--text-dim); text-transform: uppercase; }
.team-search-add {
  font-size: 0.7rem;
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface-hover);
  color: var(--accent-a);
  cursor: pointer;
  font-family: inherit;
}
.team-search-add:hover { background: var(--border); }
.team-search-add.added { color: var(--text-dim); cursor: default; }

/* Tier drop zones */
.tier-label {
  font-size: 0.65rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin: 8px 0 3px;
}
.tier-s-label { color: var(--accent-s); }
.tier-a-label { color: var(--accent-a); }
.tier-b-label { color: var(--accent-b); }

.tier-drop-zone {
  min-height: 32px;
  border: 1px dashed var(--border);
  border-radius: 5px;
  padding: 4px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 2px;
}
.tier-drop-zone.drag-over { border-color: var(--accent-a); background: rgba(38,139,210,0.05); }
.tier-unassigned { border-style: solid; }

/* Tier chips */
.tier-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 2px 6px 2px 4px;
  font-size: 0.75rem;
  cursor: grab;
  color: var(--text-bright);
  user-select: none;
}
.tier-chip:active { cursor: grabbing; }
.tier-chip img { width: 14px; height: 14px; object-fit: contain; }
.tier-chip-remove {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-dim);
  font-size: 0.8rem;
  padding: 0;
  line-height: 1;
  margin-left: 2px;
  font-family: inherit;
}
.tier-chip-remove:hover { color: var(--error-yellow); }

.settings-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.settings-btn {
  flex: 1;
  padding: 7px 0;
  border-radius: 5px;
  font-size: 0.8rem;
  font-family: inherit;
  cursor: pointer;
  font-weight: 600;
  border: 1px solid var(--border);
}
.settings-btn-primary {
  background: var(--accent-a);
  color: white;
  border-color: var(--accent-a);
}
.settings-btn-primary:hover { opacity: 0.88; }
.settings-btn-reset {
  background: var(--surface);
  color: var(--text);
}
.settings-btn-reset:hover { background: var(--surface-hover); }

footer {
  text-align: center;
  color: var(--text-dim);
  font-size: 0.7rem;
  margin-top: 32px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
"""


# ─── JavaScript ───────────────────────────────────────────────────

JS = """
function switchTab(name, evt) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (evt && evt.target) {
    evt.target.classList.add('active');
  } else {
    var fallback = document.querySelector('.tab[onclick*="' + name + '"]');
    if (fallback) fallback.classList.add('active');
  }
  if (name === 'calendar') highlightCalendar();
}

function toggleDetail(el, evt) {
  // Don't toggle if clicking a link inside the panel
  if (evt && evt.target) {
    var tag = evt.target.tagName;
    if (tag === 'A' || tag === 'BUTTON' || evt.target.closest('.detail-link')) return;
  }
  el.classList.toggle('expanded');
}

function changeDate(delta) {
  var input = document.getElementById('cal-date');
  var d = new Date(input.value + 'T12:00:00');
  d.setDate(d.getDate() + delta);
  var iso = d.toISOString().split('T')[0];
  input.value = iso;
  goToDate(iso);
}

function goToDate(dateStr) {
  // Reload the page with a date parameter
  // The script generates static HTML, so for other dates we open
  // ESPN directly or regenerate. For simplicity, we fetch ESPN
  // scoreboards client-side for non-today dates.
  var today = new Date().toISOString().split('T')[0];
  if (dateStr === today) {
    location.reload();
    return;
  }
  fetchCalendarForDate(dateStr);
}

function fetchCalendarForDate(dateStr) {
  var espnDate = dateStr.replace(/-/g, '');
  var timeline = document.getElementById('cal-timeline');
  timeline.innerHTML = '<div class="no-games">Loading...</div>';

  // Fetch all configured leagues for that date
  var leagues = CONFIGURED_LEAGUES;
  var promises = leagues.map(function(l) {
    var url = 'https://site.api.espn.com/apis/site/v2/sports/' + l.sport + '/' + l.league + '/scoreboard?dates=' + espnDate;
    return fetch(url).then(function(r) { return r.json(); }).then(function(data) {
      return { key: l.key, data: data };
    }).catch(function() { return { key: l.key, data: null }; });
  });

  Promise.all(promises).then(function(results) {
    var events = [];
    results.forEach(function(r) {
      if (!r.data) return;
      var leagueName = r.key.toUpperCase();
      var ls = r.data.leagues || [];
      if (ls.length > 0) leagueName = ls[0].abbreviation || leagueName;

      (r.data.events || []).forEach(function(ev) {
        (ev.competitions || []).forEach(function(comp) {
          var competitors = comp.competitors || [];
          if (competitors.length < 2 && r.key !== 'f1') return;

          var dateObj = new Date(ev.date || comp.date || '');
          var status = (comp.status || ev.status || {}).type || {};
          var state = status.state || 'pre';
          var timeStr = '';
          if (state === 'pre') {
            timeStr = dateObj.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
          } else if (state === 'post') {
            timeStr = status.shortDetail || 'Final';
          } else {
            timeStr = status.shortDetail || 'Live';
          }

          var home = competitors.find(function(c){return c.homeAway==='home';}) || competitors[0] || {};
          var away = competitors.find(function(c){return c.homeAway!=='home';}) || competitors[1] || {};
          var ht = (home.team||{});
          var at = (away.team||{});

          var label;
          if (r.key === 'f1') {
            var ct = (comp.type||{}).abbreviation || '';
            label = (ev.name || 'F1') + ' — ' + ct;
          } else {
            label = (at.abbreviation||'?') + ' @ ' + (ht.abbreviation||'?');
          }

          var score = '';
          if (home.score !== undefined && home.score !== null && home.score !== '' &&
              away.score !== undefined && away.score !== null && away.score !== '') {
            score = ' (' + away.score + '-' + home.score + ')';
          }

          events.push({
            hour: dateObj.getHours(),
            league: leagueName,
            label: label + score,
            state: state,
            timeStr: timeStr,
          });
        });
      });
    });

    events.sort(function(a,b) { return a.hour - b.hour; });

    if (events.length === 0) {
      timeline.innerHTML = '<div class="no-games">No games on this date</div>';
      return;
    }

    var minH = Math.max(0, events[0].hour - 1);
    var maxH = Math.min(23, events[events.length-1].hour + 1);
    var html = '';
    for (var h = minH; h <= maxH; h++) {
      var ampm = h < 12 ? 'AM' : 'PM';
      var dh = h % 12 || 12;
      var eventsHtml = '';
      events.forEach(function(e) {
        if (e.hour !== h) return;
        var stateClass = 'cal-' + e.state;
        eventsHtml += '<div class="cal-event ' + stateClass + '">' +
          '<span class="cal-league">' + e.league + '</span>' +
          '<span class="cal-label">' + e.label + '</span>' +
          '<span class="cal-time">' + e.timeStr + '</span>' +
          '</div>';
      });
      html += '<div class="cal-hour"><div class="cal-hour-label">' + dh + ' ' + ampm + '</div>' +
        '<div class="cal-hour-events">' + eventsHtml + '</div></div>';
    }
    timeline.innerHTML = html;
  });
}

// Scroll to current hour on calendar tab load
document.querySelectorAll('.tab').forEach(function(btn) {
  btn.addEventListener('click', function() {
    setTimeout(function() {
      var cur = document.querySelector('.current-hour');
      if (cur) cur.scrollIntoView({behavior:'smooth', block:'center'});
    }, 100);
  });
});

// Sidebar
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebar-overlay').classList.toggle('open');
}

var viewAll = false;

function switchView(all) {
  viewAll = all;
  filterLeagues();
}

// Stash of all game cards extracted from original HTML, keyed by a unique index
var _allCards = null;
var _origDashboard = null;

function _extractAllCards() {
  if (_allCards) return;
  _allCards = [];
  var dashboard = document.getElementById('tab-dashboard');
  if (!dashboard) return;
  _origDashboard = dashboard.innerHTML;
  // Collect every game card (has data-league); mark extra section cards
  dashboard.querySelectorAll('[data-league]').forEach(function(el) {
    var clone = el.cloneNode(true);
    if (el.closest('[data-extra]')) clone.setAttribute('data-extra', '1');
    _allCards.push(clone);
  });
}

function filterLeagues() {
  _extractAllCards();

  var checked = new Set();
  document.querySelectorAll('.sidebar-league input:checked').forEach(function(cb) {
    checked.add(cb.value);
  });

  var dashboard = document.getElementById('tab-dashboard');
  if (!dashboard) return;

  if (viewAll) {
    // Restore original and show/hide by checked leagues
    dashboard.innerHTML = _origDashboard;
    dashboard.querySelectorAll('[data-league]').forEach(function(el) {
      el.style.display = checked.has(el.getAttribute('data-league')) ? '' : 'none';
    });
    _cleanupGrids(dashboard);
    return;
  }

  var prefs = loadPrefs();
  var teamTier = {};
  ['S','A','B'].forEach(function(t) {
    (prefs.teams[t]||[]).forEach(function(k){ teamTier[k] = t; });
  });
  var leagueTier = {};
  ['S','A','B'].forEach(function(t) {
    (prefs.leagues[t]||[]).forEach(function(k){ leagueTier[k] = t; });
  });
  var hasPrefs = Object.keys(teamTier).length > 0 || Object.keys(leagueTier).length > 0;

  // Sort cards into tier buckets grouped by league
  // buckets[tier][leagueKey] = [cardElement, ...]
  var buckets = { S: {}, A: {}, B: {} };

  (_allCards||[]).forEach(function(card) {
    var league = card.getAttribute('data-league');
    if (!checked.has(league)) return;

    var home = card.getAttribute('data-home-abbr') || '';
    var away = card.getAttribute('data-away-abbr') || '';
    var tier;

    // Never show extra-section cards in My View
    if (card.getAttribute('data-extra')) return;

    if (!hasPrefs) {
      // No prefs: use original Python tier
      tier = card.getAttribute('data-orig-tier') || 'B';
    } else {
      // Team pref takes priority over league pref
      tier = teamTier[home + ':' + league] || teamTier[away + ':' + league] || leagueTier[league];
      if (!tier) return; // not in any pref
    }

    if (!buckets[tier][league]) buckets[tier][league] = [];
    buckets[tier][league].push(card);
  });

  // Render buckets into dashboard
  var leagueInfoMap = {};
  (window.ALL_LEAGUES_LIST||[]).forEach(function(l){ leagueInfoMap[l.key] = l; });

  var frag = document.createDocumentFragment();
  var anySection = false;

  ['S','A','B'].forEach(function(tier) {
    var tierBucket = buckets[tier];
    var leagueKeys = Object.keys(tierBucket);
    if (!leagueKeys.length) return;
    anySection = true;

    var section = document.createElement('section');
    section.className = 'tier-section tier-' + tier.toLowerCase();

    leagueKeys.forEach(function(lk) {
      var cards = tierBucket[lk];
      var li = leagueInfoMap[lk];
      var lname = li ? li.name : lk.toUpperCase();
      var sportIcons = {basketball:'🏀',football:'🏈','american-football':'🏈',baseball:'⚾',hockey:'🏒',soccer:'⚽',racing:'🏎'};
      var icon = li && li.sport ? (sportIcons[li.sport] || '') : '';

      var label = document.createElement('div');
      label.className = 'league-label';
      if (icon) {
        label.innerHTML = '<span class="league-icon">' + icon + '</span> ' + lname;
      } else {
        label.textContent = lname;
      }
      section.appendChild(label);

      var gridClass = tier === 'S' ? 'tier-s-grid' : tier === 'A' ? 'tier-a-grid' : 'tier-b-grid';
      var grid = document.createElement('div');
      grid.className = 'games-grid ' + gridClass;

      cards.forEach(function(origCard) {
        var card = origCard.cloneNode(true);
        // Re-style card size to match tier
        if (!card.classList.contains('compact-row') && !card.classList.contains('f1-card')) {
          if (tier === 'S') { card.classList.remove('medium'); card.classList.add('large'); }
          else if (tier === 'A') { card.classList.remove('large'); card.classList.add('medium'); }
          else { card.classList.remove('large'); card.classList.add('medium'); }
        }
        card.style.display = '';
        grid.appendChild(card);
      });

      section.appendChild(grid);
    });

    frag.appendChild(section);
  });

  dashboard.innerHTML = '';
  if (anySection) {
    dashboard.appendChild(frag);
  } else {
    dashboard.innerHTML = '<div class="no-games">No games match your favorites &mdash; try adjusting your tier settings</div>';
  }
}

function _cleanupGrids(container) {
  (container || document).querySelectorAll('.games-grid').forEach(function(grid) {
    var hasVisible = Array.from(grid.querySelectorAll('[data-league]')).some(function(el){
      return el.style.display !== 'none';
    });
    grid.style.display = hasVisible ? '' : 'none';
    var prev = grid.previousElementSibling;
    if (prev && prev.classList.contains('league-label')) prev.style.display = hasVisible ? '' : 'none';
  });
  (container || document).querySelectorAll('.tier-section').forEach(function(sec) {
    var hasVisible = Array.from(sec.querySelectorAll('[data-league]')).some(function(el){
      return el.style.display !== 'none';
    });
    sec.style.display = hasVisible ? '' : 'none';
  });
}

// ── Settings panel ──────────────────────────────────────────────

function switchSidebarTab(name, btn) {
  document.querySelectorAll('.sidebar-tab').forEach(function(b) { b.classList.remove('active'); });
  document.querySelectorAll('.sidebar-panel').forEach(function(p) { p.style.display = 'none'; });
  btn.classList.add('active');
  document.getElementById('sidebar-panel-' + name).style.display = '';
}

// localStorage prefs: { teams: {S:[...], A:[...], B:[...]}, leagues: {S:[...], A:[...], B:[...]} }
// team keys: "ABBR:league_key"

function loadPrefs() {
  try {
    var raw = localStorage.getItem('sports_prefs');
    if (raw) {
      var p = JSON.parse(raw);
      if (p && p.teams && p.leagues) {
        // Check if non-empty
        var hasAny = ['S','A','B'].some(function(t) {
          return (p.teams[t]||[]).length > 0 || (p.leagues[t]||[]).length > 0;
        });
        if (hasAny) return p;
      }
    }
  } catch(e) {}
  return defaultPrefs();
}

function defaultPrefs() {
  var defaults = window.CONFIG_DEFAULTS || {teams:{S:[],A:[],B:[]}, leagues:{S:[],A:[],B:[]}};
  return {
    teams:   {S: (defaults.teams.S||[]).slice(),   A: (defaults.teams.A||[]).slice(),   B: (defaults.teams.B||[]).slice()},
    leagues: {S: (defaults.leagues.S||[]).slice(), A: (defaults.leagues.A||[]).slice(), B: (defaults.leagues.B||[]).slice()}
  };
}

function savePrefs(prefs) {
  localStorage.setItem('sports_prefs', JSON.stringify(prefs));
}

// Event delegation for team search results (avoids inline onclick quote issues)
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.team-search-add:not([disabled])');
  if (btn) {
    var key = btn.getAttribute('data-key');
    if (key) addTeam(key, 'B');
  }
  var rem = e.target.closest('.tier-chip-remove');
  if (rem) {
    var type = rem.getAttribute('data-type');
    var key2 = rem.getAttribute('data-key');
    if (type === 'team') removeTeam(key2);
    else if (type === 'league') removeLeague(key2);
  }
});

function renderTeamSearch() {
  var input = document.getElementById('team-search');
  var q = input ? input.value.toLowerCase().trim() : '';
  var prefs = loadPrefs();
  var allAdded = new Set();
  ['S','A','B'].forEach(function(t) { (prefs.teams[t]||[]).forEach(function(k){ allAdded.add(k); }); });

  var teams = window.ALL_TEAMS || [];
  var filtered = q
    ? teams.filter(function(t) {
        return t.abbr.toLowerCase().includes(q) ||
               t.name.toLowerCase().includes(q) ||
               t.leagueName.toLowerCase().includes(q);
      })
    : teams.slice(0, 40);

  var html = '';
  filtered.forEach(function(t) {
    var key = t.abbr + ':' + t.league;
    var added = allAdded.has(key);
    var img = t.logo
      ? '<img src="' + t.logo + '" alt="" width="18" height="18" style="object-fit:contain">'
      : '<span style="width:18px;display:inline-block"></span>';
    html += '<div class="team-search-row">' +
      img +
      '<span class="team-search-name">' + t.name + '</span>' +
      '<span class="team-search-league">' + t.leagueName + '</span>' +
      '<button class="team-search-add' + (added ? ' added' : '') + '" data-key="' + key + '"' + (added ? ' disabled' : '') + '>' +
        (added ? '&#10003;' : '+ Add') +
      '</button>' +
    '</div>';
  });
  if (!html) html = '<div style="font-size:0.75rem;color:var(--text-dim);padding:4px">No teams found</div>';
  var el = document.getElementById('team-search-results');
  if (el) el.innerHTML = html;
}

function addTeam(key, tier) {
  var prefs = loadPrefs();
  ['S','A','B'].forEach(function(t) {
    prefs.teams[t] = (prefs.teams[t]||[]).filter(function(k){ return k !== key; });
  });
  if (!prefs.teams[tier]) prefs.teams[tier] = [];
  prefs.teams[tier].push(key);
  savePrefs(prefs);
  renderTeamSearch();
  renderTierZones();
}

function removeTeam(key) {
  var prefs = loadPrefs();
  ['S','A','B'].forEach(function(t) {
    prefs.teams[t] = (prefs.teams[t]||[]).filter(function(k){ return k !== key; });
  });
  savePrefs(prefs);
  renderTierZones();
}

function removeLeague(key) {
  var prefs = loadPrefs();
  ['S','A','B'].forEach(function(t) {
    prefs.leagues[t] = (prefs.leagues[t]||[]).filter(function(k){ return k !== key; });
  });
  savePrefs(prefs);
  renderTierZones();
}

function applyAndUpdate() {
  filterLeagues();
  // Flash the button to confirm
  var btn = document.querySelector('.settings-btn-primary');
  if (btn) { btn.textContent = 'Saved!'; setTimeout(function(){ btn.textContent = 'Save & Update'; }, 1200); }
}

function resetToDefaults() {
  savePrefs(defaultPrefs());
  renderTeamSearch();
  renderTierZones();
  filterLeagues();
}

var _dragTeam = null;
var _dragLeague = null;

function dragTeamStart(evt, key) {
  _dragTeam = key;
  _dragLeague = null;
  evt.dataTransfer.effectAllowed = 'move';
}

function dropTeam(evt, tier) {
  evt.preventDefault();
  if (!_dragTeam) return;
  addTeam(_dragTeam, tier);
  _dragTeam = null;
}

function dragLeagueStart(evt, key) {
  _dragLeague = key;
  _dragTeam = null;
  evt.dataTransfer.effectAllowed = 'move';
}

function dropLeague(evt, tier) {
  evt.preventDefault();
  if (!_dragLeague) return;
  var prefs = loadPrefs();
  ['S','A','B'].forEach(function(t) {
    prefs.leagues[t] = (prefs.leagues[t]||[]).filter(function(k){ return k !== _dragLeague; });
  });
  if (tier !== 'none') {
    if (!prefs.leagues[tier]) prefs.leagues[tier] = [];
    prefs.leagues[tier].push(_dragLeague);
  }
  savePrefs(prefs);
  _dragLeague = null;
  document.querySelectorAll('.tier-drop-zone').forEach(function(z){ z.classList.remove('drag-over'); });
  renderTierZones();
}

function renderTierZones() {
  var prefs = loadPrefs();
  var teamMap = {};
  (window.ALL_TEAMS || []).forEach(function(t){ teamMap[t.abbr + ':' + t.league] = t; });

  ['S','A','B'].forEach(function(tier) {
    var zone = document.getElementById('tier-teams-' + tier);
    if (!zone) return;
    zone.innerHTML = '';
    (prefs.teams[tier]||[]).forEach(function(key) {
      var t = teamMap[key];
      if (!t) return;
      var chip = document.createElement('div');
      chip.className = 'tier-chip';
      chip.draggable = true;
      chip.dataset.key = key;
      chip.dataset.dtype = 'team';
      chip.addEventListener('dragstart', function(e) { dragTeamStart(e, key); });
      if (t.logo) {
        var img = document.createElement('img');
        img.src = t.logo; img.width = 14; img.height = 14;
        img.style.objectFit = 'contain';
        chip.appendChild(img);
      }
      chip.appendChild(document.createTextNode(t.abbr));
      var rm = document.createElement('button');
      rm.className = 'tier-chip-remove';
      rm.title = 'Remove';
      rm.setAttribute('data-type', 'team');
      rm.setAttribute('data-key', key);
      rm.innerHTML = '&times;';
      chip.appendChild(rm);
      zone.appendChild(chip);
    });
  });

  var allLeagues = window.ALL_LEAGUES_LIST || [];
  var assignedLeagues = new Set();
  ['S','A','B'].forEach(function(t){ (prefs.leagues[t]||[]).forEach(function(k){ assignedLeagues.add(k); }); });

  ['S','A','B'].forEach(function(tier) {
    var zone = document.getElementById('tier-leagues-' + tier);
    if (!zone) return;
    zone.innerHTML = '';
    (prefs.leagues[tier]||[]).forEach(function(key) {
      var league = allLeagues.find(function(l){ return l.key === key; });
      var name = league ? league.name : key;
      var chip = document.createElement('div');
      chip.className = 'tier-chip';
      chip.draggable = true;
      chip.addEventListener('dragstart', function(e) { dragLeagueStart(e, key); });
      chip.appendChild(document.createTextNode(name));
      var rm = document.createElement('button');
      rm.className = 'tier-chip-remove';
      rm.title = 'Remove';
      rm.setAttribute('data-type', 'league');
      rm.setAttribute('data-key', key);
      rm.innerHTML = '&times;';
      chip.appendChild(rm);
      zone.appendChild(chip);
    });
  });

  var unassignedZone = document.getElementById('tier-leagues-none');
  if (unassignedZone) {
    unassignedZone.innerHTML = '';
    allLeagues.forEach(function(league) {
      if (assignedLeagues.has(league.key)) return;
      var chip = document.createElement('div');
      chip.className = 'tier-chip';
      chip.draggable = true;
      chip.addEventListener('dragstart', function(e) { dragLeagueStart(e, league.key); });
      chip.appendChild(document.createTextNode(league.name));
      unassignedZone.appendChild(chip);
    });
  }

  document.querySelectorAll('.tier-drop-zone').forEach(function(zone) {
    zone.addEventListener('dragenter', function(){ zone.classList.add('drag-over'); });
    zone.addEventListener('dragleave', function(e){
      if (!zone.contains(e.relatedTarget)) zone.classList.remove('drag-over');
    });
  });
}

function highlightCalendar() {
  var prefs = loadPrefs();
  var teamTier = {};
  ['S','A','B'].forEach(function(t) {
    (prefs.teams[t]||[]).forEach(function(k){ teamTier[k] = t; });
  });
  var leagueTier = {};
  ['S','A','B'].forEach(function(t) {
    (prefs.leagues[t]||[]).forEach(function(k){ leagueTier[k] = t; });
  });
  document.querySelectorAll('#tab-calendar .cal-event').forEach(function(el) {
    var lk = el.getAttribute('data-league') || '';
    var home = el.getAttribute('data-home-abbr') || '';
    var away = el.getAttribute('data-away-abbr') || '';
    var inTier = teamTier[home + ':' + lk] || teamTier[away + ':' + lk] || leagueTier[lk];
    if (inTier) {
      el.classList.add('cal-fav');
      el.style.borderLeftColor = inTier === 'S' ? 'var(--accent-s)' : inTier === 'A' ? 'var(--accent-a)' : 'var(--accent-b)';
    } else {
      el.classList.remove('cal-fav');
      el.style.borderLeftColor = '#93a1a1';
    }
  });
}

document.addEventListener('DOMContentLoaded', function() {
  renderTeamSearch();
  renderTierZones();
  filterLeagues();
  highlightCalendar();
  startLiveScorePolling();
});

// ── Live score polling ───────────────────────────────────────────

function startLiveScorePolling() {
  pollLiveScores();
  setInterval(pollLiveScores, 30000);
}

function pollLiveScores() {
  // Collect unique leagues that have live or upcoming games in the DOM
  var leagueSet = {};
  document.querySelectorAll('[data-game-id][data-sport]').forEach(function(card) {
    var lk = card.getAttribute('data-league');
    var sport = card.getAttribute('data-sport');
    if (lk && sport && !leagueSet[lk]) leagueSet[lk] = sport;
  });

  Object.keys(leagueSet).forEach(function(lk) {
    var sport = leagueSet[lk];
    var url = 'https://site.api.espn.com/apis/site/v2/sports/' + sport + '/' + lk + '/scoreboard';
    fetch(url).then(function(r) { return r.json(); }).then(function(data) {
      applyLiveScores(data.events || []);
    }).catch(function(){});
  });
}

function applyLiveScores(events) {
  events.forEach(function(ev) {
    var gameId = ev.id;
    var cards = document.querySelectorAll('[data-game-id="' + gameId + '"]');
    if (!cards.length) return;

    var comp = (ev.competitions || [])[0];
    if (!comp) return;

    var statusType = (comp.status || {}).type || {};
    var state = statusType.state || 'pre';
    var detail = statusType.shortDetail || '';

    var competitors = comp.competitors || [];
    var home = competitors.find(function(c){ return c.homeAway === 'home'; }) || {};
    var away = competitors.find(function(c){ return c.homeAway === 'away'; }) || {};
    var homeScore = (home.score !== undefined && home.score !== null) ? String(home.score) : '';
    var awayScore = (away.score !== undefined && away.score !== null) ? String(away.score) : '';

    cards.forEach(function(card) {
      // Update state attribute
      card.setAttribute('data-state', state);

      // Update status text
      var statusEl = card.querySelector('.status-text');
      if (statusEl) {
        var dot = state === 'in' ? '<span class="state-dot live"></span> '
                : state === 'post' ? '<span class="state-dot final"></span> '
                : '<span class="state-dot"></span> ';
        statusEl.innerHTML = dot + detail;
      }

      // Update scores
      if (state !== 'pre' && (homeScore || awayScore)) {
        var awayEl = card.querySelector('.score-away');
        var homeEl = card.querySelector('.score-home');
        if (awayEl) { awayEl.textContent = awayScore; awayEl.style.display = ''; }
        if (homeEl) { homeEl.textContent = homeScore; homeEl.style.display = ''; }
      }
    });
  });
}
"""


def atomic_write(path, content):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def main():
    if "--cast-server" in sys.argv[1:]:
        config = load_config()
        run_cast_server(config)
        return

    config = load_config()
    local_tz = ZoneInfo(config.get("timezone", "America/Los_Angeles"))
    today = datetime.now(local_tz).strftime("%Y%m%d")

    my_leagues = collect_leagues(config)
    my_league_keys = {lk for lk, _, _ in my_leagues}

    # Build full fetch list: my leagues + all extra leagues
    all_fetch = list(my_leagues)
    for lk, sport, league, display in ALL_LEAGUES:
        if lk not in my_league_keys:
            all_fetch.append((lk, sport, league))

    # Fetch scoreboards + streams in parallel
    print(f"Fetching {len(all_fetch)} leagues + streams...")
    raw_data = {}
    errors = []
    streams = []

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {}
        for lk, sport, league in all_fetch:
            f = pool.submit(fetch_scoreboard, sport, league, today)
            futures[f] = ("scoreboard", lk, sport)

        stream_future = pool.submit(fetch_streams)
        futures[stream_future] = ("streams", None, None)

        for future in as_completed(futures):
            kind, lk, sport = futures[future]
            if kind == "streams":
                streams = future.result() or []
            else:
                result = future.result()
                if result is None:
                    errors.append(lk)
                raw_data[lk] = result

    # Extract all games
    all_games = []
    for lk, sport, league in all_fetch:
        data = raw_data.get(lk)
        if data:
            all_games.extend(extract_games(lk, sport, data, local_tz))

    # Match streams
    if streams:
        print(f"  Matching against {len(streams)} available streams...")
        for game in all_games:
            game["stream_url"] = match_stream(game, streams)

    # Classify my games into tiers
    tiered = classify_games(all_games, config["rules"])

    # Upgrade streamed.pk links to concrete highest-viewer stream links.
    # Performance: resolve only for "My games" (tiered) rather than all fetched games.
    my_games = []
    for tier in ("S", "A", "B"):
        my_games.extend(tiered.get(tier, []))
    linked = [g.get("stream_url") for g in my_games if g.get("stream_url")]
    resolve_cfg = config.get("best_stream_resolve", {}) or {}
    resolve_enabled = bool(resolve_cfg.get("enabled", True))
    resolve_timeout_sec = int(resolve_cfg.get("timeout_sec", 5))
    resolve_settle_ms = int(resolve_cfg.get("settle_ms", 1200))
    resolve_max_matches = resolve_cfg.get("max_matches")
    if linked and resolve_enabled:
        total_links = len(linked)
        if resolve_max_matches is not None:
            try:
                total_links = min(total_links, int(resolve_max_matches))
            except Exception:
                pass
        print(f"  Resolving best stream variants for {total_links} my-game link(s)...")
        t0 = time.time()
        best_map = resolve_best_stream_urls(
            linked,
            timeout_sec=resolve_timeout_sec,
            settle_ms=resolve_settle_ms,
            max_matches=resolve_max_matches,
            verbose=True,
        )
        print(f"  Best-stream resolve done in {time.time() - t0:.1f}s")
        if best_map:
            upgraded = 0
            for game in my_games:
                su = game.get("stream_url")
                if not su:
                    continue
                mid = _streamed_match_id(su)
                best = best_map.get(mid) if mid else None
                if best and best != su:
                    game["stream_url"] = best
                    upgraded += 1
            if upgraded:
                print(f"  Upgraded {upgraded} stream link(s) to highest-viewer variants")

    # Extra games (from non-config leagues, not classified)
    classified_ids = set()
    for tier in ("S", "A", "B"):
        for g in tiered.get(tier, []):
            classified_ids.add(id(g))
    extra_games = [g for g in all_games if id(g) not in classified_ids]
    extra_games.sort(key=lambda g: g["game_time"] or datetime.max.replace(tzinfo=timezone.utc))

    # Flat list for calendar (my games only)
    all_classified = []
    for tier in ("S", "A", "B"):
        all_classified.extend(tiered.get(tier, []))

    total = sum(len(g) for g in tiered.values())
    print(f"Found {total} matching games (S:{len(tiered['S'])} A:{len(tiered['A'])} B:{len(tiered['B'])}) + {len(extra_games)} extra")

    # Build sidebar league info
    league_info = []
    for lk, sport, league, display in ALL_LEAGUES:
        is_mine = lk in my_league_keys
        game_count = sum(1 for g in all_games if g["league_key"] == lk)
        league_info.append({
            "key": lk, "display": display, "mine": is_mine, "count": game_count,
        })

    print("Fetching team rosters...")
    all_teams = fetch_all_teams(ALL_LEAGUES)
    print(f"  Got {len(all_teams)} teams across all leagues")

    generated_at = datetime.now(local_tz)
    html = render_html(tiered, config, generated_at, errors, all_classified,
                       extra_games, league_info, all_teams=all_teams)

    # Inject configured leagues for JS date navigation
    leagues_js = json.dumps([
        {"key": lk, "sport": sport, "league": league}
        for lk, sport, league, _ in ALL_LEAGUES
    ])
    html = html.replace(
        "var leagues = CONFIGURED_LEAGUES;",
        f"var leagues = {leagues_js};"
    )

    output_path = Path(config.get("output", SCRIPT_DIR / "sports.html"))
    atomic_write(output_path, html)
    print(f"Written to {output_path}")


if __name__ == "__main__":
    main()
