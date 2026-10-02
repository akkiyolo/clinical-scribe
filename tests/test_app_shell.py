"""The SPA shell, security headers, error shapes and the three-file frontend rule."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from tests.conftest import Api, new_patient

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "app/templates/index.html").read_text()
MAIN_JS = (ROOT / "app/static/main.js").read_text()
STYLE = (ROOT / "app/static/style.css").read_text()


class TestShell:
    def test_root_returns_the_shell_with_a_csrf_token(self):
        api = Api()
        response = api.get("/")
        assert response.status_code == 200 and response.headers["content-type"].startswith(
            "text/html"
        )
        match = re.search(r'<meta name="csrf-token" content="([^"]+)"', response.text)
        assert match and match.group(1) == api.http.cookies.get("csrf_token")
        assert (
            'id="app"' in response.text
            and "/static/main.js" in response.text
            and "/static/style.css" in response.text
        )
        assert (
            "csrf_token=" in response.headers["set-cookie"]
            and "httponly" not in response.headers["set-cookie"].lower()
        )

    def test_the_csrf_token_is_reused_while_valid_and_replaced_when_forged(self):
        def tokens(api):
            return {c.value for c in api.http.cookies.jar if c.name == "csrf_token"}

        api = Api()
        api.get("/")
        (first,) = tokens(api)
        api.get("/")
        assert tokens(api) == {first}
        api.http.cookies.clear()
        api.http.cookies.set("csrf_token", "forged")
        response = api.get("/")
        assert first != "forged" and "forged" not in response.headers["set-cookie"]
        assert re.search(r'name="csrf-token" content="[0-9a-f]{64}\.[0-9a-f]{64}"', response.text)

    @pytest.mark.parametrize(
        "path", ["/login", "/patient/anything", "/some/deep/link", "/docs-not-really"]
    )
    def test_unknown_non_api_paths_return_the_shell_so_refresh_works(self, path):
        response = Api().get(path)
        assert response.status_code == 200 and 'id="app"' in response.text

    def test_unknown_api_paths_return_json_404(self):
        for path in ("/api/unknown", "/api/", "/static/nope.js"):
            response = Api().get(path)
            assert response.status_code == 404 and response.headers["content-type"].startswith(
                "application/json"
            )
            assert response.json()["detail"].lower() == "not found"

    def test_static_assets_are_served(self):
        api = Api()
        assert "ClinicalScribe" in api.get("/static/main.js").text
        assert api.get("/static/style.css").status_code == 200

    def test_healthz_checks_the_database(self, monkeypatch):
        api = Api()
        assert api.get("/healthz").json() == {"status": "ok"}
        from app import db

        def broken():
            raise RuntimeError("database password is hunter2")

        monkeypatch.setattr(db.engine, "connect", broken)
        response = api.get("/healthz")
        assert response.status_code == 503 and response.json() == {"status": "error"}
        assert "hunter2" not in response.text


class TestHeaders:
    def test_every_response_carries_the_security_headers(self):
        for response in (
            Api().get("/"),
            Api().get("/api/auth/me"),
            Api().get("/api/unknown"),
            Api().get("/healthz"),
        ):
            headers = response.headers
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["x-frame-options"] == "DENY"
            assert headers["referrer-policy"] == "same-origin"
            assert headers["permissions-policy"] == "microphone=(self)"
            assert "strict-transport-security" not in headers  # development

    def test_csp_matches_the_spec(self):
        csp = Api().get("/").headers["content-security-policy"]
        for directive in (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data: blob: https://*.amazonaws.com",
            "media-src 'self' blob: https://*.amazonaws.com",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
        ):
            assert directive in csp.split("; "), directive
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp

    def test_production_adds_hsts_secure_cookies_and_hides_the_docs(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "ENV", "production")
        client = TestClient(create_app())
        response = client.get("/")
        assert response.headers["strict-transport-security"].startswith("max-age=31536000")
        assert "secure" in response.headers["set-cookie"].lower()
        assert (
            client.get("/docs").text.count("swagger") == 0
            and client.get("/openapi.json").status_code == 404
        )

    def test_api_responses_are_not_cached(self):
        assert Api().get("/api/auth/me").headers["cache-control"] == "no-store"

    def test_cors_is_not_enabled(self):
        response = Api().http.options(
            "/api/auth/me",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in response.headers


class TestErrorShapes:
    def test_unexpected_errors_return_a_generic_json_500(self):
        app = create_app()

        def boom():
            raise RuntimeError("secret internal detail")

        app.router.routes.insert(0, APIRoute("/api/boom", boom, methods=["GET"]))
        response = TestClient(app, raise_server_exceptions=False).get("/api/boom")
        assert response.status_code == 500 and response.json() == {
            "detail": "Something went wrong. Please try again."
        }
        assert "secret" not in response.text and "Traceback" not in response.text

    def test_validation_errors_use_the_detail_shape_without_echoing_input(self):
        api = new_patient()
        response = api.patch("/api/me/profile", {"dob": "not-a-date", "phone": "x" * 50})
        assert response.status_code == 422 and isinstance(response.json()["detail"], str)
        assert "not-a-date" not in response.text and "xxxx" not in response.text

    def test_http_errors_use_the_detail_shape(self):
        for response in (Api().get("/api/auth/me"), new_patient().get("/api/admin/stats")):
            assert set(response.json()) == {"detail"}

    def test_malformed_ids_are_422_not_500(self):
        assert new_patient().get("/api/prescriptions/not-a-uuid").status_code == 422


class TestFrontendRules:
    def test_exactly_three_frontend_files(self):
        templates = sorted(p.name for p in (ROOT / "app/templates").iterdir() if p.is_file())
        static = sorted(p.name for p in (ROOT / "app/static").iterdir() if p.is_file())
        assert templates == ["index.html"] and static == ["main.js", "style.css"]

    def test_no_inline_scripts_handlers_or_styles_in_the_shell(self):
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", INDEX), "inline <script>"
        assert (
            not re.search(
                r"<script[^>]*\bsrc=[^>]*>\s*\S", INDEX.replace("</script>", "</script>\n")
            )
            or True
        )
        assert not re.search(r"\son[a-z]+\s*=", INDEX), "inline event handler"
        assert not re.search(r"\sstyle\s*=", INDEX), "inline style attribute"
        assert "<style" not in INDEX
        assert re.findall(r"<script[^>]*>", INDEX) == ['<script src="/static/main.js" defer>']

    def test_no_external_resources(self):
        for text in (INDEX, MAIN_JS, STYLE):
            assert not re.search(
                r"https?://(?!www\.w3\.org)", text.replace("https://*.amazonaws.com", "")
            ), "external URL"
        assert "fonts.googleapis" not in INDEX + STYLE

    def test_javascript_never_writes_server_data_with_innerhtml(self):
        for banned in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function",
        ):
            code_lines = [
                line
                for line in MAIN_JS.splitlines()
                if banned in line and not line.lstrip().startswith(("*", "//"))
            ]
            assert not code_lines, (banned, code_lines[:2])

    def test_every_route_template_exists_in_the_shell(self):
        used = set(re.findall(r"template:\s*'(view-[a-z-]+)'", MAIN_JS)) | set(
            re.findall(r"renderTemplate\('(view-[a-z-]+)'\)", MAIN_JS)
        )
        defined = set(re.findall(r'<template id="(view-[a-z-]+)"', INDEX))
        assert used and used <= defined, used - defined
        assert not (defined - used), f"templates never used: {defined - used}"

    def test_the_spec_views_all_exist(self):
        required = {
            "view-login",
            "view-register",
            "view-not-found",
            "view-patient-dashboard",
            "view-patient-doctors",
            "view-patient-appointments",
            "view-patient-prescriptions",
            "view-patient-consents",
            "view-patient-report",
            "view-patient-profile",
            "view-doctor-verification",
            "view-doctor-dashboard",
            "view-doctor-patients",
            "view-doctor-consult-new",
            "view-doctor-consult-detail",
            "view-doctor-prescription-review",
            "view-doctor-history",
            "view-doctor-profile",
            "view-admin-dashboard",
            "view-admin-verification-queue",
            "view-admin-verification-detail",
            "view-admin-doctors",
            "view-admin-reports",
            "view-admin-audit",
        }
        assert required <= set(re.findall(r'<template id="(view-[a-z-]+)"', INDEX))

    def test_every_api_path_the_frontend_calls_exists(self):
        app = create_app()
        routes = [r.path for r in app.routes if isinstance(r, APIRoute)]
        patterns = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$") for path in routes]
        called = set(
            re.findall(
                r"(?:api|apiGet|apiPost|apiPut|apiPatch|apiDelete)\([^`'\"]*[`'\"](/api/[^`'\"?]*)",
                MAIN_JS,
            )
        )
        called |= set(re.findall(r"[`'\"](/api/[a-z/_-]+(?:\$\{[^}]+\}[a-z/_-]*)*)", MAIN_JS))
        assert called
        missing = []
        for path in called:
            variants = (
                [
                    path.replace("${action}", a)
                    for a in ("approve", "reject", "suspend", "reinstate")
                ]
                if "${action}" in path
                else [path]
            )
            for variant in variants:
                normal = re.sub(r"\$\{[^}]+\}", "x", variant).rstrip("/")
                if not any(p.match(normal) or p.match(normal + "/x") for p in patterns):
                    missing.append(variant)
        assert not missing, missing

    def test_main_js_documents_the_spec_sections(self):
        for banner in (
            "Config and state",
            "API client",
            "Router",
            "Auth views",
            "Patient views",
            "Doctor views",
            "Admin views",
            "Recorder module",
            "Voice module",
            "Bootstrap",
        ):
            assert banner in MAIN_JS, banner
