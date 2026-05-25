"""
Leado scraping pipeline.

Flow:
  1. Claude generates targeted queries from the user's product description.
  2. Reddit (PRAW) and YouTube (Data API v3) return raw candidate posts/comments.
  3. Claude batch-scores every candidate and writes a personalised outreach message.
"""

import os
import json
import logging
import hashlib
from typing import Any, Optional

import re
import requests
import praw
import anthropic
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from serpapi import GoogleSearch
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ── Clients (lazy-initialised so missing keys don't crash import) ─────────────

def _reddit() -> Optional[praw.Reddit]:
    cid = os.environ.get("REDDIT_CLIENT_ID", "")
    secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not cid or not secret:
        log.warning("Reddit credentials missing — skipping Reddit scrape.")
        return None
    return praw.Reddit(
        client_id=cid,
        client_secret=secret,
        user_agent=os.environ.get("REDDIT_USER_AGENT", "Leado/1.0"),
    )

def _youtube():
    key = os.environ.get("YOUTUBE_API_KEY", "")
    if not key:
        log.warning("YOUTUBE_API_KEY missing — skipping YouTube scrape.")
        return None
    return build("youtube", "v3", developerKey=key)

def _claude() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ── Step 1 — Query generation ─────────────────────────────────────────────────

QUERY_PROMPT = """\
You are a B2B/B2C lead generation expert.

The user's product or service: <product>{product}</product>

Generate search queries that will surface people who NEED this service RIGHT NOW \
(not people learning about the topic or creating content about it).

CRITICAL: Queries must target CLIENTS/BUYERS expressing a need — not freelancers, \
not tutorials, not people discussing the industry. Think: "someone posting on a forum \
because they are stuck and need to hire someone."

Include a MIX of English AND French queries. French prospects are high priority — \
add phrases like "je cherche", "quelqu'un pour", "besoin d'un", "je veux engager", \
"monteur vidéo freelance", etc. adapted to the product.

Return ONLY valid JSON — no markdown fences, no commentary:
{{
  "reddit_queries": ["<4-5 English phrases targeting buyers>", "<3-4 French phrases targeting buyers>"],
  "youtube_queries": ["<2-3 English phrases>", "<2-3 French phrases>"],
  "target_subreddits": ["<8-12 subreddit names without r/, most relevant first>"],
  "buyer_signals": ["<6-10 words/phrases in EN and FR that strongly suggest buying intent>"]
}}
"""

def generate_search_queries(product: str) -> dict:
    claude = _claude()
    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": QUERY_PROMPT.format(product=product)}],
    )
    try:
        return json.loads(msg.content[0].text)
    except (json.JSONDecodeError, IndexError, KeyError) as exc:
        log.error("Failed to parse query JSON from Claude: %s", exc)
        # Fallback: use the raw product string as a single query
        return {
            "reddit_queries": [product],
            "youtube_queries": [product],
            "target_subreddits": ["entrepreneur", "smallbusiness", "freelance", "forhire"],
            "buyer_signals": ["looking for", "need", "hire", "recommend", "help"],
        }


# ── Step 2a — Reddit scraping ─────────────────────────────────────────────────

# Subreddits that always carry high buying-intent posts
BASE_SUBREDDITS = [
    "forhire", "hiring", "freelance", "slavelabour",
    "entrepreneur", "smallbusiness", "startups",
    "digital_marketing", "socialmedia",
]

def _post_to_raw(post: Any, source_subreddit: str) -> dict:
    text = (post.selftext or "").strip()
    if not text:
        text = post.title
    snippet = text[:600]
    return {
        "id": f"reddit_{post.id}",
        "source": "reddit",
        "source_url": f"https://reddit.com{post.permalink}",
        "author": str(post.author) if post.author else "[deleted]",
        "title": post.title,
        "content_snippet": snippet,
        "subreddit": source_subreddit,
        "score": post.score,
        "created_utc": post.created_utc,
    }

def scrape_reddit(queries: list[str], subreddits: list[str], limit: int = 5) -> list[dict]:
    reddit = _reddit()
    if not reddit:
        return []

    seen: set[str] = set()
    results: list[dict] = []
    all_subs = list(dict.fromkeys(subreddits + BASE_SUBREDDITS))  # dedupe, keep order

    # Search within specific subreddits first (higher relevance)
    for sub_name in all_subs[:12]:
        for query in queries[:6]:
            try:
                sub = reddit.subreddit(sub_name)
                for post in sub.search(query, sort="new", time_filter="month", limit=limit):
                    if post.id in seen:
                        continue
                    seen.add(post.id)
                    results.append(_post_to_raw(post, sub_name))
            except Exception as exc:
                log.debug("Reddit sub=%s query=%r: %s", sub_name, query, exc)

    # Broad search across all of Reddit as a catch-all
    for query in queries[:4]:
        try:
            for post in reddit.subreddit("all").search(
                query, sort="new", time_filter="week", limit=limit
            ):
                if post.id in seen:
                    continue
                seen.add(post.id)
                results.append(_post_to_raw(post, post.subreddit.display_name))
        except Exception as exc:
            log.debug("Reddit all query=%r: %s", query, exc)

    return results


# ── Step 2b — Google scraping via SerpApi ────────────────────────────────────

def scrape_google(queries: list[str], limit: int = 5) -> list[dict]:
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        log.warning("SERPAPI_KEY missing — skipping Google scrape.")
        return []

    seen: set[str] = set()
    results: list[dict] = []

    for query in queries[:4]:
        try:
            search = GoogleSearch({"q": query, "api_key": key, "num": limit, "hl": "fr"})
            data = search.get_dict()
            for r in data.get("organic_results", []):
                rid = r.get("link", "")
                if rid in seen:
                    continue
                seen.add(rid)
                snippet = r.get("snippet", "") or r.get("title", "")
                results.append({
                    "id": f"google_{hashlib.md5(rid.encode()).hexdigest()[:10]}",
                    "source": "google",
                    "source_url": rid,
                    "author": r.get("displayed_link", "google"),
                    "title": r.get("title", ""),
                    "content_snippet": snippet[:600],
                })
        except Exception as exc:
            log.error("SerpApi query=%r: %s", query, exc)

    return results


# ── Step 2c — YouTube scraping ────────────────────────────────────────────────

def _comment_to_raw(comment: dict, video_id: str, video_title: str) -> dict:
    snippet = comment["snippet"]["topLevelComment"]["snippet"]
    text = snippet.get("textDisplay", "")
    author = snippet.get("authorDisplayName", "unknown")
    return {
        "id": f"youtube_{comment['id']}",
        "source": "youtube",
        "source_url": f"https://youtube.com/watch?v={video_id}",
        "author": author,
        "title": video_title,
        "content_snippet": text[:600],
    }

def scrape_youtube(queries: list[str], limit: int = 3) -> list[dict]:
    yt = _youtube()
    if not yt:
        return []

    seen: set[str] = set()
    results: list[dict] = []

    for query in queries[:4]:
        try:
            search_resp = yt.search().list(
                q=query,
                part="snippet",
                type="video",
                maxResults=limit,
                order="date",
                relevanceLanguage="fr",
            ).execute()

            for item in search_resp.get("items", []):
                video_id = item["id"]["videoId"]
                video_title = item["snippet"]["title"]

                try:
                    ct_resp = yt.commentThreads().list(
                        videoId=video_id,
                        part="snippet",
                        maxResults=20,
                        order="relevance",
                        textFormat="plainText",
                    ).execute()
                except HttpError as exc:
                    log.debug("YT comments disabled video=%s: %s", video_id, exc)
                    continue

                for thread in ct_resp.get("items", []):
                    cid = thread["id"]
                    if cid in seen:
                        continue
                    seen.add(cid)
                    results.append(_comment_to_raw(thread, video_id, video_title))

        except HttpError as exc:
            log.error("YouTube search query=%r: %s", query, exc)

    return results


# ── Step 2d — Hacker News scraping (Algolia public API) ──────────────────────

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"

def scrape_hackernews(queries: list[str], limit: int = 10) -> list[dict]:
    seen: set[str] = set()
    results: list[dict] = []

    for query in queries[:5]:
        for tag in ("story", "comment"):
            try:
                resp = requests.get(
                    HN_SEARCH_URL,
                    params={
                        "query": query,
                        "tags": tag,
                        "hitsPerPage": limit,
                        "numericFilters": "created_at_i>1700000000",  # ~Nov 2023+
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                hits = resp.json().get("hits", [])
            except Exception as exc:
                log.debug("HN query=%r tag=%s: %s", query, tag, exc)
                continue

            for hit in hits:
                oid = hit.get("objectID", "")
                if oid in seen:
                    continue
                seen.add(oid)

                if tag == "story":
                    text = hit.get("story_text") or hit.get("title", "")
                    url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
                    title = hit.get("title", "")
                else:
                    text = hit.get("comment_text", "")
                    url = f"https://news.ycombinator.com/item?id={oid}"
                    title = hit.get("story_title", "")

                snippet = text[:600].strip() if text else title
                if not snippet:
                    continue

                results.append({
                    "id": f"hn_{oid}",
                    "source": "hackernews",
                    "source_url": url,
                    "author": hit.get("author", "unknown"),
                    "title": title,
                    "content_snippet": snippet,
                })

    return results


# ── Step 2e — Indie Hackers scraping (via SerpApi site: filter) ───────────────

def scrape_indiehackers(queries: list[str], limit: int = 5) -> list[dict]:
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        log.warning("SERPAPI_KEY missing — skipping Indie Hackers scrape.")
        return []

    seen: set[str] = set()
    results: list[dict] = []

    for query in queries[:4]:
        site_query = f"site:indiehackers.com {query}"
        try:
            search = GoogleSearch({
                "q": site_query,
                "api_key": key,
                "num": limit,
            })
            data = search.get_dict()
            for r in data.get("organic_results", []):
                url = r.get("link", "")
                if url in seen:
                    continue
                seen.add(url)
                snippet = r.get("snippet", "") or r.get("title", "")
                results.append({
                    "id": f"ih_{hashlib.md5(url.encode()).hexdigest()[:10]}",
                    "source": "indiehackers",
                    "source_url": url,
                    "author": r.get("displayed_link", "indiehackers.com"),
                    "title": r.get("title", ""),
                    "content_snippet": snippet[:600],
                })
        except Exception as exc:
            log.error("IndieHackers query=%r: %s", query, exc)

    return results


# ── Step 2f — Upwork scraping (SerpApi site: filter) ─────────────────────────

def scrape_upwork(queries: list[str], limit: int = 5) -> list[dict]:
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        log.warning("SERPAPI_KEY missing — skipping Upwork scrape.")
        return []

    seen: set[str] = set()
    results: list[dict] = []

    for query in queries[:4]:
        site_query = f"site:upwork.com/jobs {query}"
        try:
            search = GoogleSearch({"q": site_query, "api_key": key, "num": limit})
            data = search.get_dict()
            for r in data.get("organic_results", []):
                url = r.get("link", "")
                if url in seen or "upwork.com/jobs" not in url:
                    continue
                seen.add(url)
                snippet = r.get("snippet", "") or r.get("title", "")
                results.append({
                    "id": f"upwork_{hashlib.md5(url.encode()).hexdigest()[:10]}",
                    "source": "upwork",
                    "source_url": url,
                    "author": r.get("displayed_link", "upwork.com"),
                    "title": r.get("title", ""),
                    "content_snippet": snippet[:600],
                })
        except Exception as exc:
            log.error("Upwork query=%r: %s", query, exc)

    return results


# ── Step 2g — Twitter/X scraping (Nitter public mirrors) ─────────────────────

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.nl",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
]

def _get_nitter_base() -> Optional[str]:
    headers = {"User-Agent": "Mozilla/5.0"}
    for instance in NITTER_INSTANCES:
        try:
            r = requests.get(instance, headers=headers, timeout=6)
            if r.status_code == 200 and "nitter" in r.text.lower():
                return instance
        except Exception:
            continue
    return None

def scrape_twitter(queries: list[str], limit: int = 10) -> list[dict]:
    from bs4 import BeautifulSoup

    base = _get_nitter_base()
    if not base:
        log.warning("No Nitter instance available — skipping Twitter scrape.")
        return []

    seen: set[str] = set()
    results: list[dict] = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for query in queries[:4]:
        try:
            url = f"{base}/search?q={requests.utils.quote(query)}&f=tweets"
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for item in soup.select(".timeline-item")[:limit]:
                content_el = item.select_one(".tweet-content")
                author_el = item.select_one(".username")
                link_el = item.select_one(".tweet-link")
                if not content_el:
                    continue
                text = content_el.get_text(strip=True)
                author = author_el.get_text(strip=True) if author_el else "unknown"
                href = link_el.get("href", "") if link_el else ""
                tweet_url = f"https://twitter.com{href}" if href.startswith("/") else href
                tid = hashlib.md5(f"{author}:{text[:80]}".encode()).hexdigest()[:12]
                if tid in seen:
                    continue
                seen.add(tid)
                results.append({
                    "id": f"twitter_{tid}",
                    "source": "twitter",
                    "source_url": tweet_url,
                    "author": author,
                    "title": "",
                    "content_snippet": text[:600],
                })
        except Exception as exc:
            log.debug("Twitter/Nitter query=%r: %s", query, exc)

    return results


# ── Step 2h — GitHub Issues/Discussions scraping (public API) ────────────────

GH_SEARCH_URL = "https://api.github.com/search/issues"

def scrape_github(queries: list[str], limit: int = 8) -> list[dict]:
    seen: set[str] = set()
    results: list[dict] = []
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Leado/1.0",
    }
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    if gh_token:
        headers["Authorization"] = f"token {gh_token}"

    for query in queries[:4]:
        # Combine query with hiring/service-seeking signals
        gh_query = f'"{query}" is:issue is:open in:body'
        try:
            resp = requests.get(
                GH_SEARCH_URL,
                params={"q": gh_query, "per_page": limit, "sort": "updated"},
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 403:
                log.warning("GitHub API rate limit hit — skipping remaining queries.")
                break
            resp.raise_for_status()
            items = resp.json().get("items", [])
        except Exception as exc:
            log.debug("GitHub query=%r: %s", query, exc)
            continue

        for item in items:
            url = item.get("html_url", "")
            if url in seen:
                continue
            seen.add(url)
            body = (item.get("body") or "").strip()
            snippet = body[:600] if body else item.get("title", "")
            results.append({
                "id": f"gh_{item['id']}",
                "source": "github",
                "source_url": url,
                "author": item.get("user", {}).get("login", "unknown"),
                "title": item.get("title", ""),
                "content_snippet": snippet,
            })

    return results


# ── Step 2i — Quora scraping (SerpApi site: filter) ──────────────────────────

def scrape_quora(queries: list[str], limit: int = 5) -> list[dict]:
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        log.warning("SERPAPI_KEY missing — skipping Quora scrape.")
        return []

    seen: set[str] = set()
    results: list[dict] = []

    for query in queries[:4]:
        site_query = f"site:quora.com {query}"
        try:
            search = GoogleSearch({"q": site_query, "api_key": key, "num": limit})
            data = search.get_dict()
            for r in data.get("organic_results", []):
                url = r.get("link", "")
                if url in seen or "quora.com" not in url:
                    continue
                seen.add(url)
                snippet = r.get("snippet", "") or r.get("title", "")
                results.append({
                    "id": f"quora_{hashlib.md5(url.encode()).hexdigest()[:10]}",
                    "source": "quora",
                    "source_url": url,
                    "author": r.get("displayed_link", "quora.com"),
                    "title": r.get("title", ""),
                    "content_snippet": snippet[:600],
                })
        except Exception as exc:
            log.error("Quora query=%r: %s", query, exc)

    return results


# ── Step 2j — Codeur.com scraping (direct HTTP + BS4) ────────────────────────

def scrape_codeur(queries: list[str], limit: int = 8) -> list[dict]:
    from bs4 import BeautifulSoup

    seen: set[str] = set()
    results: list[dict] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9",
    }

    for query in queries[:4]:
        try:
            url = f"https://www.codeur.com/projects?q={requests.utils.quote(query)}"
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                log.debug("Codeur status=%d query=%r", r.status_code, query)
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for link in soup.select('a[href^="/projects/"]')[:limit]:
                href = link.get("href", "")
                if not href or href in seen or href == "/projects/":
                    continue
                seen.add(href)
                text = link.get_text(separator=" ", strip=True)
                if len(text) < 40:
                    continue
                # Title = everything up to the first metadata marker
                title_match = re.split(r"\s+Il y a|\s+There are|\s+Posted", text)
                title = title_match[0].strip()[:120]
                # Body = text after "Vues " / "Views " where the actual brief starts
                body_match = re.split(r"\d+\s*Vues?\s*|\d+\s*Views?\s*", text, maxsplit=1)
                body = (body_match[1].strip() if len(body_match) > 1 else text)[:600]
                results.append({
                    "id": f"codeur_{hashlib.md5(href.encode()).hexdigest()[:10]}",
                    "source": "codeur",
                    "source_url": f"https://www.codeur.com{href}",
                    "author": "codeur.com",
                    "title": title,
                    "content_snippet": body,
                })
        except Exception as exc:
            log.error("Codeur query=%r: %s", query, exc)

    return results


# ── Step 2k — LinkedIn scraping (SerpApi site: filter on posts) ──────────────

def scrape_linkedin(queries: list[str], limit: int = 5) -> list[dict]:
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        log.warning("SERPAPI_KEY missing — skipping LinkedIn scrape.")
        return []

    seen: set[str] = set()
    results: list[dict] = []

    buyer_prefixes = [
        '"looking for"',
        '"je cherche"',
        '"hiring"',
        '"besoin d\'un"',
    ]

    for query in queries[:3]:
        for prefix in buyer_prefixes[:2]:
            site_query = f'site:linkedin.com/posts {prefix} "{query}"'
            try:
                search = GoogleSearch({"q": site_query, "api_key": key, "num": limit})
                data = search.get_dict()
                for r in data.get("organic_results", []):
                    url = r.get("link", "")
                    if url in seen or "linkedin.com" not in url:
                        continue
                    seen.add(url)
                    snippet = r.get("snippet", "") or r.get("title", "")
                    results.append({
                        "id": f"li_{hashlib.md5(url.encode()).hexdigest()[:10]}",
                        "source": "linkedin",
                        "source_url": url,
                        "author": r.get("title", "").split(" - ")[0].split("'s Post")[0].strip(),
                        "title": r.get("title", ""),
                        "content_snippet": snippet[:600],
                    })
            except Exception as exc:
                log.error("LinkedIn query=%r: %s", site_query, exc)

    return results


# ── Step 2l — Product Hunt scraping (SerpApi site: filter on discussions) ────

def scrape_producthunt(queries: list[str], limit: int = 5) -> list[dict]:
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        log.warning("SERPAPI_KEY missing — skipping Product Hunt scrape.")
        return []

    seen: set[str] = set()
    results: list[dict] = []

    for query in queries[:4]:
        # Target PH posts/discussions where founders express needs — exclude product listings
        site_query = (
            f'site:producthunt.com "{query}" '
            '("looking for" OR "need a" OR "hire" OR "je cherche" OR "seeking") '
            '-inurl:products'
        )
        try:
            search = GoogleSearch({"q": site_query, "api_key": key, "num": limit})
            data = search.get_dict()
            for r in data.get("organic_results", []):
                url = r.get("link", "")
                if url in seen or "producthunt.com" not in url:
                    continue
                seen.add(url)
                snippet = r.get("snippet", "") or r.get("title", "")
                results.append({
                    "id": f"ph_{hashlib.md5(url.encode()).hexdigest()[:10]}",
                    "source": "producthunt",
                    "source_url": url,
                    "author": r.get("displayed_link", "producthunt.com"),
                    "title": r.get("title", ""),
                    "content_snippet": snippet[:600],
                })
        except Exception as exc:
            log.error("ProductHunt query=%r: %s", query, exc)

    return results


# ── Step 2m — Malt scraping (SerpApi — client briefs & job signals) ───────────

def scrape_malt(queries: list[str], limit: int = 5) -> list[dict]:
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        log.warning("SERPAPI_KEY missing — skipping Malt scrape.")
        return []

    seen: set[str] = set()
    results: list[dict] = []

    for query in queries[:4]:
        # Target Malt blog/community pages — skip /profile/ (freelancer CVs, not clients)
        site_query = (
            f'site:malt.fr "{query}" '
            '"je recherche" OR "je cherche" OR "nous cherchons" OR "recrutement" OR "mission" '
            '-inurl:profile'
        )
        try:
            search = GoogleSearch({"q": site_query, "api_key": key, "num": limit})
            data = search.get_dict()
            for r in data.get("organic_results", []):
                url = r.get("link", "")
                if url in seen or "malt.fr" not in url or "/profile/" in url:
                    continue
                seen.add(url)
                snippet = r.get("snippet", "") or r.get("title", "")
                results.append({
                    "id": f"malt_{hashlib.md5(url.encode()).hexdigest()[:10]}",
                    "source": "malt",
                    "source_url": url,
                    "author": r.get("displayed_link", "malt.fr"),
                    "title": r.get("title", ""),
                    "content_snippet": snippet[:600],
                })
        except Exception as exc:
            log.error("Malt query=%r: %s", query, exc)

    return results


# ── Step 3 — Claude batch scoring + outreach generation ──────────────────────

SCORE_PROMPT = """\
You are a sales qualification expert with a very strict filter.

The seller's product/service: <product>{product}</product>

Below is a JSON array of candidate leads scraped from Reddit, YouTube, and Google.

STRICT BUYER INTENT RULE — a lead is only valuable if the author is EXPLICITLY expressing \
a personal need or desire to hire/buy RIGHT NOW. Discard anything that is:
  - Educational content (tutorials, "how to", "tips for", industry discussions)
  - A freelancer/provider advertising their own services
  - General industry talk with no client need expressed
  - News, reviews, or commentary about the field

VALID buying signals (score 50+): "looking for", "need a", "want to hire", "recommend someone", \
"help me find", "je cherche", "besoin d'un", "quelqu'un qui peut", "je veux engager", \
"qui peut m'aider", "asap", "urgent", explicit project descriptions seeking a provider.

For each lead evaluate:
  - intent_score (0–100):
      80-100 = hot  (explicitly hiring/seeking, urgent, clear project)
      50-79  = warm (clear personal pain point, open to solutions)
      1-49   = cold (tangential, informational, or provider — NOT a buyer)
      0      = noise (tutorial, general discussion, no buying signal whatsoever)
  - intent_label: "hot" | "warm" | "cold"
  - ai_summary: one sentence on the specific buying signal found (or why it's cold/noise)
  - suggested_reply: a 2-3 sentence outreach message, friendly, zero corporate jargon, \
specific to their situation. Written in the SAME LANGUAGE as the lead's content.
  - translated_snippet: if the lead content is in English, provide a French translation \
of the content_snippet (max 200 chars). If already in French, return null.

Return ONLY a valid JSON array — no markdown, no extra keys:
[
  {{
    "id": "<same id as input>",
    "intent_score": <number>,
    "intent_label": "<hot|warm|cold>",
    "ai_summary": "<string>",
    "suggested_reply": "<string>",
    "translated_snippet": "<string or null>"
  }},
  ...
]

Leads to evaluate:
{leads_json}
"""

def score_and_enrich(raw_leads: list[dict], product: str) -> list[dict]:
    if not raw_leads:
        return []

    claude = _claude()

    # Build minimal payload to keep tokens low
    payload = [
        {
            "id": lead["id"],
            "source": lead["source"],
            "title": lead.get("title", ""),
            "content": lead["content_snippet"],
        }
        for lead in raw_leads
    ]

    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{
            "role": "user",
            "content": SCORE_PROMPT.format(
                product=product,
                leads_json=json.dumps(payload, ensure_ascii=False, indent=2),
            ),
        }],
    )

    try:
        raw_text = msg.content[0].text.strip()
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```", 2)[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.rsplit("```", 1)[0].strip()
        scores: list[dict] = json.loads(raw_text)
    except (json.JSONDecodeError, IndexError, KeyError) as exc:
        log.error("Failed to parse scoring JSON from Claude: %s", exc)
        return []

    score_by_id = {s["id"]: s for s in scores}

    enriched: list[dict] = []
    for lead in raw_leads:
        scored = score_by_id.get(lead["id"])
        if not scored:
            continue
        enriched.append({**lead, **scored})

    return sorted(enriched, key=lambda x: x.get("intent_score", 0), reverse=True)


# ── Public entry point ────────────────────────────────────────────────────────

def run_pipeline(product: str, sources: Optional[list] = None) -> list:
    """
    Full pipeline: queries → scrape → score.
    Returns a list of enriched lead dicts ready to be saved to DB.
    """
    if sources is None:
        sources = ["reddit", "youtube"]

    log.info("Leado pipeline start | product=%r sources=%s", product, sources)

    # 1 — Generate queries
    queries = generate_search_queries(product)
    log.info("Generated queries: %s", queries)

    raw: list[dict] = []

    # 2 — Scrape
    if "reddit" in sources:
        reddit_leads = scrape_reddit(
            queries.get("reddit_queries", [product]),
            queries.get("target_subreddits", []),
        )
        log.info("Reddit raw leads: %d", len(reddit_leads))
        raw.extend(reddit_leads)

    if "youtube" in sources:
        yt_leads = scrape_youtube(queries.get("youtube_queries", [product]))
        log.info("YouTube raw leads: %d", len(yt_leads))
        raw.extend(yt_leads)

    if "google" in sources:
        google_leads = scrape_google(queries.get("reddit_queries", [product]))
        log.info("Google raw leads: %d", len(google_leads))
        raw.extend(google_leads)

    if "hackernews" in sources:
        hn_leads = scrape_hackernews(queries.get("reddit_queries", [product]))
        log.info("HackerNews raw leads: %d", len(hn_leads))
        raw.extend(hn_leads)

    if "indiehackers" in sources:
        ih_leads = scrape_indiehackers(queries.get("reddit_queries", [product]))
        log.info("IndieHackers raw leads: %d", len(ih_leads))
        raw.extend(ih_leads)

    if "upwork" in sources:
        uw_leads = scrape_upwork(queries.get("reddit_queries", [product]))
        log.info("Upwork raw leads: %d", len(uw_leads))
        raw.extend(uw_leads)

    if "twitter" in sources:
        tw_leads = scrape_twitter(queries.get("reddit_queries", [product]))
        log.info("Twitter raw leads: %d", len(tw_leads))
        raw.extend(tw_leads)

    if "github" in sources:
        gh_leads = scrape_github(queries.get("reddit_queries", [product]))
        log.info("GitHub raw leads: %d", len(gh_leads))
        raw.extend(gh_leads)

    if "quora" in sources:
        qr_leads = scrape_quora(queries.get("reddit_queries", [product]))
        log.info("Quora raw leads: %d", len(qr_leads))
        raw.extend(qr_leads)

    if "codeur" in sources:
        co_leads = scrape_codeur(queries.get("reddit_queries", [product]))
        log.info("Codeur raw leads: %d", len(co_leads))
        raw.extend(co_leads)

    if "linkedin" in sources:
        li_leads = scrape_linkedin(queries.get("reddit_queries", [product]))
        log.info("LinkedIn raw leads: %d", len(li_leads))
        raw.extend(li_leads)

    if "producthunt" in sources:
        ph_leads = scrape_producthunt(queries.get("reddit_queries", [product]))
        log.info("ProductHunt raw leads: %d", len(ph_leads))
        raw.extend(ph_leads)

    if "malt" in sources:
        ma_leads = scrape_malt(queries.get("reddit_queries", [product]))
        log.info("Malt raw leads: %d", len(ma_leads))
        raw.extend(ma_leads)

    if not raw:
        log.warning("No raw leads found — check API credentials.")
        return []

    # Filter low-signal content (emojis, reactions, very short snippets)
    raw = [
        r for r in raw
        if len(r.get("content_snippet", "").encode("ascii", "ignore")) > 25
    ]

    # Deduplicate by content fingerprint (same author + same snippet prefix)
    seen_fp: set[str] = set()
    deduped: list[dict] = []
    for lead in raw:
        fp = hashlib.md5(
            f"{lead['author']}:{lead['content_snippet'][:120]}".encode()
        ).hexdigest()
        if fp not in seen_fp:
            seen_fp.add(fp)
            deduped.append(lead)

    log.info("Deduped leads: %d → %d", len(raw), len(deduped))

    # 3 — Score + enrich (batch, single Claude call)
    enriched = score_and_enrich(deduped, product)
    log.info("Enriched leads: %d", len(enriched))

    return enriched
