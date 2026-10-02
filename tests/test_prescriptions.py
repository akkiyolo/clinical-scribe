"""SOAP generation, the prescription agent, safety flags, DOCX output, review and approval."""

from __future__ import annotations

import io
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from docx import Document
from pydantic import ValidationError
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models.consult import AgentRun, Consult, Prescription
from app.models.doctor import DoctorProfile
from app.models.enums import AgentRunStatus, ConsultStatus, DoctorStatus
from app.models.file import File
from app.models.report import AuditLog
from app.schemas.prescription import PrescriptionContent
from app.services.jobs import expire_stale_consult, sweep_stuck_jobs
from app.services.llm import GeminiLLMClient, LLMClient, LLMError, MockLLM
from app.services.prescription_agent import run_prescription_agent
from app.services.scribe import SOAPOutput
from app.services.speech import MockSTT
from app.services.storage import get_storage_service
from tests.conftest import (
    PNG_BYTES,
    Api,
    grant_consent,
    new_patient,
    run_to_prescription,
    verified_doctor,
)

FULL = MockSTT.MOCK_TRANSCRIPT
MONTH = "Doctor: Start Metformin 500 mg twice daily after meals for 30 days. Review in one month."


class ScriptedLLM(LLMClient):
    """Returns a chosen prescription draft; everything else comes from the canned MockLLM."""

    def __init__(
        self, prescription: dict | None = None, soap_error: Exception | None = None, on_call=None
    ):
        self.prescription = prescription
        self.soap_error = soap_error
        self.on_call = on_call
        self.mock = MockLLM()
        self.calls: list[str] = []

    def generate_structured(self, system, user, schema, max_retries=2):
        self.calls.append(schema.__name__)
        if self.on_call:
            self.on_call(schema.__name__)
        if schema.__name__ == "SOAPOutput" and self.soap_error:
            raise self.soap_error
        if schema.__name__ == "PrescriptionContent" and self.prescription is not None:
            return schema.model_validate(self.prescription)
        return self.mock.generate_structured(system, user, schema, max_retries)

    def generate_text(self, system, user):
        return "text"


@pytest.fixture
def use_llm(monkeypatch):
    def install(client: LLMClient) -> LLMClient:
        monkeypatch.setattr("app.services.scribe.get_llm_client", lambda: client)
        monkeypatch.setattr("app.services.prescription_agent.get_llm_client", lambda: client)
        return client

    return install


def med(name, **overrides):
    base = {
        "drug_name": name,
        "strength": None,
        "dose": None,
        "route": "oral",
        "frequency": None,
        "duration": None,
        "instructions": None,
        "source_quote": None,
    }
    base.update(overrides)
    return base


def draft_json(meds, **extra):
    body = {
        "diagnosis": ["Tension-type headache"],
        "icd10": [],
        "medications": meds,
        "tests_advised": [],
        "advice": ["Rest"],
        "follow_up": "2 weeks",
        "notes": None,
    }
    body.update(extra)
    return body


def with_unsourced_drug(consulting, use_llm):
    """A draft that also prescribes Warfarin, which neither the transcript nor the SOAP plan mention."""
    use_llm(
        ScriptedLLM(
            draft_json(
                [
                    med(
                        "Ibuprofen",
                        strength="400 mg",
                        dose="1 tablet",
                        frequency="twice daily after meals",
                        duration="5 days",
                    ),
                    med(
                        "Warfarin",
                        strength="5 mg",
                        dose="1 tablet",
                        frequency="daily",
                        duration="30 days",
                    ),
                ]
            )
        )
    )
    return run_to_prescription(consulting)


def open_docx(api, file_id: str) -> Document:
    response = api.get(f"/api/files/{file_id}")
    assert response.status_code == 200, response.text
    return Document(io.BytesIO(response.content))


def all_text(document: Document) -> str:
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def flag_ids(prescription: dict, severity="high") -> list[str]:
    return [f["id"] for f in prescription["safety_flags"] if f["severity"] == severity]


class TestSoap:
    def test_generation_from_a_transcript_produces_structured_soap(self, consulting):
        doc, cid = consulting.doctor, consulting.consult_id
        doc.put(f"/api/consults/{cid}/transcript", {"transcript_text": FULL})
        assert doc.post(f"/api/consults/{cid}/soap/generate").status_code == 200
        body = doc.get(f"/api/consults/{cid}").json()
        assert body["status"] == "soap_ready"
        soap = body["soap"]
        assert all(soap[k] for k in ("subjective", "objective", "assessment", "plan"))
        assert soap["status"] == "draft" and soap["icd10_codes"][0]["code"]

    def test_generation_needs_a_transcript(self, consulting):
        assert (
            consulting.doctor.post(
                f"/api/consults/{consulting.consult_id}/soap/generate"
            ).status_code
            == 400
        )

    def test_soap_can_be_edited_while_awaiting_review_and_stays_soap_ready(self, consulting):
        doc, cid = consulting.doctor, consulting.consult_id
        doc.put(f"/api/consults/{cid}/transcript", {"transcript_text": FULL})
        doc.post(f"/api/consults/{cid}/soap/generate")
        edit = doc.put(
            f"/api/consults/{cid}/soap",
            {
                "plan": "Edited plan",
                "icd10_codes": [
                    {"code": "G44.2", "description": "Tension-type headache", "confidence": 0.9}
                ],
            },
        )
        assert edit.status_code == 200
        body = doc.get(f"/api/consults/{cid}").json()
        assert body["status"] == "soap_ready" and body["soap"]["plan"] == "Edited plan"
        assert body["soap"]["icd10_codes"] == [
            {"code": "G44.2", "description": "Tension-type headache", "confidence": 0.9}
        ]
        assert (
            doc.put(f"/api/consults/{cid}/soap", {"icd10_codes": [{"code": ""}]}).status_code == 422
        )

    def test_llm_failure_marks_the_consult_failed_with_a_readable_message_and_retry_works(
        self, consulting, use_llm
    ):
        doc, cid = consulting.doctor, consulting.consult_id
        use_llm(ScriptedLLM(soap_error=LLMError("LLM service timed out. Please try again.")))
        doc.put(f"/api/consults/{cid}/transcript", {"transcript_text": FULL})
        doc.post(f"/api/consults/{cid}/soap/generate")
        failed = doc.get(f"/api/consults/{cid}").json()
        assert failed["status"] == "failed" and "timed out" in failed["error_message"]

        use_llm(ScriptedLLM())
        assert doc.post(f"/api/consults/{cid}/retry").json()["detail"] == "Retrying SOAP generation"
        assert doc.get(f"/api/consults/{cid}").json()["status"] == "soap_ready"

    def test_an_unconfigured_llm_fails_loudly_instead_of_inventing_a_note(
        self, consulting, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "LLM_PROVIDER", "gemini")
        monkeypatch.setattr(get_settings(), "LLM_API_KEY", "")
        doc, cid = consulting.doctor, consulting.consult_id
        doc.put(
            f"/api/consults/{cid}/transcript",
            {"transcript_text": "A real transcript about diabetes."},
        )
        doc.post(f"/api/consults/{cid}/soap/generate")
        body = doc.get(f"/api/consults/{cid}").json()
        assert body["status"] == "failed" and "No LLM is configured" in body["error_message"]
        assert body["llm_provider"] == "unconfigured" and "soap" not in body

    def test_gemini_client_retries_invalid_json_then_fails_cleanly(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "LLM_API_KEY", "real-looking-key-123456")
        client = GeminiLLMClient()
        responses = iter(["not json", '{"subjective": 1}', "```json\n{}\n```"])
        calls = []

        def fake_call(system, user, json_mode=False):
            calls.append(user)
            return {"candidates": [{"content": {"parts": [{"text": next(responses)}]}}]}

        monkeypatch.setattr(client, "_call_api", fake_call)
        with pytest.raises(LLMError, match="3 attempts"):
            client.generate_structured("sys", "user", SOAPOutput, max_retries=2)
        assert len(calls) == 3 and "Previous response was invalid" in calls[2]

    def test_gemini_client_accepts_fenced_json_on_a_retry(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "LLM_API_KEY", "real-looking-key-123456")
        client = GeminiLLMClient()
        good = '{"subjective":"s","objective":"o","assessment":"a","plan":"p","icd10_codes":[]}'
        texts = iter(["nope", f"```json\n{good}\n```"])
        monkeypatch.setattr(
            client,
            "_call_api",
            lambda *a, **k: {"candidates": [{"content": {"parts": [{"text": next(texts)}]}}]},
        )
        assert client.generate_structured("s", "u", SOAPOutput).plan == "p"

    def test_gemini_client_sends_the_key_in_a_header_never_the_url(self, monkeypatch):
        import httpx

        monkeypatch.setattr(get_settings(), "LLM_API_KEY", "secret-key-value-abc123")
        seen = {}

        class FakeClient:
            def __init__(self, *a, **k): ...
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None, headers=None):
                seen.update(url=url, headers=headers, body=json)
                return httpx.Response(
                    200,
                    json={"candidates": [{"content": {"parts": [{"text": "hi"}]}}]},
                    request=httpx.Request("POST", url),
                )

        monkeypatch.setattr("app.services.llm.httpx.Client", FakeClient)
        assert GeminiLLMClient().generate_text("sys", "user") == "hi"
        assert "secret-key-value-abc123" not in seen["url"]
        assert seen["headers"]["x-goog-api-key"] == "secret-key-value-abc123"
        assert "responseSchema" not in seen["body"]["generationConfig"]

    def test_transcript_cannot_change_after_the_soap_is_approved(self, consulting):
        run_to_prescription(consulting)
        response = consulting.doctor.put(
            f"/api/consults/{consulting.consult_id}/transcript", {"transcript_text": "changed"}
        )
        assert response.status_code == 409
        assert (
            consulting.doctor.put(
                f"/api/consults/{consulting.consult_id}/soap", {"plan": "x"}
            ).status_code
            == 409
        )
        assert (
            consulting.doctor.post(
                f"/api/consults/{consulting.consult_id}/soap/generate"
            ).status_code
            == 409
        )


class TestAgent:
    def test_approving_the_soap_triggers_exactly_one_run_and_is_idempotent(self, consulting, db):
        draft = run_to_prescription(consulting)
        cid = uuid.UUID(consulting.consult_id)
        for _ in range(3):
            assert consulting.doctor.post(f"/api/consults/{cid}/soap/approve").status_code == 200
        db.expire_all()
        runs = db.scalars(select(AgentRun).where(AgentRun.consult_id == cid)).all()
        drafts = db.scalars(select(Prescription).where(Prescription.consult_id == cid)).all()
        assert len(runs) == 1 and runs[0].status == AgentRunStatus.succeeded
        assert len(drafts) == 1 and str(drafts[0].id) == draft["id"]
        assert runs[0].graph_state["completed_nodes"] == [
            "gather_context",
            "extract_entities",
            "draft_prescription",
            "safety_check",
            "build_document",
            "store_and_notify",
        ]

    def test_run_requires_an_approved_soap(self, consulting, db):
        cid = uuid.UUID(consulting.consult_id)
        run = AgentRun(consult_id=cid, status=AgentRunStatus.queued)
        db.add(run)
        db.commit()
        result = run_prescription_agent(str(cid), str(run.id))
        assert "SOAP note must be approved" in result["errors"][0]
        db.expire_all()
        assert db.get(AgentRun, run.id).status == AgentRunStatus.failed
        assert (
            db.scalars(select(Prescription).where(Prescription.consult_id == cid)).first() is None
        )

    def test_draft_content_flags_and_consult_state(self, consulting):
        draft = run_to_prescription(consulting)
        assert draft["status"] == "draft" and draft["version"] == 1
        assert [m["drug_name"] for m in draft["content"]["medications"]] == [
            "Ibuprofen",
            "Cyclobenzaprine",
        ]
        assert draft["content"]["medications"][0]["source_quote"]
        assert (
            consulting.doctor.get(f"/api/consults/{consulting.consult_id}").json()["status"]
            == "prescription_ready"
        )

    def test_docx_has_doctor_details_first_and_draft_markings(self, consulting):
        draft = run_to_prescription(consulting)
        document = open_docx(consulting.doctor, draft["docx_file_id"])
        first_block = document.element.body[0]
        assert first_block.tag.endswith("}tbl")
        header_text = document.tables[0].rows[0].cells[0].text
        doctor_name = consulting.doctor.user["full_name"]
        assert (
            doctor_name in header_text
            and "Reg. No." in header_text
            and "Maharashtra Medical Council" in header_text
        )
        assert "Verified on ClinicalScribe" in header_text and "Test Clinic" in header_text
        text = all_text(document)
        assert (
            "Patient" in text
            and "Test Patient" in text
            and "Ibuprofen" in text
            and "Doctor's signature" in text
        )
        draft_line = "DRAFT: AI-generated, not valid until reviewed and approved by the doctor"
        assert draft_line in document.sections[0].header.paragraphs[0].text
        assert any(draft_line in p.text for p in document.sections[0].footer.paragraphs)
        assert len(document.tables) >= 3  # header, patient block, medications (+ icd10, flags)

    def test_docx_opens_in_libreoffice(self, consulting, tmp_path):
        import shutil
        import subprocess

        if not shutil.which("soffice"):
            pytest.skip("LibreOffice not installed")
        draft = run_to_prescription(consulting)
        path = tmp_path / "draft.docx"
        path.write_bytes(consulting.doctor.get(f"/api/files/{draft['docx_file_id']}").content)
        done = subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(path)],
            capture_output=True,
            timeout=180,
        )
        assert (tmp_path / "draft.pdf").exists() and (
            tmp_path / "draft.pdf"
        ).stat().st_size > 1000, done.stderr

    def test_missing_doctor_photo_never_fails_the_document(self, consulting):
        doc = consulting.doctor
        file_id = doc.upload("/api/me/photo", "p.png", PNG_BYTES, "image/png").json()["file_id"]
        with SessionLocal() as session:
            record = session.get(File, uuid.UUID(file_id))
            get_storage_service().delete(record.s3_key)  # photo row exists, object is gone
        draft = run_to_prescription(consulting)
        assert "Reg. No." in open_docx(doc, draft["docx_file_id"]).tables[0].rows[0].cells[0].text

    def test_doctor_photo_is_embedded_when_available(self, consulting):
        consulting.doctor.upload("/api/me/photo", "p.png", PNG_BYTES, "image/png")
        draft = run_to_prescription(consulting)
        document = open_docx(consulting.doctor, draft["docx_file_id"])
        assert document.tables[0].rows[0].cells[1]._tc.xpath(".//pic:pic")

    def test_suspension_mid_pipeline_aborts_at_the_next_node(self, consulting, admin, db, use_llm):
        def suspend_during_entity_extraction(schema_name):
            if schema_name == "ClinicalEntities":
                admin.post(
                    f"/api/admin/doctors/{consulting.doctor.id}/suspend",
                    {"reason": "Suspended mid pipeline"},
                )

        use_llm(ScriptedLLM(on_call=suspend_during_entity_extraction))
        doc, cid = consulting.doctor, consulting.consult_id
        doc.put(f"/api/consults/{cid}/transcript", {"transcript_text": FULL})
        doc.post(f"/api/consults/{cid}/soap/generate")
        admin_view_before = admin.get(f"/api/admin/doctors/{doc.id}").json()["status"]
        assert admin_view_before == "verified"
        doc.post(f"/api/consults/{cid}/soap/approve")

        db.expire_all()
        consult = db.get(Consult, uuid.UUID(cid))
        run = db.scalars(select(AgentRun).where(AgentRun.consult_id == consult.id)).one()
        assert (
            consult.status == ConsultStatus.failed and "no longer verified" in consult.error_message
        )
        assert run.status == AgentRunStatus.failed
        assert run.graph_state["completed_nodes"] == [
            "gather_context",
            "extract_entities",
        ]  # stopped before drafting
        assert (
            db.scalars(select(Prescription).where(Prescription.consult_id == consult.id)).first()
            is None
        )

    def test_revoked_consent_aborts_before_storing_a_draft(self, consulting, db, use_llm):
        def revoke_consent(schema_name):
            if schema_name == "PrescriptionContent":
                consulting.patient.delete(f"/api/consents/{consulting.consent_id}")

        use_llm(ScriptedLLM(on_call=revoke_consent))
        doc, cid = consulting.doctor, consulting.consult_id
        doc.put(f"/api/consults/{cid}/transcript", {"transcript_text": FULL})
        doc.post(f"/api/consults/{cid}/soap/generate")
        doc.post(f"/api/consults/{cid}/soap/approve")
        db.expire_all()
        consult = db.get(Consult, uuid.UUID(cid))
        assert consult.status == ConsultStatus.failed and "revoked consent" in consult.error_message
        assert (
            db.scalars(select(Prescription).where(Prescription.consult_id == consult.id)).first()
            is None
        )

    def test_retry_after_failure_creates_no_duplicates(self, consulting, db, use_llm):
        doc, cid = consulting.doctor, consulting.consult_id
        use_llm(ScriptedLLM(soap_error=None))

        class Flaky(ScriptedLLM):
            failed = False

            def generate_structured(self, system, user, schema, max_retries=2):
                if schema.__name__ == "PrescriptionContent" and not Flaky.failed:
                    Flaky.failed = True
                    raise LLMError("LLM service error (status 503)")
                return super().generate_structured(system, user, schema, max_retries)

        use_llm(Flaky())
        doc.put(f"/api/consults/{cid}/transcript", {"transcript_text": FULL})
        doc.post(f"/api/consults/{cid}/soap/generate")
        doc.post(f"/api/consults/{cid}/soap/approve")
        assert doc.get(f"/api/consults/{cid}").json()["status"] == "failed"
        assert (
            doc.post(f"/api/consults/{cid}/retry").json()["detail"]
            == "Retrying prescription drafting"
        )
        assert doc.post(f"/api/consults/{cid}/retry").status_code == 400  # nothing left to retry
        body = doc.get(f"/api/consults/{cid}").json()
        assert body["status"] == "prescription_ready"
        db.expire_all()
        assert db.query(Prescription).filter_by(consult_id=uuid.UUID(cid)).count() == 1
        runs = (
            db.query(AgentRun)
            .filter_by(consult_id=uuid.UUID(cid))
            .order_by(AgentRun.created_at)
            .all()
        )
        assert [r.status for r in runs] == [AgentRunStatus.failed, AgentRunStatus.succeeded]

    def test_rerunning_the_agent_reuses_the_existing_draft(self, consulting, db):
        draft = run_to_prescription(consulting)
        cid = uuid.UUID(consulting.consult_id)
        second = AgentRun(consult_id=cid, status=AgentRunStatus.queued)
        db.add(second)
        db.commit()
        result = run_prescription_agent(str(cid), str(second.id))
        assert result["prescription_id"] == draft["id"]
        db.expire_all()
        assert db.query(Prescription).filter_by(consult_id=cid).count() == 1

    def test_sweeper_fails_stale_runs_and_lets_the_doctor_retry(self, consulting, db):
        cid = uuid.UUID(consulting.consult_id)
        consult = db.get(Consult, cid)
        consult.status = ConsultStatus.prescription_generating
        run = AgentRun(
            consult_id=cid,
            status=AgentRunStatus.running,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        )
        db.add(run)
        db.commit()
        swept = sweep_stuck_jobs()
        assert swept >= 1
        db.expire_all()
        assert db.get(AgentRun, run.id).status == AgentRunStatus.failed
        assert "restarted" in db.get(AgentRun, run.id).error
        consult = db.get(Consult, cid)
        assert consult.status == ConsultStatus.failed and "Retry" in consult.error_message

    def test_a_fresh_running_job_is_not_swept(self, consulting, db):
        cid = uuid.UUID(consulting.consult_id)
        db.get(Consult, cid).status = ConsultStatus.prescription_generating
        run = AgentRun(
            consult_id=cid, status=AgentRunStatus.running, started_at=datetime.now(timezone.utc)
        )
        db.add(run)
        db.commit()
        sweep_stuck_jobs()
        db.expire_all()
        assert db.get(AgentRun, run.id).status == AgentRunStatus.running

    def test_reading_a_stale_consult_expires_it_without_a_restart(self, consulting, db):
        cid = uuid.UUID(consulting.consult_id)
        consult = db.get(Consult, cid)
        consult.status = ConsultStatus.soap_generating
        consult.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.commit()
        body = consulting.doctor.get(f"/api/consults/{cid}").json()
        assert body["status"] == "failed" and "timed out" in body["error_message"]
        db.expire_all()
        assert expire_stale_consult(db, db.get(Consult, cid)) is False


class TestSafetyThroughTheApi:
    def test_medication_missing_from_transcript_and_plan_is_a_high_flag_and_nothing_is_removed(
        self, consulting, use_llm
    ):
        draft = with_unsourced_drug(consulting, use_llm)
        high = [f for f in draft["safety_flags"] if f["type"] == "medication_not_in_source"]
        assert [f["field_ref"] for f in high] == ["medications[1].drug_name"] and high[0][
            "severity"
        ] == "high"
        assert "Warfarin" in high[0]["message"]
        assert [m["drug_name"] for m in draft["content"]["medications"]] == [
            "Ibuprofen",
            "Warfarin",
        ]  # never removed

    def test_a_drug_in_the_approved_soap_plan_counts_as_sourced(self, consulting):
        # The transcript only mentions Ibuprofen, but the doctor-approved plan lists Cyclobenzaprine too.
        draft = run_to_prescription(
            consulting,
            "Doctor: Take Ibuprofen 400mg, one tablet twice daily after meals for 5 days.",
        )
        assert not [f for f in draft["safety_flags"] if f["type"] == "medication_not_in_source"]

    def test_allergy_conflict_is_a_high_flag(self, admin, use_llm):
        doctor, patient = verified_doctor(admin), new_patient(allergies="Penicillin, Sulfa drugs")
        grant_consent(patient, doctor)
        cid = doctor.post("/api/consults", {"patient_id": patient.id}).json()["id"]
        use_llm(
            ScriptedLLM(
                draft_json(
                    [
                        med(
                            "Amoxicillin",
                            strength="500 mg",
                            dose="1 capsule",
                            frequency="three times a day",
                            duration="7 days",
                        )
                    ]
                )
            )
        )
        setup = type("S", (), {"doctor": doctor, "consult_id": cid})
        draft = run_to_prescription(
            setup,
            "Doctor: Amoxicillin 500 mg, 1 capsule three times a day for 7 days. Review in one week.",
        )
        types = {(f["type"], f["severity"]) for f in draft["safety_flags"]}
        assert ("allergy_conflict", "high") in types

    def test_missing_dose_frequency_duration_are_medium_flags(self, admin, use_llm):
        doctor, patient = verified_doctor(admin), new_patient()
        grant_consent(patient, doctor)
        cid = doctor.post("/api/consults", {"patient_id": patient.id}).json()["id"]
        use_llm(ScriptedLLM(draft_json([med("Metformin")])))
        setup = type("S", (), {"doctor": doctor, "consult_id": cid})
        draft = run_to_prescription(setup, MONTH)
        missing = {f["field_ref"] for f in draft["safety_flags"] if f["type"] == "missing_field"}
        assert {
            "medications[0].dose",
            "medications[0].frequency",
            "medications[0].duration",
        } <= missing
        assert all(
            f["severity"] == "medium" for f in draft["safety_flags"] if f["type"] == "missing_field"
        )

    def test_flags_appear_in_the_draft_document(self, consulting, use_llm):
        draft = with_unsourced_drug(consulting, use_llm)
        text = all_text(open_docx(consulting.doctor, draft["docx_file_id"]))
        assert "AI safety flags" in text and "[HIGH]" in text and "Warfarin" in text


class TestReviewAndApproval:
    def test_approval_requires_every_high_flag_to_be_acknowledged(self, consulting, use_llm):
        draft = with_unsourced_drug(consulting, use_llm)
        doc, pid = consulting.doctor, draft["id"]
        high = flag_ids(draft)
        assert high
        refused = doc.post(f"/api/prescriptions/{pid}/approve", {"acknowledged_flag_ids": []})
        assert refused.status_code == 400 and "acknowledge" in refused.json()["detail"]
        assert (
            doc.post(
                f"/api/prescriptions/{pid}/approve", {"acknowledged_flag_ids": ["flag-bogus"]}
            ).status_code
            == 400
        )
        approved = doc.post(f"/api/prescriptions/{pid}/approve", {"acknowledged_flag_ids": high})
        assert approved.status_code == 200 and approved.json()["status"] == "approved"
        stored_high = [f for f in approved.json()["safety_flags"] if f["severity"] == "high"]
        assert all(f["acknowledged"] for f in stored_high)  # acknowledgments stay in the database

    def test_approval_issues_code_new_docx_without_draft_banner_and_keeps_the_draft_file(
        self, consulting
    ):
        draft = run_to_prescription(consulting)
        doc, pid = consulting.doctor, draft["id"]
        approved = doc.post(
            f"/api/prescriptions/{pid}/approve", {"acknowledged_flag_ids": flag_ids(draft)}
        ).json()
        assert re.fullmatch(r"RX-\d{4}-\d{6}-v1", approved["approval_code"])
        assert approved["docx_file_id"] != draft["docx_file_id"]  # a new object
        assert (
            doc.get(f"/api/files/{draft['docx_file_id']}").status_code == 200
        )  # the draft file is kept

        final = open_docx(doc, approved["docx_file_id"])
        text = all_text(final)
        assert (
            approved["approval_code"] in text and "Digitally approved on" in text and "IST" in text
        )
        assert "DRAFT" not in text and "DRAFT" not in final.sections[0].header.paragraphs[0].text
        assert "AI safety flags" not in text
        footer = " ".join(p.text for p in final.sections[0].footer.paragraphs)
        assert f"Approved by {consulting.doctor.user['full_name']}" in footer and "Page" in footer
        assert doc.get(f"/api/consults/{consulting.consult_id}").json()["status"] == "completed"
        again = doc.post(f"/api/prescriptions/{pid}/approve", {"acknowledged_flag_ids": []})
        assert (
            again.status_code == 200 and again.json()["approval_code"] == approved["approval_code"]
        )

    def test_patient_sees_nothing_until_approval_then_only_the_safe_view(self, consulting):
        draft = run_to_prescription(consulting)
        patient, doc, pid = consulting.patient, consulting.doctor, draft["id"]
        assert patient.get("/api/prescriptions").json()["items"] == []
        assert patient.get(f"/api/prescriptions/{pid}").status_code == 404
        assert patient.get(f"/api/files/{draft['docx_file_id']}").status_code == 403
        assert (
            patient.get(f"/api/consults/{consulting.consult_id}/prescriptions").json()["items"]
            == []
        )

        doc.post(f"/api/prescriptions/{pid}/approve", {"acknowledged_flag_ids": flag_ids(draft)})
        listed = patient.get("/api/prescriptions").json()["items"]
        assert len(listed) == 1 and listed[0]["status"] == "approved"
        view = patient.get(f"/api/prescriptions/{pid}").json()
        blob = str(view)
        assert (
            "safety_flags" not in view and "source_quote" not in blob and "agent_run_id" not in blob
        )
        assert view["medications"][0]["drug_name"] == "Ibuprofen" and view["approval_code"]

        download = patient.get(f"/api/files/{view['docx_file_id']}")
        assert download.status_code == 200
        assert re.search(
            r"Prescription-Test-Patient-\d{4}-\d{2}-\d{2}\.docx",
            download.headers["content-disposition"],
        )
        assert (
            patient.get(f"/api/files/{draft['docx_file_id']}").status_code == 403
        )  # the draft file stays private
        assert new_patient().get(f"/api/prescriptions/{pid}").status_code == 404

    def test_editing_creates_a_new_version_supersedes_the_draft_and_rechecks_safety(
        self, consulting
    ):
        draft = run_to_prescription(consulting)
        doc = consulting.doctor
        content = draft["content"]
        content["medications"].append(
            med("Warfarin", dose="5 mg", frequency="daily", duration="30 days")
        )
        edited = doc.put(
            f"/api/prescriptions/{draft['id']}", {"content": content, "expected_version": 1}
        )
        assert edited.status_code == 200
        v2 = edited.json()
        assert (
            v2["version"] == 2
            and v2["status"] == "draft"
            and v2["docx_file_id"] != draft["docx_file_id"]
        )
        assert any(
            f["type"] == "medication_not_in_source" and "Warfarin" in f["message"]
            for f in v2["safety_flags"]
        )
        assert doc.get(f"/api/prescriptions/{draft['id']}").json()["status"] == "superseded"
        assert "Warfarin" in all_text(open_docx(doc, v2["docx_file_id"]))
        history = doc.get(f"/api/consults/{consulting.consult_id}/prescriptions").json()["items"]
        assert [(h["version"], h["status"]) for h in history] == [(2, "draft"), (1, "superseded")]

    def test_concurrent_edit_returns_409(self, consulting):
        draft = run_to_prescription(consulting)
        doc = consulting.doctor
        assert (
            doc.put(
                f"/api/prescriptions/{draft['id']}",
                {"content": draft["content"], "expected_version": 1},
            ).status_code
            == 200
        )
        stale = doc.put(
            f"/api/prescriptions/{draft['id']}",
            {"content": draft["content"], "expected_version": 1},
        )
        assert stale.status_code == 409 and "reload" in stale.json()["detail"]
        new_id = doc.get(f"/api/consults/{consulting.consult_id}").json()["latest_prescription"][
            "id"
        ]
        wrong_version = doc.put(
            f"/api/prescriptions/{new_id}", {"content": draft["content"], "expected_version": 1}
        )
        assert wrong_version.status_code == 409
        assert (
            doc.post(
                f"/api/prescriptions/{draft['id']}/approve", {"acknowledged_flag_ids": []}
            ).status_code
            == 409
        )

    def test_approved_prescriptions_are_immutable_and_edits_create_a_reviewed_new_version(
        self, consulting
    ):
        draft = run_to_prescription(consulting)
        doc, patient = consulting.doctor, consulting.patient
        v1 = doc.post(
            f"/api/prescriptions/{draft['id']}/approve", {"acknowledged_flag_ids": flag_ids(draft)}
        ).json()
        content = v1["content"]
        content["advice"] = ["Drink plenty of water"]

        v2 = doc.put(f"/api/prescriptions/{v1['id']}", {"content": content}).json()
        assert v2["version"] == 2 and v2["status"] == "draft"
        still_v1 = doc.get(f"/api/prescriptions/{v1['id']}").json()
        assert still_v1["status"] == "approved" and still_v1["content"]["advice"] != [
            "Drink plenty of water"
        ]
        listed = patient.get("/api/prescriptions").json()["items"]
        assert [p["version"] for p in listed] == [
            1
        ]  # the patient keeps the approved v1 until v2 is approved
        assert patient.get(f"/api/prescriptions/{v2['id']}").status_code == 404

        v2_approved = doc.post(
            f"/api/prescriptions/{v2['id']}/approve", {"acknowledged_flag_ids": flag_ids(v2)}
        ).json()
        assert v2_approved["approval_code"].endswith("-v2")
        assert v2_approved["approval_code"].split("-")[2] == v1["approval_code"].split("-")[2]
        assert doc.get(f"/api/prescriptions/{v1['id']}").json()["status"] == "superseded"
        assert [p["version"] for p in patient.get("/api/prescriptions").json()["items"]] == [2]
        history = patient.get(f"/api/consults/{consulting.consult_id}/prescriptions").json()[
            "items"
        ]
        assert [(h["version"], h["status"]) for h in history] == [
            (2, "approved"),
            (1, "superseded"),
        ]

    def test_reject_then_regenerate_with_a_doctor_note(self, consulting, db):
        draft = run_to_prescription(consulting)
        doc = consulting.doctor
        response = doc.post(
            f"/api/prescriptions/{draft['id']}/reject",
            {"regenerate": True, "note": "Use paracetamol only"},
        )
        assert response.status_code == 200 and response.json()["regenerating"] is True
        assert doc.get(f"/api/prescriptions/{draft['id']}").json()["status"] == "rejected"
        consult = doc.get(f"/api/consults/{consulting.consult_id}").json()
        assert (
            consult["status"] == "prescription_ready"
            and consult["latest_prescription"]["version"] == 2
        )
        db.expire_all()
        runs = (
            db.query(AgentRun)
            .filter_by(consult_id=uuid.UUID(consulting.consult_id))
            .order_by(AgentRun.created_at)
            .all()
        )
        assert runs[-1].graph_state["doctor_note"] == "Use paracetamol only"
        assert doc.post(f"/api/prescriptions/{draft['id']}/reject").status_code == 409

    def test_reject_without_regenerating_then_regenerate_from_the_consult(self, consulting):
        draft = run_to_prescription(consulting)
        doc, cid = consulting.doctor, consulting.consult_id
        assert doc.post(f"/api/prescriptions/{draft['id']}/reject").json()["regenerating"] is False
        assert doc.get(f"/api/consults/{cid}").json()["status"] == "soap_approved"
        assert (
            doc.post(
                f"/api/consults/{cid}/prescriptions/regenerate", {"note": "Try again"}
            ).status_code
            == 200
        )
        body = doc.get(f"/api/consults/{cid}").json()
        assert (
            body["status"] == "prescription_ready" and body["latest_prescription"]["version"] == 2
        )
        assert (
            doc.post(f"/api/consults/{cid}/prescriptions/regenerate").status_code == 409
        )  # a draft is open

    def test_suspended_doctor_cannot_approve_but_a_reinstated_one_can(self, admin, consulting):
        draft = run_to_prescription(consulting)
        doc, pid = consulting.doctor, draft["id"]
        admin.post(f"/api/admin/doctors/{doc.id}/suspend", {"reason": "Investigation in progress"})
        assert (
            doc.post(
                f"/api/prescriptions/{pid}/approve", {"acknowledged_flag_ids": flag_ids(draft)}
            ).status_code
            == 403
        )
        assert (
            doc.put(f"/api/prescriptions/{pid}", {"content": draft["content"]}).status_code == 403
        )
        admin.post(f"/api/admin/doctors/{doc.id}/reinstate", {"reason": "Investigation concluded"})
        assert (
            doc.post(
                f"/api/prescriptions/{pid}/approve", {"acknowledged_flag_ids": flag_ids(draft)}
            ).status_code
            == 200
        )

    def test_nothing_reaches_the_patient_without_an_explicit_doctor_approval(self, consulting, db):
        run_to_prescription(consulting)
        rows = db.query(Prescription).filter_by(consult_id=uuid.UUID(consulting.consult_id)).all()
        assert [r.status.value for r in rows] == ["draft"] and all(
            r.approved_by is None for r in rows
        )
        assert consulting.patient.get("/api/prescriptions").json()["total"] == 0

    def test_edit_payload_is_validated_and_bounded(self, consulting):
        draft = run_to_prescription(consulting)
        doc, pid = consulting.doctor, draft["id"]
        assert (
            doc.put(
                f"/api/prescriptions/{pid}", {"content": {"medications": [{"strength": "x"}]}}
            ).status_code
            == 422
        )
        assert (
            doc.put(f"/api/prescriptions/{pid}", {"content": {"advice": ["x"] * 31}}).status_code
            == 422
        )
        assert (
            doc.put(f"/api/prescriptions/{pid}", {"content": {"follow_up": "x" * 501}}).status_code
            == 422
        )

    def test_audit_rows_exist_for_the_clinical_actions(self, consulting, db):
        draft = run_to_prescription(consulting)
        doc, patient = consulting.doctor, consulting.patient
        approved = doc.post(
            f"/api/prescriptions/{draft['id']}/approve", {"acknowledged_flag_ids": flag_ids(draft)}
        ).json()
        patient.get(f"/api/files/{approved['docx_file_id']}")
        doc.put(
            f"/api/consults/{consulting.consult_id}/transcript", {"transcript_text": "x"}
        )  # refused, not audited
        actions = {
            a
            for (a,) in db.execute(
                select(AuditLog.action).where(
                    AuditLog.resource_id.in_(
                        [draft["id"], consulting.consult_id, approved["docx_file_id"]]
                    )
                )
            )
        }
        assert {
            "consult.create",
            "consult.transcript.update",
            "consult.soap.generate",
            "soap.approve",
            "prescription.approve",
            "prescription.download",
            "file.access",
        } <= actions

    def test_doctor_profile_changes_do_not_alter_an_approved_document(self, consulting, db):
        draft = run_to_prescription(consulting)
        doc = consulting.doctor
        approved = doc.post(
            f"/api/prescriptions/{draft['id']}/approve", {"acknowledged_flag_ids": flag_ids(draft)}
        ).json()
        before = doc.get(f"/api/files/{approved['docx_file_id']}").content
        profile = db.get(DoctorProfile, uuid.UUID(doc.id))
        assert profile.status == DoctorStatus.verified
        assert doc.get(f"/api/files/{approved['docx_file_id']}").content == before


def test_prescription_content_schema_validates():
    with pytest.raises(ValidationError):
        PrescriptionContent.model_validate({"medications": [{"drug_name": "x" * 201}]})
    assert PrescriptionContent.model_validate({}).medications == []


@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="real row-level locking needs PostgreSQL (run with TEST_DATABASE_URL)",
)
class TestConcurrentClicks:
    """Simultaneous duplicate requests (double clicks, two tabs) must not duplicate work."""

    def same_doctor_sessions(self, consulting, count=2):
        sessions = []
        for _ in range(count):
            session = Api()
            session.login(consulting.doctor.email)
            sessions.append(session)
        return sessions

    def test_simultaneous_soap_approvals_start_exactly_one_agent_run(self, consulting, db):
        doc, cid = consulting.doctor, consulting.consult_id
        doc.put(f"/api/consults/{cid}/transcript", {"transcript_text": FULL})
        doc.post(f"/api/consults/{cid}/soap/generate")
        sessions = self.same_doctor_sessions(consulting, 4)
        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = list(
                pool.map(
                    lambda s: s.post(f"/api/consults/{cid}/soap/approve").status_code, sessions
                )
            )
        assert codes == [200] * 4
        db.expire_all()
        assert db.query(AgentRun).filter_by(consult_id=uuid.UUID(cid)).count() == 1
        assert db.query(Prescription).filter_by(consult_id=uuid.UUID(cid)).count() == 1

    def test_simultaneous_prescription_approvals_issue_one_code(self, consulting, db):
        draft = run_to_prescription(consulting)
        ack = [f["id"] for f in draft["safety_flags"] if f["severity"] == "high"]
        sessions = self.same_doctor_sessions(consulting, 4)
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(
                pool.map(
                    lambda s: s.post(
                        f"/api/prescriptions/{draft['id']}/approve", {"acknowledged_flag_ids": ack}
                    ),
                    sessions,
                )
            )
        assert [r.status_code for r in responses] == [200] * 4
        assert len({r.json()["approval_code"] for r in responses}) == 1
        db.expire_all()
        row = db.get(Prescription, uuid.UUID(draft["id"]))
        assert row.status.value == "approved" and row.approval_code

    def test_simultaneous_edits_of_one_draft_create_exactly_one_new_version(self, consulting, db):
        draft = run_to_prescription(consulting)
        sessions = self.same_doctor_sessions(consulting, 4)
        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = sorted(
                pool.map(
                    lambda s: s.put(
                        f"/api/prescriptions/{draft['id']}",
                        {"content": draft["content"], "expected_version": 1},
                    ).status_code,
                    sessions,
                )
            )
        assert codes == [200, 409, 409, 409]
        db.expire_all()
        versions = (
            db.query(Prescription).filter_by(consult_id=uuid.UUID(consulting.consult_id)).all()
        )
        assert sorted(v.version for v in versions) == [1, 2]

    def test_simultaneous_consent_grants_leave_one_active_consent(self, doctor, db):
        patient = new_patient()
        sessions = []
        for _ in range(4):
            session = Api()
            session.login(patient.email)
            sessions.append(session)
        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = sorted(
                pool.map(
                    lambda s: s.post("/api/consents", {"doctor_id": doctor.id}).status_code,
                    sessions,
                )
            )
        assert codes == [201, 409, 409, 409]
