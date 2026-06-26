from typing import Tuple

from rich.console import Console

from TTS.aws_polly import AWSPolly
from TTS.edge_tts import EdgeTTS
from TTS.elevenlabs import elevenlabs
from TTS.engine_wrapper import TTSEngine
from TTS.GTTS import GTTS
from TTS.openai_tts import OpenAITTS
from TTS.pyttsx import pyttsx
from TTS.streamlabs_polly import StreamlabsPolly
from TTS.TikTok import TikTok
from utils import settings
from utils.console import print_step, print_table

console = Console()

TTSProviders = {
    "Edge": EdgeTTS,
    "GoogleTranslate": GTTS,
    "AWSPolly": AWSPolly,
    "StreamlabsPolly": StreamlabsPolly,
    "TikTok": TikTok,
    "pyttsx": pyttsx,
    "ElevenLabs": elevenlabs,
    "OpenAI": OpenAITTS,
}


def resolve_tts_provider_name(configured_voice: str) -> str:
    voice = str(configured_voice or "").strip()
    if voice.casefold() != "tiktok":
        return voice

    session_id = settings.config["settings"]["tts"].get("tiktok_sessionid", "")
    if session_id:
        return voice

    fallback_voice = "edge"
    print_step("TikTok sessionid is missing. Falling back to Edge TTS.")
    settings.config["settings"]["tts"]["voice_choice"] = fallback_voice
    return fallback_voice


def save_text_to_mp3(reddit_obj) -> Tuple[int, int]:
    """Saves text to MP3 files.

    Args:
        reddit_obj (): Reddit object received from reddit API in reddit/subreddit.py

    Returns:
        tuple[int,int]: (total length of the audio, the number of comments audio was generated for)
    """

    voice = resolve_tts_provider_name(settings.config["settings"]["tts"]["voice_choice"])
    if str(voice).casefold() in map(lambda _: _.casefold(), TTSProviders):
        text_to_mp3 = TTSEngine(get_case_insensitive_key_value(TTSProviders, voice), reddit_obj)
    else:
        while True:
            print_step("Please choose one of the following TTS providers: ")
            print_table(TTSProviders)
            choice = input("\n")
            if choice.casefold() in map(lambda _: _.casefold(), TTSProviders):
                break
            print("Unknown Choice")
        text_to_mp3 = TTSEngine(get_case_insensitive_key_value(TTSProviders, choice), reddit_obj)
    return text_to_mp3.run()


def get_case_insensitive_key_value(input_dict, key):
    return next(
        (value for dict_key, value in input_dict.items() if dict_key.lower() == key.lower()),
        None,
    )
