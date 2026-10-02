# Architecture Decisions

Short log: date, decision, reason. Newest decisions are at the bottom; entries marked *(revised)* replace an earlier one.

| Date | Decision | Reason |
|------|----------|--------|
| 2026-10-01 | PyJWT (HS256) for session tokens | Simple, maintained, no extra crypto dependency |
| 2026-10-01 | argon2-cffi for password hashing | Spec preference; memory-hard |
| 2026-10-01 | httpx for ElevenLabs and the LLM instead of vendor SDKs | Direct control of timeouts and errors, no SDK version churn |
| 2026-10-01 | `DATABASE_URL` is normalised to `postgresql+psycopg://` in `config.py` | psycopg 3 needs the `+psycopg` dialect |
| 2026-10-01 | No `ClinicalScribe_Project_Report.docx` was present | Built from the build specification alone; there was no conflict to record |
| 2026-10-01 | `HPRRegistryChecker` is a stub that raises `NotImplementedError` | Spec: the real ABDM HPR integration is production work; no scraping of any council site |
| 2026-10-01 | Docx timestamps use IST (UTC+5:30) and say so | Spec requirement for the prescription document |
| 2026-10-02 | JSON columns use `JSON().with_variant(JSONB, "postgresql")` | Keeps JSONB on Render Postgres while local SQLite runs and tests keep working |
| 2026-10-02 | Frontend configuration is read from `<meta>` tags | Avoids inline scripts, which the strict CSP blocks |
| 2026-10-02 | CSRF is validated by one middleware for every non-GET request; only login and register are exempt | A router can no longer forget the check; the two exempt routes have no session yet and are rate limited |
| 2026-10-02 | Removed the "development demo auto-verification" (a doctor became verified after uploading any two files) | Violates the spec: the auto-check never approves anyone and an admin must approve. Seeds create a verified demo doctor instead |
| 2026-10-02 | `LLM_PROVIDER` (`gemini` \| `mock`) added to `.env`; mock is never selected implicitly. A missing key now fails with a clear message | The old mock returned the same headache/Ibuprofen note for **any** transcript whenever a key was missing, i.e. it fabricated clinical content. Production refuses `mock` |
| 2026-10-02 | `STT_PROVIDER=mock` is refused in production | It returns a fixed transcript that ignores the audio |
| 2026-10-02 | Gemini key is sent in the `x-goog-api-key` header, and `responseSchema` is not sent | Keeps the key out of URLs and logs; Gemini's schema subset rejects the `$ref`/`$defs` Pydantic emits, so the schema goes into the prompt and Pydantic validates the reply |
| 2026-10-02 | `STORAGE_BACKEND` (`local` \| `s3`), `LOCAL_STORAGE_DIR` added; production requires `s3` | Local development without AWS; tests use a temp dir so they never touch a developer's `.local_storage` |
| 2026-10-02 | Upload validation requires extension, declared MIME **and** magic bytes to agree (per-extension rules, WAV/WEBP and ZIP distinguished) | Spec 11.4; the old check accepted any bytes for audio and let a WAV pass as WEBP |
| 2026-10-02 | Request bodies are capped by an ASGI middleware (JSON 2 MB, multipart = largest upload + 1 MB) | Spec: max request body size; it works with or without a Content-Length header |
| 2026-10-02 | Doctor status transitions live only in `transition_doctor_status`; registration is the explicit `(new → pending)` transition by a system actor; approval requires a license file; admin endpoints are bound to their source state | Spec 10.1. Found that `/reinstate` could approve a pending doctor because both end in `verified` |
| 2026-10-02 | Registry matching and the duplicate flag normalise case, spacing, punctuation, titles and name order | A near-duplicate registration number should still be flagged for the admin |
| 2026-10-02 | The partial unique index on `(council, reg_number)` stays; an exact duplicate registration is refused with the generic "could not create account" | It prevents impersonation of a held number; the duplicate flag still catches formatting variants |
| 2026-10-02 | Doctor, patient and admin visibility of consults/prescriptions is metadata-only except for the owning doctor; admins reach clinical content only through a report (`reports.consult_id`), audit-logged with the report id | Spec 9 (admin content rule). The old API returned full transcripts and prescriptions to admins and patients |
| 2026-10-02 | Every doctor-facing consult/prescription route also requires an **active consent**, checked on every request | Spec 25: revoking consent must block the doctor immediately |
| 2026-10-02 | Consult/SOAP/prescription endpoints enforce state guards (409 on invalid moves); SOAP approve, prescription approve and the agent trigger are idempotent and use row locks | Spec 25 (double clicks, concurrent edits). Tested with real PostgreSQL concurrency tests |
| 2026-10-02 | Editing never mutates an approved prescription: `PUT` creates version+1 (older draft → `superseded`; an approved version stays visible to the patient until the new one is approved, then becomes `superseded`) | Spec 15.7 immutability and "patient sees the latest approved version" |
| 2026-10-02 | Approval code `RX-<year>-<6 digits>-v<version>`: the number is shared by all versions of a consult; unique index plus retry | Stable reference per prescription; no duplicate codes under concurrency |
| 2026-10-02 | Safety flags come from `services/safety.py`, deterministic code: numbers must match with their unit near the drug's own mention (window ends at the next drug and starts at the sentence start), frequency synonyms, week/day equivalence, fuzzy spelling, allergy class map; flag ids are stable hashes; `acknowledged` is stored on high flags at approval | Spec 15.3/15.5. The old check accepted "5 days" if "7 days" appeared anywhere, and an allergy string with a trailing comma flagged every drug |
| 2026-10-02 | A drug counts as sourced if it is in the transcript **or the doctor-approved SOAP plan** | Spec 15.3 wording; the plan is reviewed by the doctor before the agent runs |
| 2026-10-02 | The agent re-checks "doctor still verified" before every node and "consent still active" before storing | Spec 25: suspension mid-pipeline aborts at the next node |
| 2026-10-02 | Background jobs are plain functions on a 4-thread pool (`run_in_background`), with an inline mode for tests; stale jobs are failed by a startup sweeper **and** lazily on every consult read (10 minutes) *(revised)* | A startup-only sweep misses jobs killed shortly before a restart. `agent_runs.created_at` added so never-started runs can be aged out |
| 2026-10-02 | Draft banner goes in the document page header; the first body block is the doctor header table (details left, photo right) | Spec 15.6 asks for both "banner at top of page 1" and "doctor header at the very top"; the header region satisfies both and the first block is the doctor block |
| 2026-10-02 | TTS cache key is deterministic: `patient/<id>/prescription-audio/<prescription_id>-v<version>.mp3`; speak/session are 404 when `VOICE_AGENT_PROVIDER=none` | Spec 13: cached per prescription id + version; "none" must disable everything cleanly |
| 2026-10-02 | Agent session returns `signed_url` plus `dynamic_variables` (summary, first name, guardrails); the agent's own prompt must use them | The signed-URL endpoint takes no context; the API key never reaches the browser. Marked VERIFY AGAINST DOCS |
| 2026-10-02 | CSP is exactly the spec's (no inline style, no external fonts or images); the ElevenLabs websocket host is added to `connect-src` only when voice is enabled | Spec 18; the old CSP allowed Google Fonts, unsplash and inline styles |
| 2026-10-02 | Frontend rewritten with DOM builders (`h()`), never `innerHTML`; 24 view templates as `<template>` elements | The old frontend interpolated server data into HTML (stored HTML injection), called endpoints that do not exist and used inline handlers the CSP blocks |
| 2026-10-02 | Routers using slowapi do not use `from __future__ import annotations` | slowapi's wrapper hides the module globals, so string annotations such as `UploadFile` or `UUID` cannot be resolved |
| 2026-10-02 | Validation errors return `{"detail": "field: message; ..."}` without the submitted values | The default FastAPI body echoes the input, including passwords |
| 2026-10-02 | Alembic migration 2 adds foreign keys, `reports.consult_id`, `agent_runs.created_at`, a unique `approval_code`, SQLite-compatible partial indexes and PostgreSQL append-only triggers | Spec 7: the first migration left most references as bare UUIDs. Verified up/down/up with existing rows on SQLite and PostgreSQL |
| 2026-10-02 | `requirements.txt` is runtime-only; tests and lint tools are in `requirements-dev.txt` | Spec: no unused dependencies in the deployed image |
| 2026-10-02 | The suite runs on SQLite by default and on PostgreSQL with `TEST_DATABASE_URL` | Production is PostgreSQL (enums, JSONB, triggers, row locks); local work stays easy |
