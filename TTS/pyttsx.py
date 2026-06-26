import random
import subprocess
import sys
import tempfile
from pathlib import Path

import pyttsx3

from utils import settings


class pyttsx:
    def __init__(self):
        self.max_chars = 5000
        self.voices = []

    def run(
        self,
        text: str,
        filepath: str,
        random_voice=False,
    ):
        voice_id = settings.config["settings"]["tts"]["python_voice"]
        voice_num = settings.config["settings"]["tts"]["py_voice_num"]
        if voice_id == "" or voice_num == "":
            voice_id = 2
            voice_num = 3
            raise ValueError("set pyttsx values to a valid value, switching to defaults")
        else:
            voice_id = int(voice_id)
            voice_num = int(voice_num)
        engine = pyttsx3.init()
        voices = engine.getProperty("voices")
        english_voice_indexes = self.get_english_voice_indexes(voices)
        self.voices = english_voice_indexes or list(range(min(voice_num, len(voices))))
        if random_voice:
            voice_id = self.randomvoice()
        else:
            voice_id = self.resolve_voice_id(voice_id, english_voice_indexes, len(voices))
        if sys.platform == "darwin":
            self.run_macos_say(text=text, filepath=filepath, voice_id=voice_id)
            return
        engine.setProperty(
            "voice", voices[voice_id].id
        )  # changing index changes voices but ony 0 and 1 are working here
        engine.save_to_file(text, f"{filepath}")
        engine.runAndWait()

    def randomvoice(self):
        return random.choice(self.voices)

    def resolve_voice_id(self, configured_voice_id: int, english_voice_indexes, voice_count: int) -> int:
        if english_voice_indexes:
            if configured_voice_id < len(english_voice_indexes):
                return english_voice_indexes[configured_voice_id]
            return english_voice_indexes[0]
        return min(configured_voice_id, max(voice_count - 1, 0))

    def get_english_voice_indexes(self, voices):
        english_indexes = []
        for idx, voice in enumerate(voices):
            voice_name = getattr(voice, "name", "").lower()
            voice_id = getattr(voice, "id", "").lower()
            voice_languages = "".join(
                language.decode("utf-8", errors="ignore").lower()
                if isinstance(language, bytes)
                else str(language).lower()
                for language in getattr(voice, "languages", [])
            )
            if any(
                marker in f"{voice_name} {voice_id} {voice_languages}"
                for marker in ("en_", "en-", "enus", "enus", "english", "en_us", "en-gb")
            ):
                english_indexes.append(idx)
        return english_indexes

    def run_macos_say(self, text: str, filepath: str, voice_id: int):
        engine = pyttsx3.init()
        voices = engine.getProperty("voices")
        if not voices:
            raise RuntimeError("No system voices available for macOS 'say' fallback")
        selected_voice = voices[min(voice_id, len(voices) - 1)].name

        output_path = Path(filepath)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as temp_audio:
            temp_path = temp_audio.name

        try:
            subprocess.run(
                ["/usr/bin/say", "-v", selected_voice, "-o", temp_path, text],
                check=True,
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    temp_path,
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(output_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            Path(temp_path).unlink(missing_ok=True)
