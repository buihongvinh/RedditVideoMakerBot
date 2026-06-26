import json
import re
import textwrap
from pathlib import Path
from typing import Dict, Final

import translators
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import ViewportSize, sync_playwright
from rich.progress import track

from utils import settings
from utils.console import print_step, print_substep
from utils.imagenarator import imagemaker
from utils.playwright import clear_cookie_by_name
from utils.videos import save_data

__all__ = ["get_screenshots_of_reddit_posts"]

DEFAULT_POST_LANG = "en"
DEFAULT_SCREENSHOT_ZOOM = 1.35


def should_translate(lang: str) -> bool:
    normalized = str(lang or "").strip().lower()
    return normalized not in {"", "en", "en-us"}


POST_LOCATOR_CANDIDATES = [
    '[data-test-id="post-content"]',
    '[data-testid="post-container"]',
    'shreddit-post',
    'article',
    'main article',
]


def _first_visible_locator(page, selectors, *, timeout: int = 3000):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout)
            print_substep(f"Matched screenshot selector: {selector}", style="blue")
            return locator
        except PlaywrightError:
            continue
    return None


def _comment_locator_candidates(comment_id: str):
    return [
        f"#t1_{comment_id}",
        f'[thingid="t1_{comment_id}"]',
        f'[data-fullname="t1_{comment_id}"]',
        f'shreddit-comment#t1_{comment_id}',
        f'article[id="t1_{comment_id}"]',
    ]


def _resolve_comment_capture_locator(page, comment_id: str):
    container = _first_visible_locator(page, _comment_locator_candidates(comment_id), timeout=4000)
    if container is None:
        return None, None

    focused_candidates = [
        'div[id$="-comment-rtjson-content"]',
        '[data-testid="comment"]',
        '[slot="comment"]',
        '[id="comment-tree"] [data-testid="comment"]',
        'div[data-testid="comment"]',
    ]
    for selector in focused_candidates:
        locator = container.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=1500)
            print_substep(f"Matched comment body selector: {selector}", style="blue")
            return container, locator
        except PlaywrightError:
            continue

    print_substep("Using full comment container screenshot.", style="yellow")
    return container, container


def _clip_from_locator(locator, zoom: float = 1.0, *, padding_x: int = 28, padding_y: int = 36):
    location = locator.bounding_box()
    if location is None:
        raise TimeoutError("Could not read bounding box for screenshot target.")

    clip = {
        "x": max(location["x"] - padding_x, 0),
        "y": max(location["y"] - padding_y, 0),
        "width": location["width"] + (padding_x * 2),
        "height": location["height"] + (padding_y * 2),
    }
    if zoom != 1:
        for key in clip:
            clip[key] = float("{:.2f}".format(clip[key] * zoom))
    return clip


def _prepare_capture_layout(page):
    page.evaluate(
        """
        () => {
            document.documentElement.style.scrollPaddingTop = '120px';
            const selectors = [
                'header',
                'nav',
                '[role="banner"]',
                'reddit-header-large',
                'shreddit-app > div:first-child',
                'faceplate-search-input',
                '[data-testid="search-trigger"]'
            ];
            for (const selector of selectors) {
                for (const node of document.querySelectorAll(selector)) {
                    node.style.visibility = 'hidden';
                    node.style.pointerEvents = 'none';
                }
            }
        }
        """
    )


def _screenshot_locator(locator, path: str):
    locator.scroll_into_view_if_needed()
    locator.screenshot(path=path)


def _write_comment_fallback_image(comment: dict, path: str, *, width: int = 900, body_override: str | None = None):
    body = re.sub(r"\s+", " ", body_override or comment.get("comment_body", "")).strip()
    body = body or "[comment unavailable]"
    font = ImageFont.truetype("fonts/Roboto-Bold.ttf", 58)
    padding_x = 42
    padding_y = 34
    line_height = 74
    wrapped = []
    for paragraph in body.splitlines() or [body]:
        wrapped.extend(textwrap.wrap(paragraph, width=28) or [""])
    height = max(180, padding_y * 2 + line_height * len(wrapped))
    image = Image.new("RGBA", (width, height), (17, 22, 27, 232))
    draw = ImageDraw.Draw(image)
    y = padding_y
    for line in wrapped:
        draw.text((padding_x, y), line, fill=(245, 248, 250), font=font)
        y += line_height
    image.save(path)


def get_screenshots_of_reddit_posts(reddit_object: dict, screenshot_num: int):
    """Downloads screenshots of reddit posts as seen on the web. Downloads to assets/temp/png

    Args:
        reddit_object (Dict): Reddit object received from reddit/subreddit.py
        screenshot_num (int): Number of screenshots to download
    """
    # settings values
    W: Final[int] = int(settings.config["settings"]["resolution_w"])
    H: Final[int] = int(settings.config["settings"]["resolution_h"])
    lang: Final[str] = settings.config["reddit"]["thread"]["post_lang"] or DEFAULT_POST_LANG
    storymode: Final[bool] = settings.config["settings"]["storymode"]
    zoom_level: Final[float] = float(settings.config["settings"]["zoom"] or DEFAULT_SCREENSHOT_ZOOM)

    print_step("Downloading screenshots of reddit posts...")
    reddit_id = re.sub(r"[^\w\s-]", "", reddit_object["thread_id"])
    # ! Make sure the reddit screenshots folder exists
    Path(f"assets/temp/{reddit_id}/png").mkdir(parents=True, exist_ok=True)

    # set the theme and turn off non-essential cookies
    if settings.config["settings"]["theme"] == "dark":
        cookie_file = open("./video_creation/data/cookie-dark-mode.json", encoding="utf-8")
        bgcolor = (33, 33, 36, 255)
        txtcolor = (240, 240, 240)
        transparent = False
    elif settings.config["settings"]["theme"] == "transparent":
        if storymode:
            # Transparent theme
            bgcolor = (0, 0, 0, 0)
            txtcolor = (255, 255, 255)
            transparent = True
            cookie_file = open("./video_creation/data/cookie-dark-mode.json", encoding="utf-8")
        else:
            # Switch to dark theme
            cookie_file = open("./video_creation/data/cookie-dark-mode.json", encoding="utf-8")
            bgcolor = (33, 33, 36, 255)
            txtcolor = (240, 240, 240)
            transparent = False
    else:
        cookie_file = open("./video_creation/data/cookie-light-mode.json", encoding="utf-8")
        bgcolor = (255, 255, 255, 255)
        txtcolor = (0, 0, 0)
        transparent = False

    if storymode and settings.config["settings"]["storymodemethod"] == 1:
        print_substep("Generating images...")
        return imagemaker(
            theme=bgcolor,
            reddit_obj=reddit_object,
            txtclr=txtcolor,
            transparent=transparent,
        )

    screenshot_num: int
    with sync_playwright() as p:
        print_substep("Launching Headless Browser...")

        browser = p.chromium.launch(
            headless=True
        )  # headless=False will show the browser for debugging purposes
        # Device scale factor (or dsf for short) allows us to increase the resolution of the screenshots
        # When the dsf is 1, the width of the screenshot is 600 pixels
        # so we need a dsf such that the width of the screenshot is greater than the final resolution of the video
        dsf = (W // 600) + 1

        context = browser.new_context(
            locale=lang or "en-US,en;q=0.9",
            color_scheme="dark",
            viewport=ViewportSize(width=W, height=H),
            device_scale_factor=dsf,
            user_agent=f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version}.0.0.0 Safari/537.36",
            extra_http_headers={
                "Dnt": "1",
                "Sec-Ch-Ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
            },
        )
        cookies = json.load(cookie_file)
        cookie_file.close()

        context.add_cookies(cookies)  # load preference cookies

        page = context.new_page()
        print_substep("Opening Reddit without login...")
        page.goto("https://www.reddit.com", timeout=0)
        page.set_viewport_size(ViewportSize(width=1920, height=1080))
        page.wait_for_load_state()
        page.wait_for_timeout(3000)
        # Handle the redesign
        # Check if the redesign optout cookie is set
        if page.locator("#redesign-beta-optin-btn").is_visible():
            # Clear the redesign optout cookie
            clear_cookie_by_name(context, "redesign_optout")
            # Reload the page for the redesign to take effect
            page.reload()
        # Get the thread screenshot
        page.goto(reddit_object["thread_url"], timeout=0)
        page.set_viewport_size(ViewportSize(width=W, height=H))
        page.wait_for_load_state()
        page.wait_for_timeout(5000)
        _prepare_capture_layout(page)

        if page.locator('button:has-text("Accept all")').first.is_visible():
            page.locator('button:has-text("Accept all")').first.click()
            page.wait_for_timeout(1000)
        if page.locator('button:has-text("Continue")').first.is_visible():
            page.locator('button:has-text("Continue")').first.click()
            page.wait_for_timeout(1000)

        if page.locator(
            "#t3_12hmbug > div > div._3xX726aBn29LDbsDtzr_6E._1Ap4F5maDtT1E1YuCiaO0r.D3IL3FD0RFy_mkKLPwL4 > div > div > button"
        ).is_visible():
            # This means the post is NSFW and requires to click the proceed button.

            print_substep("Post is NSFW. You are spicy...")
            page.locator(
                "#t3_12hmbug > div > div._3xX726aBn29LDbsDtzr_6E._1Ap4F5maDtT1E1YuCiaO0r.D3IL3FD0RFy_mkKLPwL4 > div > div > button"
            ).click()
            page.wait_for_load_state()  # Wait for page to fully load

            # translate code
        if page.locator(
            "#SHORTCUT_FOCUSABLE_DIV > div:nth-child(7) > div > div > div > header > div > div._1m0iFpls1wkPZJVo38-LSh > button > i"
        ).is_visible():
            page.locator(
                "#SHORTCUT_FOCUSABLE_DIV > div:nth-child(7) > div > div > div > header > div > div._1m0iFpls1wkPZJVo38-LSh > button > i"
            ).click()  # Interest popup is showing, this code will close it

        if should_translate(lang):
            print_substep("Translating post...")
            texts_in_tl = translators.translate_text(
                reddit_object["thread_title"],
                to_language=lang,
                translator="google",
            )

            page.evaluate(
                """
                tl_content => {
                    const titleNode =
                      document.querySelector('[data-adclicklocation="title"] > div > div > h1') ||
                      document.querySelector('h1');
                    if (titleNode) {
                        titleNode.textContent = tl_content;
                    }
                }
                """,
                texts_in_tl,
            )
        else:
            print_substep("Skipping translation...")

        postcontentpath = f"assets/temp/{reddit_id}/png/title.png"
        try:
            post_locator = _first_visible_locator(page, POST_LOCATOR_CANDIDATES, timeout=4000)
            if post_locator is None:
                if storymode:
                    raise TimeoutError("Unable to locate a visible Reddit post container.")
                print_substep(
                    "No Reddit post container matched current selectors. Skipping title screenshot in comment mode.",
                    style="yellow",
                )
            else:
                if zoom_level != 1:
                    page.evaluate("document.body.style.zoom=" + str(zoom_level))
                    _screenshot_locator(post_locator, postcontentpath)
                else:
                    _screenshot_locator(post_locator, postcontentpath)
        except Exception as e:
            print_substep("Something went wrong!", style="red")
            resp = input(
                "Something went wrong with making the screenshots! Do you want to skip the post? (y/n) "
            )

            if resp.casefold().startswith("y"):
                save_data("", "", "skipped", reddit_id, "")
                print_substep(
                    "The post is successfully skipped! You can now restart the program and this post will skipped.",
                    "green",
                )

            resp = input("Do you want the error traceback for debugging purposes? (y/n)")
            if not resp.casefold().startswith("y"):
                exit()

            raise e

        if storymode:
            page.locator('[data-click-id="text"]').first.screenshot(
                path=f"assets/temp/{reddit_id}/png/story_content.png"
            )
        else:
            captured_comments = []
            for idx, comment in enumerate(
                track(
                    reddit_object["comments"][:screenshot_num],
                    "Generating matching comment cards...",
                )
            ):
                output_path = f"assets/temp/{reddit_id}/png/comment_{idx}.png"
                card_text = comment["comment_body"]
                if should_translate(lang):
                    card_text = translators.translate_text(
                        comment["comment_body"],
                        translator="google",
                        to_language=lang,
                    )
                _write_comment_fallback_image(comment, path=output_path, body_override=card_text)
                captured_comments.append(comment)
            reddit_object["comments"] = captured_comments

        # close browser instance when we are done using it
        browser.close()

    print_substep("Screenshots downloaded Successfully.", style="bold green")
    if storymode:
        return screenshot_num
    return len(reddit_object.get("comments", [])[:screenshot_num])
