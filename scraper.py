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
from typing import Any

import praw
import anthropic
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ── Clients (lazy-initialised so missing keys don't crash import) ─────────────

def _reddit() -> praw.Reddit | None:
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

Return ONLY valid JSON — no markdown fences, no commentary:
{{
  "reddit_queries": ["<5-8 short search phrases for Reddit>"],
  "youtube_queries": ["<3-5 short search phrases for YouTube>"],
  "target_subreddits": ["<8-12 subreddit names without r/, most relevant first>"],
  "buyer_signals": ["<6-10 words/phrases that, if found, strongly suggest buying intent>"]
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


# ── Step 2b — YouTube scraping ────────────────────────────────────────────────

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


# ── Step 3 — Claude batch scoring + outreach generation ──────────────────────

SCORE_PROMPT = """\
You are a sales qualification expert.

The seller's product/service: <product>{product}</product>

Below is a JSON array of candidate leads scraped from Reddit and YouTube.
For each lead evaluate:
  - intent_score (0–100): How likely is this person to need and BUY this service RIGHT NOW?
      80-100 = hot  (explicitly hiring, urgent pain, budget signals)
      50-79  = warm (clear pain point, open to solutions)
      0-49   = cold (tangential, informational, not a buyer)
  - intent_label: "hot" | "warm" | "cold"
  - ai_summary: one sentence explaining WHY this is (or isn't) a strong lead
  - suggested_reply: a 2-3 sentence outreach message. Friendly, specific to their situation, \
zero corporate jargon. Written in the same language as the lead's post.

Return ONLY a valid JSON array — no markdown, no extra keys:
[
  {{
    "id": "<same id as input>",
    "intent_score": <number>,
    "intent_label": "<hot|warm|cold>",
    "ai_summary": "<string>",
    "suggested_reply": "<string>"
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
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": SCORE_PROMPT.format(
                product=product,
                leads_json=json.dumps(payload, ensure_ascii=False, indent=2),
            ),
        }],
    )

    try:
        scores: list[dict] = json.loads(msg.content[0].text)
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

def run_pipeline(product: str, sources: list[str] | None = None) -> list[dict]:
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

    if not raw:
        log.warning("No raw leads found — check API credentials.")
        return []

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
