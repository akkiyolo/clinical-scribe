"""Voice agent / TTS service interface + ElevenLabs implementation + noop."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from app.config import get_settings, is_placeholder

logger = logging.getLogger(__name__)


class FeatureDisabled(Exception):
    """Raised when voice features are disabled."""

    pass


class VoiceError(Exception):
    """Typed error for voice service failures."""

    pass


class VoiceAgent(ABC):
    @abstractmethod
    def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        """Synthesize text to speech. Returns audio bytes (mp3)."""
        ...

    @abstractmethod
    def start_session(self, context: dict) -> dict:
        """Start a voice agent session. Returns client connection info."""
        ...


class ElevenLabsVoiceAgent(VoiceAgent):
    """ElevenLabs text-to-speech and Conversational AI agent sessions.

    # VERIFY AGAINST DOCS: POST /v1/text-to-speech/{voice_id}?output_format=mp3_44100_128 and
    # GET /v1/convai/conversation/get-signed-url?agent_id=... (header xi-api-key) were checked
    # against elevenlabs.io/docs on 2026-10-02 but not exercised without a real key. How the
    # browser passes dynamic variables when it opens the signed URL depends on the ElevenLabs
    # client SDK version and is left to the frontend integration.
    """

    def __init__(self):
        settings = get_settings()
        self.api_key = settings.ELEVENLABS_API_KEY
        self.voice_id = settings.ELEVENLABS_VOICE_ID
        self.agent_id = settings.ELEVENLABS_AGENT_ID
        self.base_url = "https://api.elevenlabs.io/v1"

    def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        """Convert text to speech (mp3)."""
        voice = voice_id or self.voice_id
        if is_placeholder(self.api_key) or is_placeholder(voice):
            raise VoiceError("ElevenLabs API key or voice id is not configured")
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.base_url}/text-to-speech/{voice}",
                    params={"output_format": "mp3_44100_128"},
                    headers={
                        "xi-api-key": self.api_key,
                        "Content-Type": "application/json",
                        "Accept": "audio/mpeg",
                    },
                    json={
                        "text": text,
                        "model_id": "eleven_multilingual_v2",
                        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                    },
                )
        except httpx.TimeoutException as exc:
            raise VoiceError("TTS service timed out") from exc
        except httpx.HTTPError as exc:
            raise VoiceError("TTS service could not be reached") from exc
        if response.status_code != 200 or not response.content:
            logger.error("ElevenLabs TTS error: %s", response.status_code)
            raise VoiceError(f"TTS service error (status {response.status_code})")
        return response.content

    def start_session(self, context: dict) -> dict:
        """Create a signed agent URL server-side so the API key never reaches the browser.

        `context` holds the one prescription this session may discuss; it is returned as
        dynamic variables for the agent prompt (the agent's own system prompt must carry the
        guardrails: explain only what is written, no new medical advice, urgent symptoms go to
        the doctor or emergency services, never discuss other patients).
        """
        if is_placeholder(self.api_key) or is_placeholder(self.agent_id):
            raise VoiceError("ElevenLabs agent is not configured")
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(
                    f"{self.base_url}/convai/conversation/get-signed-url",
                    params={"agent_id": self.agent_id},
                    headers={"xi-api-key": self.api_key},
                )
        except httpx.HTTPError as exc:
            raise VoiceError("Agent session could not be created") from exc
        if response.status_code != 200:
            logger.error("ElevenLabs agent session error: %s", response.status_code)
            raise VoiceError(f"Agent session error (status {response.status_code})")
        signed_url = response.json().get("signed_url")
        if not signed_url:
            raise VoiceError("Agent session returned no signed URL")
        return {"signed_url": signed_url, "dynamic_variables": context}


class NoopVoiceAgent(VoiceAgent):
    """No-op implementation when voice features are disabled."""

    def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        raise FeatureDisabled("Voice features are disabled")

    def start_session(self, context: dict) -> dict:
        raise FeatureDisabled("Voice features are disabled")


def get_voice_agent() -> VoiceAgent:
    """Factory for voice agent based on config."""
    settings = get_settings()
    if settings.VOICE_AGENT_PROVIDER == "elevenlabs":
        return ElevenLabsVoiceAgent()
    return NoopVoiceAgent()
