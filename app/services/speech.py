"""Speech-to-text service interface + ElevenLabs implementation + mock."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel

from app.config import get_settings, is_placeholder

logger = logging.getLogger(__name__)


class TranscriptResult(BaseModel):
    text: str
    language: str | None = None
    duration_seconds: float | None = None
    raw: dict | None = None


class SpeechError(Exception):
    """Typed error for speech service failures."""

    pass


class SpeechToText(ABC):
    @abstractmethod
    def transcribe(
        self, audio_bytes: bytes, filename: str, language: str | None = None
    ) -> TranscriptResult: ...


def speaker_labelled_text(result: dict) -> str:
    """Build 'Speaker 1: ...' lines from a diarized ElevenLabs response, else use plain text."""
    words = [w for w in result.get("words") or [] if w.get("type", "word") == "word"]
    if not words or not any(w.get("speaker_id") for w in words):
        return (result.get("text") or "").strip()

    labels: dict[str, str] = {}
    lines: list[str] = []
    current: str | None = None
    buffer: list[str] = []
    for word in words:
        speaker = word.get("speaker_id") or "speaker_0"
        labels.setdefault(speaker, f"Speaker {len(labels) + 1}")
        if speaker != current and buffer:
            lines.append(f"{labels[current]}: {' '.join(buffer)}")
            buffer = []
        current = speaker
        buffer.append(word.get("text", ""))
    if buffer and current is not None:
        lines.append(f"{labels[current]}: {' '.join(buffer)}")
    return "\n".join(lines)


class ElevenLabsSTT(SpeechToText):
    """ElevenLabs Scribe speech-to-text.

    # VERIFY AGAINST DOCS: POST https://api.elevenlabs.io/v1/speech-to-text, header xi-api-key,
    # multipart fields file / model_id / language_code / diarize, response text / language_code /
    # words[].speaker_id. Checked against elevenlabs.io/docs on 2026-10-02; not exercised live
    # because no real API key was available.
    """

    def __init__(self):
        self.settings = get_settings()
        self.api_key = self.settings.ELEVENLABS_API_KEY
        self.model = self.settings.STT_MODEL
        self.base_url = "https://api.elevenlabs.io/v1"

    def transcribe(
        self, audio_bytes: bytes, filename: str, language: str | None = None
    ) -> TranscriptResult:
        if is_placeholder(self.api_key):
            raise SpeechError(
                "ElevenLabs API key is missing or a placeholder. Set STT_PROVIDER=mock for demo mode."
            )

        lang = language or self.settings.STT_LANGUAGE
        if lang == "auto":
            lang = None

        data = {"model_id": self.model, "diarize": "true"}
        if lang:
            data["language_code"] = lang
        try:
            with httpx.Client(timeout=120.0) as client:
                response = client.post(
                    f"{self.base_url}/speech-to-text",
                    headers={"xi-api-key": self.api_key},
                    files={"file": (filename, audio_bytes)},
                    data=data,
                )
        except httpx.TimeoutException as exc:
            raise SpeechError("Speech-to-text service timed out. Please try again.") from exc
        except httpx.HTTPError as exc:
            raise SpeechError("Speech-to-text service could not be reached.") from exc

        if response.status_code != 200:
            logger.error("ElevenLabs STT error: %s %s", response.status_code, response.text[:300])
            raise SpeechError(f"Speech-to-text service error (status {response.status_code})")

        try:
            result = response.json()
        except ValueError as exc:
            raise SpeechError("Speech-to-text service returned an unreadable response") from exc
        text = speaker_labelled_text(result)
        if not text:
            raise SpeechError("Speech-to-text returned an empty transcript")

        ends = [
            w.get("end")
            for w in result.get("words") or []
            if isinstance(w.get("end"), (int, float))
        ]
        return TranscriptResult(
            text=text,
            language=result.get("language_code") or lang,
            duration_seconds=max(ends) if ends else None,
            raw=None,
        )


class MockSTT(SpeechToText):
    """Returns a deterministic synthetic transcript for tests/dev."""

    MOCK_TRANSCRIPT = """Doctor: Good morning. How are you feeling today?

Patient: Good morning, Doctor. I've been having persistent headaches for the past week, mainly in the frontal region. They get worse in the evening.

Doctor: I see. On a scale of 1 to 10, how would you rate the pain?

Patient: Around 6 to 7, especially when I've been working on the computer for long hours.

Doctor: Have you noticed any other symptoms? Nausea, visual disturbances, neck stiffness?

Patient: Some mild nausea occasionally, but no visual problems. My neck does feel tense.

Doctor: Are you taking any medications currently?

Patient: Just occasional paracetamol, maybe twice a week. I'm not on any regular medication. No known allergies.

Doctor: Let me examine you. Blood pressure is 130 over 85, slightly elevated. Fundoscopy is normal. Neck muscles show some tension.

Patient: Is everything okay, Doctor?

Doctor: Based on my examination, this appears to be tension-type headache, likely related to prolonged screen time and possibly stress. I'm going to prescribe you Ibuprofen 400mg, take one tablet twice daily after meals for 5 days. Also, I'll prescribe a muscle relaxant - Cyclobenzaprine 5mg at bedtime for 7 days to help with the neck tension.

Patient: Should I be worried about the blood pressure?

Doctor: It's mildly elevated. I'd recommend reducing your salt intake and we'll monitor it. I'd also like you to get a complete blood count and thyroid function test done. Try to take regular breaks from screen work, at least every 30 minutes. Practice some neck stretches. Let's follow up in two weeks to review your test results and see how you're responding to treatment.

Patient: Thank you, Doctor."""

    def transcribe(
        self, audio_bytes: bytes, filename: str, language: str | None = None
    ) -> TranscriptResult:
        return TranscriptResult(
            text=self.MOCK_TRANSCRIPT,
            language=language or "en",
            duration_seconds=180.0,
            raw={"provider": "mock"},
        )


def get_stt_service() -> SpeechToText:
    """Factory for speech-to-text service based on config."""
    settings = get_settings()
    if settings.STT_PROVIDER == "elevenlabs":
        return ElevenLabsSTT()
    return MockSTT()
