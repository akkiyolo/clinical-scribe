"""Speech-to-text consult flow, the ElevenLabs STT adapter, TTS read-aloud and the voice agent."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.main import content_security_policy
from app.models.consult import Consult
from app.models.file import File
from app.models.report import AuditLog
from app.routers.voice import spoken_summary
from app.services.speech import ElevenLabsSTT, MockSTT, SpeechError, speaker_labelled_text
from app.services.storage import get_storage_service
from app.services.voice_agent import ElevenLabsVoiceAgent, NoopVoiceAgent, VoiceError
from tests.conftest import WEBM_BYTES, Api, new_patient, run_to_prescription


def upload_audio(consulting, data=WEBM_BYTES):
    return consulting.doctor.upload(
        f"/api/consults/{consulting.consult_id}/audio", "recording.webm", data, "audio/webm"
    )


class TestAudioToTranscript:
    def test_uploaded_audio_is_transcribed_and_becomes_ready(self, consulting):
        assert upload_audio(consulting).status_code == 200
        body = consulting.doctor.get(f"/api/consults/{consulting.consult_id}").json()
        assert (
            body["transcription_status"] == "ready"
            and body["transcript_text"] == MockSTT.MOCK_TRANSCRIPT
        )
        assert body["transcription_provider"] == "mock" and body["audio_file_id"]
        assert body["transcript_edited"] is False

    def test_editing_the_transcript_sets_the_edited_flag(self, consulting):
        upload_audio(consulting)
        doc, cid = consulting.doctor, consulting.consult_id
        assert (
            doc.put(
                f"/api/consults/{cid}/transcript", {"transcript_text": "Corrected transcript"}
            ).status_code
            == 200
        )
        body = doc.get(f"/api/consults/{cid}").json()
        assert (
            body["transcript_text"] == "Corrected transcript" and body["transcript_edited"] is True
        )

    def test_pasting_a_transcript_skips_audio_entirely(self, consulting):
        doc, cid = consulting.doctor, consulting.consult_id
        assert (
            doc.put(
                f"/api/consults/{cid}/transcript", {"transcript_text": "Pasted text"}
            ).status_code
            == 200
        )
        body = doc.get(f"/api/consults/{cid}").json()
        assert body["transcription_status"] == "ready" and body["audio_file_id"] is None
        assert (
            doc.put(f"/api/consults/{cid}/transcript", {"transcript_text": ""}).status_code == 422
        )

    def test_failure_sets_failed_with_a_message_and_retry_works(self, consulting, monkeypatch):
        class Broken:
            def transcribe(self, audio, filename, language=None):
                raise SpeechError("Speech-to-text service error (status 503)")

        monkeypatch.setattr("app.routers.consults.get_stt_service", lambda: Broken())
        assert upload_audio(consulting).status_code == 200
        doc, cid = consulting.doctor, consulting.consult_id
        failed = doc.get(f"/api/consults/{cid}").json()
        assert (
            failed["transcription_status"] == "failed" and "status 503" in failed["error_message"]
        )
        assert failed["transcript_text"] is None

        monkeypatch.setattr("app.routers.consults.get_stt_service", lambda: MockSTT())
        assert doc.post(f"/api/consults/{cid}/retry").json()["detail"] == "Retrying transcription"
        recovered = doc.get(f"/api/consults/{cid}").json()
        assert recovered["transcription_status"] == "ready" and recovered["error_message"] is None

    def test_unexpected_errors_never_leak_internals(self, consulting, monkeypatch):
        class Crashing:
            def transcribe(self, audio, filename, language=None):
                raise RuntimeError("secret internal detail: /srv/keys/elevenlabs.txt")

        monkeypatch.setattr("app.routers.consults.get_stt_service", lambda: Crashing())
        upload_audio(consulting)
        body = consulting.doctor.get(f"/api/consults/{consulting.consult_id}").json()
        assert body["transcription_status"] == "failed"
        assert (
            "secret internal" not in str(body)
            and body["error_message"] == "Transcription failed. Please try again."
        )

    def test_audio_is_deleted_after_transcription_when_configured_and_audited(
        self, consulting, monkeypatch, db
    ):
        monkeypatch.setattr(get_settings(), "DELETE_AUDIO_AFTER_TRANSCRIPTION", True)
        upload_audio(consulting)
        doc, cid = consulting.doctor, consulting.consult_id
        db.expire_all()
        consult = db.get(Consult, uuid.UUID(cid))
        assert consult.transcription_status.value == "ready" and consult.audio_file_id is None
        assert (
            db.scalars(
                select(File).where(
                    File.owner_id == uuid.UUID(doc.id), File.category == "consult_audio"
                )
            ).first()
            is None
        )
        row = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "consult.audio.delete", AuditLog.resource_id == cid
            )
        ).first()
        assert row is not None

    def test_audio_is_kept_by_default(self, consulting, db):
        upload_audio(consulting)
        record = db.get(
            File,
            uuid.UUID(
                consulting.doctor.get(f"/api/consults/{consulting.consult_id}").json()[
                    "audio_file_id"
                ]
            ),
        )
        assert get_storage_service().exists(record.s3_key)

    def test_audio_cannot_be_uploaded_after_the_soap_is_approved_or_while_transcribing(
        self, consulting, db
    ):
        run_to_prescription(consulting)
        assert upload_audio(consulting).status_code == 409
        other = consulting
        consult = db.get(Consult, uuid.UUID(other.consult_id))
        consult.status = consult.status.__class__.draft
        consult.transcription_status = consult.transcription_status.__class__.transcribing
        db.commit()
        assert upload_audio(consulting).status_code == 409
        assert (
            consulting.doctor.put(
                f"/api/consults/{consulting.consult_id}/transcript", {"transcript_text": "x"}
            ).status_code
            == 409
        )
        assert (
            consulting.doctor.post(
                f"/api/consults/{consulting.consult_id}/soap/generate"
            ).status_code
            == 409
        )

    def test_retry_with_nothing_failed_is_refused(self, consulting):
        assert (
            consulting.doctor.post(f"/api/consults/{consulting.consult_id}/retry").status_code
            == 400
        )


class FakeHttp:
    """Stand-in for httpx.Client capturing the request and returning a canned response."""

    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _go(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.error:
            raise self.error
        return self.response

    def post(self, url, **kw):
        return self._go("POST", url, **kw)

    def get(self, url, **kw):
        return self._go("GET", url, **kw)


def response(status=200, json=None, content=b"", text=""):
    return httpx.Response(
        status,
        json=json,
        content=content or text.encode() or None,
        request=httpx.Request("POST", "https://x"),
    )


class TestElevenLabsStt:
    def configure(self, monkeypatch, key="sk_real_looking_key_123"):
        monkeypatch.setattr(get_settings(), "ELEVENLABS_API_KEY", key)

    def test_success_uses_the_documented_request_and_labels_speakers(self, monkeypatch):
        self.configure(monkeypatch)
        words = [
            {"text": "Hello", "type": "word", "speaker_id": "speaker_0", "end": 0.5},
            {"text": "there", "type": "word", "speaker_id": "speaker_0", "end": 1.0},
            {"text": "Hi", "type": "word", "speaker_id": "speaker_1", "end": 2.0},
        ]
        fake = FakeHttp(
            response(json={"text": "Hello there Hi", "language_code": "en", "words": words})
        )
        monkeypatch.setattr("app.services.speech.httpx.Client", fake)
        result = ElevenLabsSTT().transcribe(b"audio", "rec.webm")
        method, url, kw = fake.calls[0]
        assert url == "https://api.elevenlabs.io/v1/speech-to-text" and kw["headers"] == {
            "xi-api-key": "sk_real_looking_key_123"
        }
        assert (
            kw["data"]["model_id"] == get_settings().STT_MODEL
            and kw["data"]["language_code"] == "en"
        )
        assert kw["data"]["diarize"] == "true" and kw["files"]["file"][0] == "rec.webm"
        assert (
            result.text == "Speaker 1: Hello there\nSpeaker 2: Hi"
            and result.duration_seconds == 2.0
        )
        assert result.language == "en"

    def test_auto_language_omits_language_code(self, monkeypatch):
        self.configure(monkeypatch)
        fake = FakeHttp(response(json={"text": "namaste"}))
        monkeypatch.setattr("app.services.speech.httpx.Client", fake)
        ElevenLabsSTT().transcribe(b"a", "r.webm", language="auto")
        assert "language_code" not in fake.calls[0][2]["data"]

    def test_plain_text_is_used_when_the_response_has_no_speakers(self):
        assert speaker_labelled_text({"text": " just text ", "words": []}) == "just text"

    @pytest.mark.parametrize(
        "fake,expect",
        [
            (
                FakeHttp(response(status=401, text="invalid key sk_real_looking_key_123")),
                "status 401",
            ),
            (FakeHttp(response(status=500, text="boom")), "status 500"),
            (FakeHttp(error=httpx.ReadTimeout("slow")), "timed out"),
            (FakeHttp(error=httpx.ConnectError("no route")), "could not be reached"),
            (FakeHttp(response(json={"text": ""})), "empty transcript"),
        ],
    )
    def test_provider_errors_become_typed_speech_errors_without_leaking(
        self, monkeypatch, fake, expect
    ):
        self.configure(monkeypatch)
        monkeypatch.setattr("app.services.speech.httpx.Client", fake)
        with pytest.raises(SpeechError, match=expect) as caught:
            ElevenLabsSTT().transcribe(b"a", "r.webm")
        assert "sk_real_looking_key_123" not in str(caught.value) and "boom" not in str(
            caught.value
        )

    def test_placeholder_key_is_refused_before_any_request(self, monkeypatch):
        self.configure(monkeypatch, key="your-elevenlabs-api-key")
        fake = FakeHttp(response(json={"text": "x"}))
        monkeypatch.setattr("app.services.speech.httpx.Client", fake)
        with pytest.raises(SpeechError, match="missing or a placeholder"):
            ElevenLabsSTT().transcribe(b"a", "r.webm")
        assert fake.calls == []


class MockVoice:
    def __init__(self):
        self.synth_calls = 0

    def synthesize(self, text, voice_id=None):
        self.synth_calls += 1
        self.last_text = text
        return b"ID3" + b"\x00" * 30 + text.encode()[:20]

    def start_session(self, context):
        self.context = context
        return {
            "signed_url": "wss://api.elevenlabs.io/v1/convai/conversation?signature=abc",
            "dynamic_variables": context,
        }


@pytest.fixture
def voice_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "VOICE_AGENT_PROVIDER", "elevenlabs")
    voice = MockVoice()
    monkeypatch.setattr("app.routers.voice.get_voice_agent", lambda: voice)
    return voice


def approve(consulting) -> dict:
    draft = run_to_prescription(consulting)
    return consulting.doctor.post(
        f"/api/prescriptions/{draft['id']}/approve",
        {
            "acknowledged_flag_ids": [
                f["id"] for f in draft["safety_flags"] if f["severity"] == "high"
            ]
        },
    ).json()


class TestVoice:
    def test_provider_none_disables_everything_cleanly(self, consulting):
        approved = approve(consulting)
        assert get_settings().VOICE_AGENT_PROVIDER == "none"
        assert (
            consulting.patient.post(f"/api/prescriptions/{approved['id']}/speak").status_code == 404
        )
        assert (
            consulting.patient.post(
                "/api/voice/session", {"prescription_id": approved["id"]}
            ).status_code
            == 404
        )
        assert 'name="voice-enabled" content="false"' in Api().get("/").text
        with pytest.raises(Exception, match="disabled"):
            NoopVoiceAgent().synthesize("x")

    def test_speak_only_works_for_approved_prescriptions_and_caches_the_audio(
        self, consulting, voice_on, db
    ):
        draft = run_to_prescription(consulting)
        patient, doc = consulting.patient, consulting.doctor
        assert (
            patient.post(f"/api/prescriptions/{draft['id']}/speak").status_code == 404
        )  # still a draft
        assert doc.post(f"/api/prescriptions/{draft['id']}/speak").status_code == 404
        approved = doc.post(
            f"/api/prescriptions/{draft['id']}/approve",
            {
                "acknowledged_flag_ids": [
                    f["id"] for f in draft["safety_flags"] if f["severity"] == "high"
                ]
            },
        ).json()

        first = patient.post(f"/api/prescriptions/{approved['id']}/speak")
        second = patient.post(f"/api/prescriptions/{approved['id']}/speak")
        assert (
            first.status_code == second.status_code == 200
            and first.headers["content-type"] == "audio/mpeg"
        )
        assert (
            first.content == second.content and voice_on.synth_calls == 1
        )  # generated once, then cached
        cached = db.scalars(
            select(File).where(File.s3_key.like(f"patient/{patient.id}/prescription-audio/%"))
        ).one()
        assert (
            cached.s3_key.endswith(f"{approved['id']}-v1.mp3")
            and cached.category.value == "prescription_audio"
        )
        assert (
            doc.post(f"/api/prescriptions/{approved['id']}/speak").status_code == 200
        )  # owning doctor too
        actions = [
            a
            for (a,) in db.execute(
                select(AuditLog.action).where(AuditLog.resource_id == approved["id"])
            )
        ]
        assert actions.count("voice.speak") == 3

    def test_summary_covers_drug_dose_frequency_duration_advice_and_follow_up(
        self, consulting, voice_on
    ):
        approved = approve(consulting)
        consulting.patient.post(f"/api/prescriptions/{approved['id']}/speak")
        text = voice_on.last_text
        for expected in (
            "Ibuprofen",
            "400mg",
            "twice daily after meals",
            "for 5 days",
            "Advice",
            "Follow up",
            "emergency services",
        ):
            assert expected in text, expected
        assert "source_quote" not in text and "flag" not in text.lower()

    def test_other_users_cannot_have_a_prescription_read_aloud(self, admin, consulting, voice_on):
        approved = approve(consulting)
        from tests.conftest import verified_doctor

        for other in (new_patient(), admin, verified_doctor(admin)):
            assert other.post(f"/api/prescriptions/{approved['id']}/speak").status_code == 404
        assert voice_on.synth_calls == 0

    def test_a_new_version_gets_its_own_audio(self, consulting, voice_on):
        v1 = approve(consulting)
        consulting.patient.post(f"/api/prescriptions/{v1['id']}/speak")
        content = v1["content"]
        content["advice"] = ["Brand new advice"]
        v2 = consulting.doctor.put(f"/api/prescriptions/{v1['id']}", {"content": content}).json()
        consulting.doctor.post(
            f"/api/prescriptions/{v2['id']}/approve",
            {
                "acknowledged_flag_ids": [
                    f["id"] for f in v2["safety_flags"] if f["severity"] == "high"
                ]
            },
        )
        consulting.patient.post(f"/api/prescriptions/{v2['id']}/speak")
        assert voice_on.synth_calls == 2 and "Brand new advice" in voice_on.last_text

    def test_tts_failure_gives_a_clean_502(self, consulting, monkeypatch):
        approved = approve(consulting)
        monkeypatch.setattr(get_settings(), "VOICE_AGENT_PROVIDER", "elevenlabs")

        class Failing:
            def synthesize(self, text, voice_id=None):
                raise VoiceError("TTS service error (status 500)")

        monkeypatch.setattr("app.routers.voice.get_voice_agent", lambda: Failing())
        response = consulting.patient.post(f"/api/prescriptions/{approved['id']}/speak")
        assert response.status_code == 502 and "status 500" not in response.text

    def test_agent_session_is_scoped_to_one_approved_prescription_of_the_patient(
        self, consulting, voice_on, db
    ):
        draft = run_to_prescription(consulting)
        patient = consulting.patient
        assert (
            patient.post("/api/voice/session", {"prescription_id": draft["id"]}).status_code == 404
        )  # draft
        approved = consulting.doctor.post(
            f"/api/prescriptions/{draft['id']}/approve",
            {
                "acknowledged_flag_ids": [
                    f["id"] for f in draft["safety_flags"] if f["severity"] == "high"
                ]
            },
        ).json()
        session = patient.post("/api/voice/session", {"prescription_id": approved["id"]}).json()
        assert session["signed_url"].startswith("wss://")
        context = voice_on.context
        assert (
            "Ibuprofen" in context["prescription_summary"]
            and "other patient" in context["guardrails"]
        )
        assert (
            "emergency services" in context["guardrails"]
            and "new medical advice" in context["guardrails"]
        )
        assert (
            get_settings().ELEVENLABS_API_KEY not in str(session)
            or not get_settings().ELEVENLABS_API_KEY
        )
        assert (
            new_patient()
            .post("/api/voice/session", {"prescription_id": approved["id"]})
            .status_code
            == 404
        )
        assert (
            consulting.doctor.post(
                "/api/voice/session", {"prescription_id": approved["id"]}
            ).status_code
            == 403
        )
        assert patient.post("/api/voice/session", {}).status_code == 422
        assert db.scalars(select(AuditLog).where(AuditLog.action == "voice.session.start")).first()

    def test_csp_allows_the_agent_websocket_only_when_voice_is_enabled(self):
        assert "elevenlabs" not in content_security_policy(False)
        assert "wss://api.elevenlabs.io" in content_security_policy(True)


class TestElevenLabsVoiceAgent:
    def agent(self, monkeypatch, key="sk_real_looking_key_123"):
        settings = get_settings()
        monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", key)
        monkeypatch.setattr(settings, "ELEVENLABS_VOICE_ID", "voice_abc123")
        monkeypatch.setattr(settings, "ELEVENLABS_AGENT_ID", "agent_abc123")
        return ElevenLabsVoiceAgent()

    def test_synthesize_uses_the_documented_request(self, monkeypatch):
        agent = self.agent(monkeypatch)
        fake = FakeHttp(response(content=b"MP3DATA"))
        monkeypatch.setattr("app.services.voice_agent.httpx.Client", fake)
        assert agent.synthesize("hello") == b"MP3DATA"
        _, url, kw = fake.calls[0]
        assert url.endswith("/text-to-speech/voice_abc123") and kw["params"] == {
            "output_format": "mp3_44100_128"
        }
        assert (
            kw["headers"]["xi-api-key"] == "sk_real_looking_key_123"
            and kw["json"]["text"] == "hello"
        )

    def test_synthesize_errors_are_typed_and_clean(self, monkeypatch):
        agent = self.agent(monkeypatch)
        for fake in (
            FakeHttp(response(status=500, text="x")),
            FakeHttp(error=httpx.ReadTimeout("t")),
            FakeHttp(error=httpx.ConnectError("c")),
        ):
            monkeypatch.setattr("app.services.voice_agent.httpx.Client", fake)
            with pytest.raises(VoiceError):
                agent.synthesize("hello")

    def test_session_uses_the_signed_url_endpoint_server_side(self, monkeypatch):
        agent = self.agent(monkeypatch)
        fake = FakeHttp(response(json={"signed_url": "wss://api.elevenlabs.io/x?sig=1"}))
        monkeypatch.setattr("app.services.voice_agent.httpx.Client", fake)
        session = agent.start_session({"prescription_summary": "s"})
        method, url, kw = fake.calls[0]
        assert method == "GET" and url.endswith("/convai/conversation/get-signed-url")
        assert kw["params"] == {"agent_id": "agent_abc123"} and session["dynamic_variables"] == {
            "prescription_summary": "s"
        }
        assert "sk_real_looking_key_123" not in str(
            session
        )  # the API key never reaches the browser

    def test_placeholder_credentials_are_refused(self, monkeypatch):
        agent = self.agent(monkeypatch, key="your-elevenlabs-api-key")
        with pytest.raises(VoiceError):
            agent.synthesize("x")
        with pytest.raises(VoiceError):
            agent.start_session({})


def test_spoken_summary_handles_sparse_prescriptions():
    text = spoken_summary({"medications": [{"drug_name": "Paracetamol"}]})
    assert "Paracetamol" in text and "emergency services" in text and "Follow up" not in text
