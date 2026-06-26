import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from requests import HTTPError

from utils import settings
from utils.ai_methods import sort_by_similarity
from utils.console import print_step, print_substep
from utils.posttextparser import posttextparser
from utils.subreddit import _contains_blocked_words, already_done
from utils.voice import sanitize_text

REDDIT_BASE_URL = "https://www.reddit.com"
USER_AGENT = "RedditVideoMakerBot/3.4.0 (public-json mode)"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


class RedditPublicJsonForbidden(RuntimeError):
    pass


@dataclass
class PublicComment:
    body: str
    permalink: str
    comment_id: str
    stickied: bool = False
    author: Optional[str] = None


@dataclass
class PublicSubmission:
    id: str
    title: str
    selftext: str
    over_18: bool
    stickied: bool
    num_comments: int
    is_self: bool
    score: int
    upvote_ratio: float
    permalink: str
    comments: List[PublicComment] = field(default_factory=list)

    def __str__(self) -> str:
        return self.id


def _reddit_get_json(path: str, params: Optional[dict] = None):
    response = requests.get(
        f"{REDDIT_BASE_URL}{path}",
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    try:
        response.raise_for_status()
    except HTTPError as exc:
        if response.status_code == 403:
            raise RedditPublicJsonForbidden("Reddit returned 403 for the public JSON endpoint.") from exc
        raise
    return response.json()


def _parse_submission(data: dict, comments: Optional[List[PublicComment]] = None) -> PublicSubmission:
    return PublicSubmission(
        id=data["id"],
        title=data.get("title", ""),
        selftext=data.get("selftext", ""),
        over_18=bool(data.get("over_18")),
        stickied=bool(data.get("stickied")),
        num_comments=int(data.get("num_comments", 0)),
        is_self=bool(data.get("is_self")),
        score=int(data.get("score", 0)),
        upvote_ratio=float(data.get("upvote_ratio") or 0),
        permalink=data.get("permalink", ""),
        comments=comments or [],
    )


def _flatten_top_level_comments(children: Iterable[dict]) -> List[PublicComment]:
    comments: List[PublicComment] = []
    for child in children:
        if child.get("kind") != "t1":
            continue
        data = child.get("data", {})
        body = data.get("body", "")
        if body in ["[removed]", "[deleted]"]:
            continue
        comments.append(
            PublicComment(
                body=body,
                permalink=data.get("permalink", ""),
                comment_id=data.get("id", ""),
                stickied=bool(data.get("stickied")),
                author=data.get("author"),
            )
        )
    return comments


def _fetch_submission_comments(post_id: str) -> Tuple[PublicSubmission, List[PublicComment]]:
    payload = _reddit_get_json(
        f"/comments/{post_id}.json",
        params={"limit": 500, "sort": "top", "raw_json": 1},
    )
    post_listing = payload[0]["data"]["children"][0]["data"]
    comments_listing = payload[1]["data"]["children"]
    comments = _flatten_top_level_comments(comments_listing)
    submission = _parse_submission(post_listing, comments=comments)
    return submission, comments


def _fetch_listing(subreddit_name: str, sort: str, *, limit: int, time_filter: Optional[str] = None):
    params = {"limit": limit, "raw_json": 1}
    if time_filter:
        params["t"] = time_filter
    payload = _reddit_get_json(f"/r/{subreddit_name}/{sort}.json", params=params)
    return [_parse_submission(child["data"]) for child in payload["data"]["children"]]


def _load_done_videos() -> list:
    from json import load
    from os.path import exists

    done_path = "./video_creation/data/videos.json"
    if not exists(done_path):
        with open(done_path, "w", encoding="utf-8") as file:
            file.write("[]")
    with open(done_path, "r", encoding="utf-8") as file:
        return load(file)


def _select_submission(subreddit_name: str) -> Tuple[Optional[PublicSubmission], float]:
    similarity_score = 0.0
    done_videos = _load_done_videos()
    storymode = settings.config["settings"]["storymode"]
    allow_nsfw = settings.config["settings"]["allow_nsfw"]
    min_comments = int(settings.config["reddit"]["thread"]["min_comments"])
    storymode_max_length = settings.config["settings"]["storymode_max_length"] or 2000
    valid_time_filters = [None, "day", "hour", "month", "week", "year", "all"]

    for time_filter in valid_time_filters:
        if settings.config["ai"]["ai_similarity_enabled"]:
            limit = 50
        else:
            limit = 25 if time_filter is None else 50

        submissions = _fetch_listing(
            subreddit_name,
            "hot" if time_filter is None else "top",
            limit=limit,
            time_filter=time_filter,
        )

        similarity_scores = None
        if settings.config["ai"]["ai_similarity_enabled"]:
            keywords = settings.config["ai"]["ai_similarity_keywords"].split(",")
            keywords = [keyword.strip() for keyword in keywords if keyword.strip()]
            if keywords:
                print(f"Sorting threads by similarity to the given keywords: {', '.join(keywords)}")
                submissions, similarity_scores = sort_by_similarity(submissions, keywords)

        for index, submission in enumerate(submissions):
            if already_done(done_videos, submission):
                continue
            if submission.over_18 and not allow_nsfw:
                print_substep("NSFW Post Detected. Skipping...")
                continue
            if submission.stickied:
                print_substep("This post was pinned by moderators. Skipping...")
                continue
            if _contains_blocked_words(submission.title + " " + (submission.selftext or "")):
                print_substep("Post contains a blocked word. Skipping...")
                continue
            if not storymode and submission.num_comments <= min_comments:
                print_substep(
                    f"This post has under the specified minimum of comments ({min_comments}). Skipping..."
                )
                continue
            if storymode:
                if not submission.selftext:
                    print_substep("You are trying to use story mode on post with no post text")
                    continue
                if len(submission.selftext) > storymode_max_length:
                    print_substep(
                        f"Post is too long ({len(submission.selftext)}), try with a different post. ({storymode_max_length} character limit)"
                    )
                    continue
                if len(submission.selftext) < 30:
                    continue
                if not submission.is_self:
                    continue

            if similarity_scores is not None:
                similarity_score = similarity_scores[index].item()
            return submission, similarity_score

    return None, similarity_score


def _filter_comment(comment: PublicComment) -> bool:
    if _contains_blocked_words(comment.body):
        return False
    if comment.stickied:
        return False
    sanitised = sanitize_text(comment.body)
    if not sanitised or sanitised == " ":
        return False
    if len(comment.body) > int(settings.config["reddit"]["thread"]["max_comment_length"]):
        return False
    if len(comment.body) < int(settings.config["reddit"]["thread"]["min_comment_length"]):
        return False
    if comment.author is None or sanitize_text(comment.body) is None:
        return False
    return True


def _safe_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _post_id_from_url(url: str) -> str:
    match = re.search(r"/comments/([a-zA-Z0-9]+)/", url)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if parts else "reddit-post"


def _split_subreddit_choices(subreddit_choice: str) -> List[str]:
    return [
        part[2:] if part.casefold().startswith("r/") else part
        for part in (sub.strip() for sub in str(subreddit_choice).split("+"))
        if part
    ]


def _wait_for_reddit_content(page) -> None:
    selectors = [
        "shreddit-post",
        '[data-test-id="post-content"]',
        '[data-testid="post-container"]',
        "article",
        "h1",
    ]
    for selector in selectors:
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=6000)
            return
        except PlaywrightError:
            continue


def _extract_post_links(page, limit: int) -> List[str]:
    return page.evaluate(
        """
        limit => {
            const seen = new Set();
            const urls = [];
            for (const link of document.querySelectorAll('a[href*="/comments/"]')) {
                const href = link.href || link.getAttribute('href') || '';
                const match = href.match(/\\/comments\\/[a-zA-Z0-9]+\\//);
                if (!match) continue;
                const url = new URL(href, location.origin).toString();
                if (seen.has(url)) continue;
                seen.add(url);
                urls.push(url);
                if (urls.length >= limit) break;
            }
            return urls;
        }
        """,
        limit,
    )


def _extract_submission_from_page(page) -> PublicSubmission:
    data = page.evaluate(
        """
        () => {
            const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
            const firstText = selectors => {
                for (const selector of selectors) {
                    const node = document.querySelector(selector);
                    const text = clean(node && node.innerText);
                    if (text) return text;
                }
                return '';
            };
            const title = firstText([
                'shreddit-post h1',
                '[data-test-id="post-content"] h1',
                '[data-testid="post-container"] h1',
                'article h1',
                'h1'
            ]);
            const selftext = firstText([
                'shreddit-post [slot="text-body"]',
                'shreddit-post [data-testid="post-content"]',
                '[data-click-id="text"]',
                '[data-test-id="post-content"] [data-click-id="text"]'
            ]);
            const commentNodes = [
                ...document.querySelectorAll('shreddit-comment'),
                ...document.querySelectorAll('[thingid^="t1_"]'),
                ...document.querySelectorAll('[id^="t1_"]')
            ];
            const seen = new Set();
            const comments = [];
            for (const node of commentNodes) {
                const idAttr = node.getAttribute('thingid') || node.id || '';
                const id = idAttr.replace(/^t1_/, '');
                if (!id || seen.has(id)) continue;
                seen.add(id);
                const bodyNode =
                    node.querySelector('[slot="comment"]') ||
                    node.querySelector('[data-testid="comment"]') ||
                    node.querySelector('div[id$="-comment-rtjson-content"]') ||
                    node;
                const body = clean(bodyNode && bodyNode.innerText);
                if (!body || body === '[removed]' || body === '[deleted]') continue;
                const linkNode = node.querySelector('a[href*="/comments/"]');
                comments.push({
                    body,
                    id,
                    permalink: linkNode ? new URL(linkNode.getAttribute('href'), location.origin).pathname : location.pathname,
                    author: node.getAttribute('author') || 'reddit-user',
                    stickied: node.hasAttribute('stickied') || node.getAttribute('stickied') === 'true'
                });
            }
            return {
                title,
                selftext,
                url: location.href,
                over18: Boolean(document.querySelector('[data-testid="content-gate"], shreddit-blurred-container')),
                comments
            };
        }
        """
    )
    comments = [
        PublicComment(
            body=_safe_text(comment.get("body")),
            permalink=comment.get("permalink") or urlparse(page.url).path,
            comment_id=comment.get("id") or f"browser_{index}",
            stickied=bool(comment.get("stickied")),
            author=comment.get("author") or "reddit-user",
        )
        for index, comment in enumerate(data.get("comments", []))
    ]
    return PublicSubmission(
        id=_post_id_from_url(data.get("url") or page.url),
        title=_safe_text(data.get("title")) or "Reddit post",
        selftext=_safe_text(data.get("selftext")),
        over_18=bool(data.get("over18")),
        stickied=False,
        num_comments=len(comments),
        is_self=True,
        score=0,
        upvote_ratio=0,
        permalink=urlparse(data.get("url") or page.url).path,
        comments=comments,
    )


def _fetch_submission_with_browser(page, url: str) -> PublicSubmission:
    print_substep(f"Opening Reddit in browser fallback: {url}", style="yellow")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    _wait_for_reddit_content(page)
    page.wait_for_timeout(3000)
    return _extract_submission_from_page(page)


def _select_submission_with_browser(subreddit_choice: str, post_id: str) -> Tuple[PublicSubmission, float]:
    configured_post_id = settings.config["reddit"]["thread"]["post_id"]
    target_post_id = post_id or (configured_post_id if configured_post_id and "+" not in str(configured_post_id) else "")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=BROWSER_USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale=settings.config["reddit"]["thread"]["post_lang"] or "en-US",
        )
        page = context.new_page()
        try:
            if target_post_id:
                submission = _fetch_submission_with_browser(
                    page, f"{REDDIT_BASE_URL}/comments/{target_post_id}/"
                )
            else:
                done_videos = _load_done_videos()
                submission = None
                for subreddit_name in _split_subreddit_choices(subreddit_choice):
                    listing_url = f"{REDDIT_BASE_URL}/r/{subreddit_name}/hot/"
                    print_substep(f"Opening subreddit listing in browser fallback: {listing_url}", style="yellow")
                    page.goto(listing_url, wait_until="domcontentloaded", timeout=60000)
                    _wait_for_reddit_content(page)
                    page.wait_for_timeout(3000)
                    post_links = _extract_post_links(page, 12)
                    if not post_links:
                        print_substep(f"No post links found in r/{subreddit_name}. Trying next subreddit.", style="yellow")
                        continue
                    for link in post_links:
                        candidate = _fetch_submission_with_browser(page, link)
                        if already_done(done_videos, candidate):
                            continue
                        submission = candidate
                        break
                    if submission is not None:
                        break
                if submission is None:
                    raise RuntimeError("Browser fallback could not find a usable Reddit post.")
        finally:
            browser.close()

    return submission, 0.0


def _build_content_from_submission(submission: PublicSubmission, similarity_score: float):
    upvotes = submission.score
    ratio = submission.upvote_ratio * 100
    num_comments = submission.num_comments
    threadurl = f"https://new.reddit.com{submission.permalink}"

    print_substep(f"Video will be: {submission.title} :thumbsup:", style="bold green")
    print_substep(f"Thread url is: {threadurl} :thumbsup:", style="bold green")
    print_substep(f"Thread has {upvotes} upvotes", style="bold blue")
    print_substep(f"Thread has a upvote ratio of {ratio}%", style="bold blue")
    print_substep(f"Thread has {num_comments} comments", style="bold blue")
    if similarity_score:
        print_substep(
            f"Thread has a similarity score up to {round(similarity_score * 100)}%",
            style="bold blue",
        )

    content = {
        "thread_url": threadurl,
        "thread_title": submission.title,
        "thread_id": submission.id,
        "is_nsfw": submission.over_18,
        "comments": [],
    }

    if settings.config["settings"]["storymode"]:
        if settings.config["settings"]["storymodemethod"] == 1:
            content["thread_post"] = posttextparser(submission.selftext)
        else:
            content["thread_post"] = submission.selftext
    else:
        for top_level_comment in submission.comments:
            if top_level_comment.body in ["[removed]", "[deleted]"]:
                continue
            if _contains_blocked_words(top_level_comment.body):
                continue
            if top_level_comment.stickied:
                continue
            sanitised = sanitize_text(top_level_comment.body)
            if not sanitised or sanitised == " ":
                continue
            if len(top_level_comment.body) > int(settings.config["reddit"]["thread"]["max_comment_length"]):
                continue
            if len(top_level_comment.body) < int(settings.config["reddit"]["thread"]["min_comment_length"]):
                continue
            if top_level_comment.author is None or sanitize_text(top_level_comment.body) is None:
                continue
            content["comments"].append(
                {
                    "comment_body": top_level_comment.body,
                    "comment_url": top_level_comment.permalink,
                    "comment_id": top_level_comment.comment_id,
                }
            )

    print_substep("Received subreddit threads Successfully.", style="bold green")
    return content


def get_subreddit_threads(POST_ID: str):
    """
    Returns a reddit object built from Reddit's public JSON endpoints.
    """

    print_step("Getting subreddit threads...")
    similarity_score = 0.0

    if not settings.config["reddit"]["thread"]["subreddit"]:
        subreddit_choice = re.sub(r"r\/", "", input("What subreddit would you like to pull from? "))
        if not subreddit_choice:
            subreddit_choice = "askreddit"
            print_substep("Subreddit not defined. Using AskReddit.")
    else:
        subreddit_choice = settings.config["reddit"]["thread"]["subreddit"]
        print_substep(f"Using subreddit: r/{subreddit_choice} from TOML config")
        if str(subreddit_choice).casefold().startswith("r/"):
            subreddit_choice = subreddit_choice[2:]

    print_substep("Using Reddit public JSON endpoints. No login required.")
    try:
        if POST_ID:
            submission, _ = _fetch_submission_comments(POST_ID)
        elif (
            settings.config["reddit"]["thread"]["post_id"]
            and len(str(settings.config["reddit"]["thread"]["post_id"]).split("+")) == 1
        ):
            submission, _ = _fetch_submission_comments(settings.config["reddit"]["thread"]["post_id"])
        else:
            submission, similarity_score = _select_submission(subreddit_choice)
            if submission is None:
                raise RuntimeError("Unable to find a suitable public Reddit post to use.")
            submission, _ = _fetch_submission_comments(submission.id)
    except RedditPublicJsonForbidden:
        print_substep("Public JSON returned 403. Falling back to browser mode.", style="yellow")
        submission, similarity_score = _select_submission_with_browser(subreddit_choice, POST_ID)

    if not submission.num_comments and not settings.config["settings"]["storymode"]:
        print_substep("No comments found. Skipping.")
        exit()

    upvotes = submission.score
    ratio = submission.upvote_ratio * 100
    num_comments = submission.num_comments
    threadurl = f"https://new.reddit.com{submission.permalink}"

    print_substep(f"Video will be: {submission.title} :thumbsup:", style="bold green")
    print_substep(f"Thread url is: {threadurl} :thumbsup:", style="bold green")
    print_substep(f"Thread has {upvotes} upvotes", style="bold blue")
    print_substep(f"Thread has a upvote ratio of {ratio}%", style="bold blue")
    print_substep(f"Thread has {num_comments} comments", style="bold blue")
    if similarity_score:
        print_substep(
            f"Thread has a similarity score up to {round(similarity_score * 100)}%",
            style="bold blue",
        )

    content = {
        "thread_url": threadurl,
        "thread_title": submission.title,
        "thread_id": submission.id,
        "is_nsfw": submission.over_18,
        "comments": [],
    }

    if settings.config["settings"]["storymode"]:
        if settings.config["settings"]["storymodemethod"] == 1:
            content["thread_post"] = posttextparser(submission.selftext)
        else:
            content["thread_post"] = submission.selftext
    else:
        for top_level_comment in submission.comments:
            if _filter_comment(top_level_comment):
                content["comments"].append(
                    {
                        "comment_body": top_level_comment.body,
                        "comment_url": top_level_comment.permalink,
                        "comment_id": top_level_comment.comment_id,
                    }
                )

    print_substep("Received subreddit threads Successfully.", style="bold green")
    return content
