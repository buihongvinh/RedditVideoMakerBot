import asyncio
import random

import edge_tts

from utils import settings


DEFAULT_VOICE = "en-US-AriaNeural"
VOICES = [
    "en-US-AriaNeural",
    "en-US-JennyNeural",
    "en-US-GuyNeural",
    "en-US-DavisNeural",
    "en-GB-SoniaNeural",
    "en-GB-RyanNeural",
    "en-AU-NatashaNeural",
    "en-AU-WilliamNeural",
]


class EdgeTTS:
    def __init__(self):
        self.max_chars = 3000
        self.voices = VOICES

    def run(self, text, filepath, random_voice: bool = False):
        voice = self.randomvoice() if random_voice else self.configured_voice()
        rate = settings.config["settings"]["tts"].get("edge_tts_rate", "+0%")
        volume = settings.config["settings"]["tts"].get("edge_tts_volume", "+0%")
        pitch = settings.config["settings"]["tts"].get("edge_tts_pitch", "+0Hz")
        asyncio.run(self.save(text=text, filepath=filepath, voice=voice, rate=rate, volume=volume, pitch=pitch))

    async def save(self, text: str, filepath: str, voice: str, rate: str, volume: str, pitch: str):
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            volume=volume,
            pitch=pitch,
        )
        await communicate.save(filepath)

    def configured_voice(self) -> str:
        return settings.config["settings"]["tts"].get("edge_tts_voice") or DEFAULT_VOICE

    def randomvoice(self):
        return random.choice(self.voices)
