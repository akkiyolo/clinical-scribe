# ClinicalScribe: changes made (2026-10-02)

This is the change log for the review, bug-fix and test pass done on the project received as `clinical scribe.zip`. It lists what was wrong, what was changed, how it was checked, and what was **not** checked.

## 1. Short version

- The code you sent was a working skeleton: about 9,000 lines, 4 smoke tests, the full prescription lifecycle untested, and a frontend that did not match the backend.
- I read all of it against `ClinicalScribe_Build_Prompt.md` and fixed what was broken, unsafe or missing. Several fixes were real security or clinical-safety problems (section 3).
- The frontend was **rewritten** (the old one called endpoints that do not exist and put server data into `innerHTML`).
- The test suite went from **4** to **435** tests. Last full runs: **SQLite 428 passed, 7 skipped**, **PostgreSQL 16 435 passed, 0 failed** (the 7 skipped on SQLite are PostgreSQL-only: append-only triggers and real concurrency).
- The whole workflow was also driven by hand in a real browser (Chromium) against PostgreSQL: register, license upload, admin approval, booking, consent, recording, upload, paste, SOAP, agent, review, edit, approve, patient download, report, audit, suspension in a live session.

### Things you must know

1. **The API keys in `.env` are placeholders** (`your-…`, `AKIAXXXX…`, `change-me…`). I checked every value without printing them. So nothing that needs ElevenLabs, Gemini or AWS could be tested live. Those parts are covered with stubbed HTTP and moto (S3) only.
2. I added `LLM_PROVIDER=mock` to your `.env` so your local demo keeps working. The UI shows a yellow "demo" banner while it is on. Change it to `gemini` when you put a real `LLM_API_KEY`.
3. Nothing was committed or pushed. The git remote in this folder is `akkiyolo/clinical-scribe`, which is not mine to push to. All changes are plain working-tree changes. `git status` shows them.
4. The zip leaves out `.venv` (your Windows virtual environment, 246 MB), my `.venv-linux`, and caches. Recreate with `pip install -r requirements-dev.txt`.

## 2. How to run it

```bash
python3.11 -m venv .venv && source .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
alembic upgrade head                 # applies the new migration to an existing database too (back up first)
python -m scripts.create_admin       # set ADMIN_BOOTSTRAP_PASSWORD to 12+ chars with letters and digits first
python -m scripts.seed_demo          # optional synthetic data
uvicorn app.main:app --reload
pytest                               # SQLite; TEST_DATABASE_URL=postgresql://… pytest for PostgreSQL
```

Demo logins after `seed_demo` (password `DemoPass123`): `dr.demo@example.com`, `dr.pending@example.com`, `patient.demo@example.com`. See `README.md` for everything else.

## 3. Problems found and fixed

### Security and clinical safety

| # | What was wrong | Fix |
|---|---|---|
| 1 | In development a doctor became **verified automatically** after uploading any two files, and `ENV=development` is what your `.env` uses. The spec says the auto-check never approves anyone. | Removed. Only an admin approves. Approval also requires an uploaded license. |
| 2 | The mock LLM returned the **same headache / Ibuprofen note and prescription for any transcript** and was used silently whenever the key was missing or a placeholder. That is fabricated clinical content. | Mock is now an explicit opt-in (`LLM_PROVIDER=mock`), labelled in the UI, refused in production. With no key the app fails with a clear message and a Retry. |
| 3 | `POST /admin/doctors/{id}/reinstate` could **approve a pending doctor** (both end in "verified"). Found by a new test. | Every admin action is bound to its required source state (409 otherwise). |
| 4 | Admins and patients could read **full transcripts, SOAP notes and prescription content**. | Patients and admins get metadata only. Admins can open a consult's content only through a report that references it, and that access is audit-logged with the report id. |
| 5 | A doctor's consent was checked only when creating a consult. After the patient revoked consent the doctor kept access. | Consent is checked on **every** consult, SOAP, prescription, file and voice request. Lists hide revoked patients. The agent aborts if consent is revoked mid-run. |
| 6 | Pending, rejected and suspended doctors could reach several clinical endpoints (consult reads, appointment updates, directory, versions…). | A parametrised test now calls every clinical route as each of those states and expects 403 `Your account is not verified yet`. |
| 7 | `GET /api/doctors` (the directory) needed no login. | Requires login; unverified doctors cannot browse it. Public fields only. |
| 8 | The prescription safety check was weak: "5 days" was accepted if "7 days" appeared anywhere; one drug's dose could support another's; an allergy string with a trailing comma flagged **every** drug; "none known" counted as an allergy; spelling variants were missed; flags used random ids. | New deterministic module `services/safety.py`: numbers must match with their unit near the drug's own sentence, frequency synonyms (BD = twice daily), week/day equivalence, fuzzy drug spelling, allergy classes (penicillin, sulfa, NSAID cross-sensitivity…), allergies mentioned in the consult, stable flag ids. 42 unit tests. |
| 9 | Frontend built HTML with `innerHTML` from server data (stored HTML injection through names, reasons, clinic names…). | Rewritten with DOM builders; a test fails if `innerHTML` appears. Checked live with a `<img onerror>` name. |
| 10 | Validation errors echoed the submitted values, including **passwords**. | Errors are `{"detail": "field: message"}` without input. |
| 11 | Upload checks accepted any bytes for audio, and let a WAV pass as WEBP. Extension, MIME and content were not required to agree. | Per-extension rules: extension, declared MIME and magic bytes must all match. Bounded reads. A body-size middleware answers 413. |
| 12 | CSP allowed inline styles, Google Fonts and stock photos, and the page used inline `onclick` (which the CSP blocks, so those links were dead). | CSP is exactly the spec's; no inline script or style; no external resources; the ElevenLabs websocket host is allowed only when voice is on. |
| 13 | Voice: the cache lookup was `LIKE '%<8 chars of id>%'` (cross-prescription collisions), an admin could hear any prescription, and the agent session used the wrong HTTP method and path and ignored the prescription context. | Deterministic cache key `prescription id + version`, patient or owning doctor only, `404` when voice is off, `GET …/get-signed-url` with dynamic variables. |
| 14 | Gemini key was sent in the URL; `responseSchema` built from Pydantic (`$ref`) would be rejected by the real API. | Key in the `x-goog-api-key` header, schema in the prompt, Pydantic validation and retries. |
| 15 | Login rate limit was per IP only. | Also 5/minute per email (spec). |
| 16 | Approval codes were `count + 1` (race → duplicates). | Unique index, shared number per consult, retry on collision. |
| 17 | Audit rows for create events had `resource_id = None` (id assigned after the audit call). A failed audit insert could poison a PostgreSQL transaction. | Ids are flushed first; audit inserts use a SAVEPOINT. |

### Workflow correctness

- **State guards** (409 on invalid moves) on appointments, transcript, SOAP generate/edit/approve, audio upload, retry. A transcript or SOAP can no longer be edited after approval.
- **Idempotency and locks**: SOAP approve, prescription approve, consent grant and prescription edit use row locks. Verified with real PostgreSQL concurrency tests (4 simultaneous requests → exactly one agent run, one approval code, one new version, one consent).
- **Versioning** follows the spec: editing a draft creates v+1 and supersedes the old draft; editing an *approved* prescription creates a new draft and leaves the approved one (and the patient's view) untouched until the new one is approved; older approved versions become `superseded`; `expected_version` gives a 409 on concurrent edits.
- **Reject / regenerate** with an optional doctor note, and a consult-level "regenerate" when no draft is open.
- **Agent**: re-checks "doctor still verified" before every node, stores a run summary in `agent_runs.graph_state`, idempotent store step, readable failure messages, Retry never duplicates rows.
- **Stuck jobs**: the old sweeper only ran at startup and only for `running` rows. Now stale queued/running runs, transcriptions and SOAP jobs are failed at startup **and** whenever a consult is read.
- **Audio**: `DELETE_AUDIO_AFTER_TRANSCRIPTION` now removes the files row too and audits it.
- **Registration**: the doctor registration uses the explicit `(new → pending)` transition; an exact duplicate council + registration number no longer crashes with a 500; registry matching ignores case/spacing/punctuation/titles/name order.
- **Appointments**: a real status machine, no past dates, admins cannot edit, `reason_for_visit` is saved.
- **Resubmission** after rejection needs a newly uploaded certificate.

### Database

New Alembic migration `c91d5e2a7f10` (tested up/down/up, and on a populated old database, on both SQLite and PostgreSQL):
foreign keys the spec requires, `reports.consult_id`, `agent_runs.created_at`, unique `approval_code`, partial unique indexes that work on SQLite too, and **PostgreSQL triggers that make `audit_logs` and `verification_events` reject UPDATE, DELETE and TRUNCATE**.

### Documents (.docx)

Doctor header table is the first block (name, specialization, registration + council, verified date, clinic, photo on the right), the draft banner is in the page header and footer, A4 with 2 cm margins, fixed column widths, page numbers, IST timestamps, approved copy with approval code and no flags. Opened and converted with LibreOffice to check it renders.

### Frontend (rewritten: `index.html`, `style.css`, `main.js`, nothing else)

- 24 view `<template>`s covering every view in the spec; hash router with role guards; API client with CSRF, 401/403/429 handling; toasts; accessible modal with focus trap and Escape; paged tables; polling that stops when you leave the page; upload with progress.
- Doctor consult screen: stepper, record / upload / paste, SOAP editor with ICD-10 chips, progress states and Retry. Prescription review: editable medications, flags beside the transcript with highlighted source quotes, acknowledgment checkbox, save-as-new-version, reject/regenerate, approve.
- Recorder: permission only on click, timer, pause/resume, stop, preview, discard, max length from `MAX_AUDIO_MB`, track cleanup, unsupported-browser fallback.
- Voice module (read-aloud plus a beta agent session); hidden when voice is off.
- Old bugs gone: the admin queue called `/api/admin/verifications` (does not exist), "cancel appointment" called a missing endpoint, booking sent the wrong field name, a `?patient=` link broke the router.

### Config, deployment, scripts

- `config.py`: stricter production validation in a testable function; `LLM_PROVIDER`, `STORAGE_BACKEND`, `LOCAL_STORAGE_DIR`.
- `render.yaml` lacked `STORAGE_BACKEND=s3` (production would not have started); fixed, plus Python version and voice settings.
- `requirements.txt` is runtime-only now; test and lint tools moved to `requirements-dev.txt`.
- Spec scripts: `scripts/seed_registry.py` (20 fake records) and `scripts/seed_demo.py` (replaces `seed.py`); `create_admin.py` unchanged in behaviour.
- `.env.example` matches the spec keys plus four documented extras; `.gitignore` extended.
- README rewritten to the spec's section list (setup, env table, Render, S3 + IAM policy, ElevenLabs, tests, demo walkthrough, security notes, limitations, production path). `docs/DECISIONS.md` rewritten.
- Code formatted with black and ruff (both clean).

## 4. How it was checked

| Check | Result |
|---|---|
| `pytest` on SQLite | 428 passed, 7 skipped |
| `TEST_DATABASE_URL=… pytest` on PostgreSQL 16 | 435 passed, 0 failed |
| `ruff check` and `black --check` | clean |
| Migrations: fresh, up/down/up, populated old DB | pass on SQLite and PostgreSQL; models and migrations agree (`compare_metadata`) |
| Browser run on a fresh PostgreSQL database following the README walkthrough | pass; no 5xx in the server log |
| Mobile width (375 px) on all doctor routes | no horizontal overflow |
| Fresh setup from your `.env` (SQLite, mock providers) in a temp copy | migrate, admin, seed, login all work |

Test files: `test_auth`, `test_verification`, `test_access_control`, `test_prescriptions`, `test_files_storage`, `test_speech_voice`, `test_safety`, `test_app_shell`, `test_config`, `test_ops`. They follow section 22 of the spec: auth, license gate on every clinical route, IDOR on every `{id}` route, consent revocation, S3 validation and presigned redirects (moto), speech and voice with stubbed providers, SOAP and agent behaviour, safety flags, approval rules, audit, shell and CSP, configuration.

## 5. Not verified (be honest with your friend)

- **Live ElevenLabs (speech-to-text, TTS, agent), live Gemini, and a real AWS bucket.** No real credentials. Request shapes were checked against the vendors' docs and stubbed in tests; the first real call should be treated as a smoke test. The browser voice assistant (websocket audio) is marked beta and was never run against a real agent. These spots carry `# VERIFY AGAINST DOCS`.
- The Gemini model id in your config (`gemini-3.8-flash`) is unverified; check it exists for your key.
- There is no automated browser test suite; the UI was verified by hand.
- The safety checks are heuristics, not a clinical drug database; the registry is a mock table.
- Background jobs are in-process: fine for a demo, not for production (documented in the README).

## 6. Where things are

| Area | Files |
|---|---|
| Business logic | `app/services/` (`verification`, `safety`, `prescription_agent`, `prescriptions`, `docx_builder`, `scribe`, `llm`, `speech`, `voice_agent`, `storage`, `uploads`, `jobs`, `audit`, `access`, `timeutil`) |
| HTTP layer | `app/routers/` |
| Models and migrations | `app/models/`, `alembic/versions/c91d5e2a7f10_…` |
| Frontend | `app/templates/index.html`, `app/static/style.css`, `app/static/main.js` |
| Scripts | `scripts/create_admin.py`, `seed_registry.py`, `seed_demo.py` |
| Tests | `tests/` |
| Docs | `README.md`, `docs/DECISIONS.md`, this file |
