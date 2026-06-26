import json
import random
import re
import subprocess
from pathlib import Path
from random import randrange
from typing import Any, Dict, Tuple
from urllib.parse import quote

import requests
import yt_dlp
from moviepy import AudioClip, AudioFileClip, ColorClip, VideoFileClip, concatenate_audioclips, concatenate_videoclips
from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip
from playwright.sync_api import sync_playwright

from utils import settings
from utils.console import print_step, print_substep


def load_background_options():
    _background_options = {}
    # Load background videos
    with open("./utils/background_videos.json") as json_file:
        _background_options["video"] = json.load(json_file)

    # Load background audios
    with open("./utils/background_audios.json") as json_file:
        _background_options["audio"] = json.load(json_file)

    # Remove "__comment" from backgrounds
    del _background_options["video"]["__comment"]
    del _background_options["audio"]["__comment"]

    for name in list(_background_options["video"].keys()):
        pos = _background_options["video"][name][3]

        if pos != "center":
            _background_options["video"][name][3] = lambda t: ("center", pos + t)

    return _background_options


def get_start_and_end_times(video_length: int, length_of_clip: int) -> Tuple[int, int]:
    """Generates a random interval of time to be used as the background of the video.

    Args:
        video_length (int): Length of the video
        length_of_clip (int): Length of the video to be used as the background

    Returns:
        tuple[int,int]: Start and end time of the randomized interval
    """
    if int(length_of_clip) <= int(video_length):
        return 0, int(length_of_clip)
    initialValue = 180
    # Issue #1649 - Ensures that will be a valid interval in the video
    while int(length_of_clip) <= int(video_length + initialValue):
        if initialValue == initialValue // 2:
            raise Exception("Your background is too short for this video length")
        else:
            initialValue //= 2  # Divides the initial value by 2 until reach 0
    random_time = randrange(initialValue, int(length_of_clip) - int(video_length))
    return random_time, random_time + video_length


def loop_audio_to_length(audio: AudioFileClip, duration: int) -> AudioFileClip:
    if audio.duration >= duration:
        start_time, end_time = get_start_and_end_times(duration, audio.duration)
        return audio.subclipped(start_time, end_time)
    loops = int(duration // audio.duration) + 1
    return concatenate_audioclips([audio] * loops).subclipped(0, duration)


def loop_video_to_length(video: VideoFileClip, duration: int) -> VideoFileClip:
    if video.duration >= duration:
        start_time, end_time = get_start_and_end_times(duration, video.duration)
        return video.subclipped(start_time, end_time)
    loops = int(duration // video.duration) + 1
    return concatenate_videoclips([video] * loops, method="compose").subclipped(0, duration)


def write_looped_video_file(source_path: str, output_path: str, duration: int, source_duration: float):
    start_time = 0
    if source_duration > duration:
        start_time, _ = get_start_and_end_times(duration, source_duration)
    command = [
        "ffmpeg",
        "-y",
        "-stream_loop",
        "-1",
        "-ss",
        str(start_time),
        "-i",
        source_path,
        "-t",
        str(duration),
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        output_path,
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def get_background_config(mode: str):
    """Fetch the background/s configuration"""
    try:
        choice = str(settings.config["settings"]["background"][f"background_{mode}"]).casefold()
    except AttributeError:
        choice = "random"

    if not choice or choice == "random":
        source_prefixes = ("pexels://", "generated://") if mode == "video" else ("pixabay://", "generated://")
        random_choices = [
            name
            for name, option in background_options[mode].items()
            if str(option[0]).startswith(source_prefixes)
        ]
        choice = random.choice(random_choices or list(background_options[mode].keys()))
        print_substep(f"Random background {mode}: {choice}", style="bold blue")
    elif choice not in background_options[mode]:
        print_substep(f"Unknown background {mode} '{choice}'. Picking random instead.", style="yellow")
        choice = random.choice(list(background_options[mode].keys()))

    return background_options[mode][choice]


def is_generated_source(uri: str) -> bool:
    return str(uri).startswith("generated://")


def is_youtube_source(uri: str) -> bool:
    return "youtube.com" in str(uri) or "youtu.be" in str(uri)


def is_pexels_source(uri: str) -> bool:
    return str(uri).startswith("pexels://")


def is_pixabay_source(uri: str) -> bool:
    return str(uri).startswith("pixabay://")


def download_direct_file(uri: str, output_path: str):
    with requests.get(uri, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(output_path, "wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)


def browser_page_media_url(url: str, script: str, *, wait_ms: int = 5000) -> str:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        media_url = page.evaluate(script)
        browser.close()
        if not media_url:
            raise RuntimeError(f"No media URL found at {url}")
        return media_url


def resolve_pexels_video_url(keyword: str) -> str:
    search_url = f"https://www.pexels.com/search/videos/{quote(keyword)}/"
    return browser_page_media_url(
        search_url,
        """
        () => {
            const videos = [...document.querySelectorAll('video')]
                .map(video => video.currentSrc || video.src)
                .filter(src => src && src.includes('videos.pexels.com') && src.endsWith('.mp4'));
            return videos.find(src => /_360_640_|_720_1280_|_1080_1920_/.test(src)) || videos[0] || '';
        }
        """,
    )


def resolve_pixabay_audio_url(category: str) -> str:
    search_url = f"https://pixabay.com/music/search/{quote(category)}/"
    detail_url = browser_page_media_url(
        search_url,
        """
        () => {
            const links = [...document.querySelectorAll('a')]
                .map(link => link.href)
                .filter(href => /\\/music\\/[a-z0-9-]+\\d+\\/$/i.test(href));
            return links[0] || '';
        }
        """,
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
            )
        )
        page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        try:
            page.locator(".playOverlay--rDjnr").first.click(timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(4000)
        audio_url = page.evaluate(
            """
            () => {
                const htmlMatches = document.documentElement.innerHTML.match(/https:\\/\\/[^"'<> ]+?\\.mp3[^"'<> ]*/g) || [];
                const download = htmlMatches.find(url => url.includes('cdn.pixabay.com/download/audio/'));
                if (download) return download.replaceAll('&amp;', '&');
                const resources = performance.getEntriesByType('resource')
                    .map(resource => resource.name)
                    .filter(url => url.includes('cdn.pixabay.com/audio/') && url.endsWith('.mp3'));
                return resources[0] || htmlMatches[0] || '';
            }
            """
        )
        browser.close()
    if not audio_url:
        raise RuntimeError(f"No Pixabay audio URL found for {category}")
    return audio_url


def download_background_video(background_config: Tuple[str, str, str, Any]):
    """Downloads or prepares the background video source."""
    Path("./assets/backgrounds/video/").mkdir(parents=True, exist_ok=True)
    # note: make sure the file name doesn't include an - in it
    uri, filename, credit, _ = background_config
    if is_generated_source(uri):
        print_substep(f"Using generated background video: {filename}", style="bold green")
        return
    if Path(f"assets/backgrounds/video/{credit}-{filename}").is_file():
        return
    print_step(
        "We need to prepare the background video. Large files are downloaded only once."
    )
    print_substep("Downloading the backgrounds videos... please be patient 🙏 ")
    print_substep(f"Downloading {filename} from {uri}")

    try:
        if is_youtube_source(uri):
            ydl_opts = {
                "format": "bestvideo[height<=1080][ext=mp4]",
                "outtmpl": f"assets/backgrounds/video/{credit}-{filename}",
                "retries": 10,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download(uri)
        elif is_pexels_source(uri):
            media_url = resolve_pexels_video_url(uri.replace("pexels://", "", 1))
            download_direct_file(media_url, f"assets/backgrounds/video/{credit}-{filename}")
        else:
            download_direct_file(uri, f"assets/backgrounds/video/{credit}-{filename}")
        print_substep("Background video downloaded successfully! 🎉", style="bold green")
    except Exception as error:
        print_substep(
            f"Background video download failed ({error}). A local fallback background will be generated.",
            style="yellow",
        )


def download_background_audio(background_config: Tuple[str, str, str]):
    """Downloads or prepares the background audio source."""
    Path("./assets/backgrounds/audio/").mkdir(parents=True, exist_ok=True)
    # note: make sure the file name doesn't include an - in it
    uri, filename, credit = background_config
    if is_generated_source(uri):
        print_substep(f"Using generated background audio: {filename}", style="bold green")
        return
    if Path(f"assets/backgrounds/audio/{credit}-{filename}").is_file():
        return
    print_step(
        "We need to prepare the background audio. Large files are downloaded only once."
    )
    print_substep("Downloading the backgrounds audio... please be patient 🙏 ")
    print_substep(f"Downloading {filename} from {uri}")

    try:
        if is_youtube_source(uri):
            ydl_opts = {
                "outtmpl": f"./assets/backgrounds/audio/{credit}-{filename}",
                "format": "bestaudio/best",
                "extract_audio": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([uri])
        elif is_pixabay_source(uri):
            media_url = resolve_pixabay_audio_url(uri.replace("pixabay://", "", 1))
            download_direct_file(media_url, f"assets/backgrounds/audio/{credit}-{filename}")
        else:
            download_direct_file(uri, f"assets/backgrounds/audio/{credit}-{filename}")
        print_substep("Background audio downloaded successfully! 🎉", style="bold green")
    except Exception as error:
        print_substep(
            f"Background audio download failed ({error}). A silent fallback track will be generated.",
            style="yellow",
        )


def create_fallback_background_video(output_path: str, duration: int):
    print_substep("Generating royalty-free procedural background video locally...", style="yellow")
    video_duration = max(int(duration), 1)
    W = int(settings.config["settings"]["resolution_w"])
    H = int(settings.config["settings"]["resolution_h"])
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        (
            f"gradients=s={W}x{H}:r=30:d={video_duration}:"
            "c0=0b1020:c1=18314f:c2=124236:c3=3d1b4f:"
            "n=4:type=spiral:speed=0.045"
        ),
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        output_path,
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        clip = ColorClip(size=(W, H), color=(20, 24, 32), duration=video_duration)
        clip.write_videofile(output_path, fps=30, audio=False, logger=None)
        clip.close()


def create_fallback_background_audio(output_path: str, duration: int):
    print_substep("Generating royalty-free ambient background music locally...", style="yellow")
    audio_duration = max(int(duration), 1)
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=220:duration={audio_duration}:sample_rate=44100",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=277.18:duration={audio_duration}:sample_rate=44100",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=329.63:duration={audio_duration}:sample_rate=44100",
        "-filter_complex",
        (
            "[0:a]volume=0.035[a0];"
            "[1:a]volume=0.025[a1];"
            "[2:a]volume=0.02[a2];"
            "[a0][a1][a2]amix=inputs=3:duration=longest,"
            "afade=t=in:st=0:d=2,"
            f"afade=t=out:st={max(audio_duration - 2, 0)}:d=2"
        ),
        "-q:a",
        "5",
        output_path,
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        silence = AudioClip(lambda t: 0, duration=audio_duration, fps=44100)
        silence.write_audiofile(output_path, fps=44100, logger=None)
        silence.close()


def chop_background(background_config: Dict[str, Tuple], video_length: int, reddit_object: dict):
    """Generates the background audio and footage to be used in the video and writes it to assets/temp/background.mp3 and assets/temp/background.mp4

    Args:
        reddit_object (Dict[str,str]) : Reddit object
        background_config (Dict[str,Tuple]]) : Current background configuration
        video_length (int): Length of the clip where the background footage is to be taken out of
    """
    thread_id = re.sub(r"[^\w\s-]", "", reddit_object["thread_id"])

    if settings.config["settings"]["background"][f"background_audio_volume"] == 0:
        print_step("Volume was set to 0. Skipping background audio creation . . .")
    else:
        audio_choice = f"{background_config['audio'][2]}-{background_config['audio'][1]}"
        audio_source = f"assets/backgrounds/audio/{audio_choice}"
        if Path(audio_source).is_file():
            print_step("Finding a spot in the backgrounds audio to chop...✂️")
            background_audio = AudioFileClip(audio_source)
            background_audio = loop_audio_to_length(background_audio, video_length)
            background_audio.write_audiofile(f"assets/temp/{thread_id}/background.mp3")
            background_audio.close()
        else:
            create_fallback_background_audio(f"assets/temp/{thread_id}/background.mp3", video_length)

    video_choice = f"{background_config['video'][2]}-{background_config['video'][1]}"
    video_source = f"assets/backgrounds/video/{video_choice}"
    if Path(video_source).is_file():
        print_step("Finding a spot in the backgrounds video to chop...✂️")
        try:
            with VideoFileClip(video_source) as video:
                write_looped_video_file(
                    video_source,
                    f"assets/temp/{thread_id}/background.mp4",
                    video_length,
                    video.duration,
                )

        except (OSError, IOError, subprocess.CalledProcessError):  # ffmpeg issue see #348
            print_substep("FFMPEG issue. Generating fallback background...")
            create_fallback_background_video(f"assets/temp/{thread_id}/background.mp4", video_length)
        print_substep("Background video chopped successfully!", style="bold green")
    else:
        create_fallback_background_video(f"assets/temp/{thread_id}/background.mp4", video_length)
    return background_config["video"][2]


# Create a tuple for downloads background (background_audio_options, background_video_options)
background_options = load_background_options()
