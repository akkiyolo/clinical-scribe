"""Diarised "Speaker N" labels become Doctor / Patient after transcription (best effort)."""

from __future__ import annotations

import pytest

from app.services.llm import LLMClient, LLMError
from app.services.scribe import assign_speaker_roles
from app.services.speech import SpeechToText, TranscriptResult
from tests.conftest import WEBM_BYTES

DIARISED = (
    "Speaker 1: Good morning. What brings you in today?\n"
    "Speaker 2: I have had a sore throat for three days.\n"
    "Speaker 1: I am prescribing azithromycin 500 mg once daily for three days.\n"
    "Speaker 2: Thank you, doctor."
)
LABELLED = (
    "Doctor: Good morning. What brings you in today?\n"
    "Patient: I have had a sore throat for three days.\n"
    "Doctor: I am prescribing azithromycin 500 mg once daily for three days.\n"
    "Patient: Thank you, doctor."
)


class RolesLLM(LLMClient):
    """Answers the speaker-role question with a fixed mapping (or an error)."""

    def __init__(self, roles=None, error: Exception | None = None):
        self.roles = roles or []
        self.error = error
        self.calls = 0

    def generate_structured(self, system, user, schema, max_retries=2):
        self.calls += 1
        if self.error:
            raise self.error
        return schema.model_validate({"roles": self.roles})

    def generate_text(self, system, user):
        return ""


def roles(**mapping):
    return [{"speaker": f"Speaker {n[1:]}", "role": role} for n, role in mapping.items()]


class TestAssignSpeakerRoles:
    def test_labels_doctor_and_patient(self):
        llm = RolesLLM(roles(s1="doctor", s2="patient"))
        assert assign_speaker_roles(DIARISED, llm) == LABELLED

    def test_order_of_speakers_does_not_matter(self):
        llm = RolesLLM(roles(s1="patient", s2="doctor"))
        out = assign_speaker_roles(DIARISED, llm)
        assert out.splitlines()[0].startswith("Patient: Good morning")
        assert out.splitlines()[1].startswith("Doctor: I have had")

    def test_other_speakers_keep_their_label(self):
        text = DIARISED + "\nSpeaker 3: I am her husband, she also has a fever."
        llm = RolesLLM(roles(s1="doctor", s2="patient", s3="other"))
        out = assign_speaker_roles(text, llm)
        assert out.endswith("Speaker 3: I am her husband, she also has a fever.")
        assert out.startswith("Doctor: Good morning")

    def test_one_person_split_into_two_labels_gets_the_same_role(self):
        text = DIARISED + "\nSpeaker 3: Also take paracetamol for the fever."
        llm = RolesLLM(roles(s1="doctor", s2="patient", s3="doctor"))
        assert assign_speaker_roles(text, llm).endswith(
            "Doctor: Also take paracetamol for the fever."
        )

    @pytest.mark.parametrize(
        "llm",
        [
            RolesLLM(roles(s1="doctor", s2="doctor")),  # no patient
            RolesLLM(roles(s1="patient", s2="other")),  # no doctor
            RolesLLM([]),  # no answer
            RolesLLM(roles(s7="doctor", s8="patient")),  # speakers not in the transcript
            RolesLLM(error=LLMError("LLM service timed out")),
            RolesLLM(error=RuntimeError("unexpected")),
        ],
    )
    def test_unsure_or_failed_answers_leave_the_transcript_unchanged(self, llm):
        assert assign_speaker_roles(DIARISED, llm) == DIARISED

    @pytest.mark.parametrize(
        "text", ["Doctor: Hello\nPatient: Hi", "plain text without labels", "Speaker 1: only one"]
    )
    def test_no_llm_call_without_two_diarised_speakers(self, text):
        llm = RolesLLM(roles(s1="doctor", s2="patient"))
        assert assign_speaker_roles(text, llm) == text and llm.calls == 0

    def test_only_line_leading_labels_are_replaced(self):
        text = "Speaker 1: Did Speaker 2: say that?\nSpeaker 2: Yes."
        llm = RolesLLM(roles(s1="doctor", s2="patient"))
        assert assign_speaker_roles(text, llm) == "Doctor: Did Speaker 2: say that?\nPatient: Yes."


class DiarisedSTT(SpeechToText):
    def transcribe(self, audio_bytes, filename, language=None):
        return TranscriptResult(text=DIARISED, language="en")


class TestTranscriptionJob:
    def upload(self, consulting):
        return consulting.doctor.upload(
            f"/api/consults/{consulting.consult_id}/audio", "rec.webm", WEBM_BYTES, "audio/webm"
        )

    def test_uploaded_audio_comes_back_with_doctor_and_patient_labels(
        self, consulting, monkeypatch
    ):
        monkeypatch.setattr("app.routers.consults.get_stt_service", DiarisedSTT)
        llm = RolesLLM(roles(s1="doctor", s2="patient"))
        monkeypatch.setattr("app.services.scribe.get_llm_client", lambda: llm)
        assert self.upload(consulting).status_code == 200
        body = consulting.doctor.get(f"/api/consults/{consulting.consult_id}").json()
        assert body["transcription_status"] == "ready" and body["transcript_text"] == LABELLED
        assert body["transcript_edited"] is False

    def test_role_labelling_failure_still_delivers_the_transcript(self, consulting, monkeypatch):
        monkeypatch.setattr("app.routers.consults.get_stt_service", DiarisedSTT)
        llm = RolesLLM(error=LLMError("LLM service could not be reached."))
        monkeypatch.setattr("app.services.scribe.get_llm_client", lambda: llm)
        assert self.upload(consulting).status_code == 200
        body = consulting.doctor.get(f"/api/consults/{consulting.consult_id}").json()
        assert body["transcription_status"] == "ready" and body["transcript_text"] == DIARISED
