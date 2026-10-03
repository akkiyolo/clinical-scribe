"""LLM client interface + Google Gemini implementation + mock."""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import get_settings, is_placeholder

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Typed error for LLM service failures."""

    pass


class LLMClient(ABC):
    """Abstract LLM client with structured output support."""

    @abstractmethod
    def generate_structured(
        self,
        system: str,
        user: str,
        schema: Type[T],
        max_retries: int = 2,
    ) -> T:
        """Generate a response conforming to the given Pydantic schema."""
        ...

    @abstractmethod
    def generate_text(self, system: str, user: str) -> str:
        """Generate plain text response."""
        ...


class GeminiLLMClient(LLMClient):
    """Google Gemini REST implementation (generateContent).

    # VERIFY AGAINST DOCS: endpoint, x-goog-api-key header and generationConfig field names were
    # checked against ai.google.dev on 2026-10-02 but could not be exercised without a real key.
    The JSON schema is given to the model in the system prompt and the response is validated
    with Pydantic; Gemini's own responseSchema accepts only an OpenAPI subset (no $ref/$defs), so
    it is deliberately not sent.
    """

    TRANSIENT_STATUS = {429, 500, 502, 503, 504}
    # Gemini answers 503 "overloaded" in bursts; back off for ~14 s in total before giving up.
    BACKOFF_SECONDS = (2.0, 4.0, 8.0)
    ATTEMPTS = len(BACKOFF_SECONDS) + 1
    MAX_RETRY_AFTER = 20.0
    QUOTA_MESSAGE = (
        "The AI service's usage quota is used up (Gemini returned RESOURCE_EXHAUSTED). "
        "Try again later, or enable billing on the Google AI project."
    )

    def __init__(self):
        settings = get_settings()
        self.api_key = settings.LLM_API_KEY
        self.model = settings.LLM_MODEL
        self.base_url = settings.LLM_BASE_URL.rstrip("/")

    def _call_api(self, system: str, user: str, json_mode: bool = False) -> dict:
        """Call generateContent, retrying transient failures (timeouts, 429, 5xx) with backoff."""
        if is_placeholder(self.api_key):
            raise LLMError("LLM API key not configured")

        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        body: dict = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

        last_error = "LLM service unavailable"
        retry_after: float | None = None
        for attempt in range(self.ATTEMPTS):
            if attempt:
                default = self.BACKOFF_SECONDS[attempt - 1]
                time.sleep(default if retry_after is None else retry_after)
                retry_after = None
            try:
                with httpx.Client(timeout=60.0) as client:
                    response = client.post(url, json=body, headers=headers)
            except httpx.TimeoutException:
                last_error = "LLM service timed out. Please try again."
                continue
            except httpx.HTTPError:
                last_error = "LLM service could not be reached."
                continue

            if response.status_code == 429 and self._quota_exhausted(response):
                logger.error("Gemini quota exhausted: %s", response.text[:300])
                raise LLMError(self.QUOTA_MESSAGE)
            if response.status_code in self.TRANSIENT_STATUS:
                logger.warning("Gemini transient error: %s", response.status_code)
                last_error = f"LLM service error (status {response.status_code})"
                retry_after = self._retry_after(response)
                continue
            if response.status_code != 200:
                logger.error("Gemini API error: %s %s", response.status_code, response.text[:300])
                raise LLMError(f"LLM service error (status {response.status_code})")
            return response.json()
        raise LLMError(last_error)

    def _retry_after(self, response: httpx.Response) -> float | None:
        """Seconds from a numeric Retry-After header, capped; None when absent or unusable."""
        try:
            seconds = float(response.headers.get("retry-after", ""))
        except ValueError:
            return None
        return min(max(seconds, 0.0), self.MAX_RETRY_AFTER)

    def _quota_exhausted(self, response: httpx.Response) -> bool:
        """True for a 429 that retrying cannot fix: a per-day quota, or a retry delay of minutes+.

        Per-minute rate limits come back with a short retryDelay and are retried as usual.
        """
        try:
            body = response.json()
        except ValueError:
            return False
        error = body.get("error") if isinstance(body, dict) else None
        details = error.get("details") if isinstance(error, dict) else None
        for detail in details if isinstance(details, list) else []:
            if not isinstance(detail, dict):
                continue
            for violation in detail.get("violations") or []:
                if "PerDay" in str(violation.get("quotaId", "")):
                    return True
            delay = str(detail.get("retryDelay", ""))
            if delay.endswith("s"):
                try:
                    if float(delay[:-1]) > self.MAX_RETRY_AFTER * 3:
                        return True
                except ValueError:
                    pass
        return False

    def _extract_text(self, result: dict) -> str:
        """Extract text from Gemini API response."""
        try:
            candidates = result.get("candidates", [])
            if not candidates:
                raise LLMError("No response from LLM")
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                raise LLMError("Empty response from LLM")
            return parts[0].get("text", "")
        except (KeyError, IndexError) as e:
            raise LLMError(f"Failed to parse LLM response: {e}")

    def generate_structured(
        self,
        system: str,
        user: str,
        schema: Type[T],
        max_retries: int = 2,
    ) -> T:
        """Generate a response and validate against a Pydantic schema."""
        json_schema = schema.model_json_schema()

        # Add instruction to return JSON
        system_with_json = (
            f"{system}\n\nYou MUST respond with valid JSON matching this schema:\n"
            f"{json.dumps(json_schema, indent=2)}\n\nReturn ONLY the JSON, no other text."
        )

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                result = self._call_api(system_with_json, user, json_mode=True)
                text = self._extract_text(result)

                # Clean JSON markers
                text = text.strip()
                if text.startswith("```json"):
                    text = text[7:]
                if text.startswith("```"):
                    text = text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

                data = json.loads(text)
                return schema.model_validate(data)

            except (json.JSONDecodeError, ValidationError) as e:
                last_error = e
                logger.warning(
                    "LLM structured output attempt %d/%d failed: %s",
                    attempt + 1,
                    max_retries + 1,
                    str(e)[:200],
                )
                if attempt < max_retries:
                    user = f"{user}\n\n[Previous response was invalid: {str(e)[:200]}. Please fix and try again.]"
                continue
            except LLMError:
                raise

        raise LLMError(
            f"LLM failed to produce valid structured output after {max_retries + 1} attempts: {last_error}"
        )

    def generate_text(self, system: str, user: str) -> str:
        """Generate plain text response."""
        result = self._call_api(system, user)
        return self._extract_text(result)


class MockLLM(LLMClient):
    """Canned, transcript-independent output for tests and an explicit LLM_PROVIDER=mock demo.

    Never selected implicitly: with a missing key the app fails loudly instead. The prescription
    safety check still flags any canned medication that is not present in the real transcript.
    """

    def generate_structured(
        self,
        system: str,
        user: str,
        schema: Type[T],
        max_retries: int = 2,
    ) -> T:
        """Return a canned response based on schema type."""

        schema_name = schema.__name__

        if schema_name == "SOAPOutput":
            return schema.model_validate(
                {
                    "subjective": "Patient reports persistent frontal headaches for the past week, rating 6-7/10. Worse with prolonged screen time. Occasional mild nausea. Neck tension noted. Taking occasional paracetamol. No known allergies.",
                    "objective": "BP 130/85 mmHg (mildly elevated). Fundoscopy normal. Neck muscles tense on palpation.",
                    "assessment": "Tension-type headache, likely related to prolonged screen time and stress. Mildly elevated blood pressure requiring monitoring.",
                    "plan": "1. Ibuprofen 400mg twice daily after meals for 5 days\n2. Cyclobenzaprine 5mg at bedtime for 7 days\n3. Complete blood count and thyroid function tests\n4. Lifestyle modifications: regular screen breaks, neck stretches, reduce salt intake\n5. Follow-up in 2 weeks",
                    "icd10_codes": [
                        {
                            "code": "G44.2",
                            "description": "Tension-type headache",
                            "confidence": 0.9,
                        },
                        {"code": "M54.2", "description": "Cervicalgia", "confidence": 0.7},
                        {
                            "code": "R03.0",
                            "description": "Elevated blood pressure reading",
                            "confidence": 0.6,
                        },
                    ],
                }
            )

        if schema_name == "ClinicalEntities":
            return schema.model_validate(
                {
                    "diagnoses": ["Tension-type headache", "Cervicalgia"],
                    "symptoms": ["Frontal headaches", "Nausea", "Neck tension"],
                    "medications_mentioned": [
                        {
                            "name": "Ibuprofen",
                            "dose": "400mg",
                            "frequency": "twice daily after meals",
                            "duration": "5 days",
                            "quote": "Ibuprofen 400mg, take one tablet twice daily after meals for 5 days",
                        },
                        {
                            "name": "Cyclobenzaprine",
                            "dose": "5mg",
                            "frequency": "at bedtime",
                            "duration": "7 days",
                            "quote": "Cyclobenzaprine 5mg at bedtime for 7 days",
                        },
                    ],
                    "allergies_mentioned": [],
                    "follow_up_mentioned": "Follow-up in 2 weeks",
                    "tests_advised": ["Complete blood count", "Thyroid function test"],
                }
            )

        if schema_name == "PrescriptionContent":
            return schema.model_validate(
                {
                    "diagnosis": ["Tension-type headache", "Cervicalgia"],
                    "icd10": [
                        {"code": "G44.2", "description": "Tension-type headache"},
                        {"code": "M54.2", "description": "Cervicalgia"},
                    ],
                    "medications": [
                        {
                            "drug_name": "Ibuprofen",
                            "strength": "400mg",
                            "dose": "1 tablet",
                            "route": "oral",
                            "frequency": "twice daily after meals",
                            "duration": "5 days",
                            "instructions": "Take after meals",
                            "source_quote": "Ibuprofen 400mg, take one tablet twice daily after meals for 5 days",
                        },
                        {
                            "drug_name": "Cyclobenzaprine",
                            "strength": "5mg",
                            "dose": "1 tablet",
                            "route": "oral",
                            "frequency": "at bedtime",
                            "duration": "7 days",
                            "instructions": "Take at bedtime for neck tension",
                            "source_quote": "Cyclobenzaprine 5mg at bedtime for 7 days",
                        },
                    ],
                    "tests_advised": ["Complete blood count", "Thyroid function test"],
                    "advice": [
                        "Take regular breaks from screen work every 30 minutes",
                        "Practice neck stretches",
                        "Reduce salt intake",
                    ],
                    "follow_up": "2 weeks",
                    "notes": None,
                }
            )

        # Generic fallback: try to construct a minimal valid object
        return schema.model_validate({})

    def generate_text(self, system: str, user: str) -> str:
        return "This is a mock LLM response for testing purposes."


class UnconfiguredLLM(LLMClient):
    """Used when no LLM is configured: every call fails with a clear, retryable error."""

    MESSAGE = (
        "No LLM is configured. Set LLM_API_KEY and LLM_MODEL "
        "(or LLM_PROVIDER=mock for a clearly labelled demo)."
    )

    def generate_structured(
        self, system: str, user: str, schema: Type[T], max_retries: int = 2
    ) -> T:
        raise LLMError(self.MESSAGE)

    def generate_text(self, system: str, user: str) -> str:
        raise LLMError(self.MESSAGE)


def get_llm_client() -> LLMClient:
    """Factory for the LLM client selected by configuration."""
    settings = get_settings()
    if settings.LLM_PROVIDER == "mock":
        return MockLLM()
    if is_placeholder(settings.LLM_API_KEY):
        return UnconfiguredLLM()
    return GeminiLLMClient()
