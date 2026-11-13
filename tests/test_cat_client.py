"""The CAT side, against a fake CAT (httpx.MockTransport). No database, no network.

Covers the CAT conversation (sign-in, upload, busy recalculate, polling,
Proof of Income), the document-type rules, the Katie notice signature, and
that the routes already in production are all still served.
"""
import hashlib
import hmac
import json

import httpx
import pytest

from app.config import settings
from app.services import cat_client as cc
from app.services import cat_sync_service as svc
from app.services import results_notifier

PROOF = {"evidence_ledger": [
    {"group": "Proof of Income", "label": "Net wages", "status": "verified",
     "crm_value": 1500.0, "doc_value": 1498.2, "bank_value": 1498.2, "found_value": 1498.2,
     "variance": 1.8, "pay_frequency": "monthly", "narrative": "Payslip matches bank."},
    {"group": "Housing", "label": "Rent", "status": "variance", "crm_value": 600},
]}


class FakeCat:
    """A scripted CAT: session login or Bearer key, uploads, a busy first read."""

    def __init__(self, *, key=None, busy_times=1, reads_needed=1, upload_status=201,
                 never_reads=False):
        self.key = key
        self.busy_times = busy_times
        self.reads_needed = reads_needed
        self.upload_status = upload_status
        self.never_reads = never_reads
        self.uploaded: list[dict] = []
        self.calls: list[str] = []
        self.polls = 0

    def _authed(self, request):
        if self.key:
            return request.headers.get("authorization") == f"Bearer {self.key}"
        # Django: the session cookie always; the CSRF header on writes only.
        return "sessionid=abc" in request.headers.get("cookie", "") and (
            request.method == "GET" or request.headers.get("x-csrftoken") == "tok")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/api/auth/me/":
            return httpx.Response(200, json={}, headers={"set-cookie": "csrftoken=tok; Path=/"})
        if path == "/api/auth/login/":
            body = json.loads(request.content)
            if body == {"email": "a@b.c", "password": "pw"}:
                return httpx.Response(200, json={}, headers={"set-cookie": "sessionid=abc; Path=/"})
            return httpx.Response(400, json={"detail": "bad login"})
        if not self._authed(request):
            return httpx.Response(401, json={"detail": "auth"})
        if path.endswith("/proof/upload/"):
            if self.upload_status != 201:
                return httpx.Response(self.upload_status, json={"detail": "case locked"})
            text = request.content.decode("latin-1")
            name = text.split('filename="', 1)[1].split('"', 1)[0]
            dtype = text.split('name="document_type"', 1)[1].split("\r\n\r\n", 1)[1].split("\r\n", 1)[0]
            self.uploaded.append({"name": name, "document_type": dtype})
            return httpx.Response(201, json={"id": len(self.uploaded)})
        if path.endswith("/proof/recalculate/"):
            if self.busy_times > 0:
                self.busy_times -= 1
                return httpx.Response(409, json={"detail": "busy"})
            return httpx.Response(202, json={})
        if path.endswith("/proof/"):
            return httpx.Response(200, json=PROOF)
        if path.startswith("/api/applications/"):
            self.polls += 1
            state = "processed" if (self.polls >= self.reads_needed and not self.never_reads) \
                else "processing"
            return httpx.Response(200, json={"documents": [
                {"document_name": u["name"], "processing_status": state,
                 "document_type": u["document_type"],
                 "extracted_data": {"gross_pay": 1800, "net_pay": 1498.2}}
                for u in self.uploaded]})
        return httpx.Response(404)


async def _no_sleep(_):
    return None


def _cfg(**kw):
    return cc.Config(base_url="http://cat", **({"email": "a@b.c", "password": "pw"} | kw))


def _item(key="d1", name="324991_income_proof_abcd1234.pdf", dtype="income_proof"):
    return svc.Outgoing(key=key, cat_name=name, cat_document_type=dtype, content=b"%PDF",
                        mime_type="application/pdf", notes="n")


TIMING = svc.Timing(poll_seconds=0, read_timeout_seconds=60, busy_retry_seconds=0)


async def _pass(fake, cfg, outgoing, awaiting=(), force_read=False):
    async with cc.CatClient(cfg, transport=httpx.MockTransport(fake)) as cat:
        return await svc.push_and_read(cat, "324991", list(outgoing), list(awaiting),
                                       force_read=force_read, timing=TIMING, sleep=_no_sleep)


@pytest.mark.asyncio
async def test_uploads_waits_out_a_busy_case_and_returns_proof_of_income():
    fake = FakeCat(busy_times=2, reads_needed=2)
    out = await _pass(fake, _cfg(), [_item(), _item("d2", "324991_bank_statement_ef01.pdf",
                                                     "bank_statement")])
    assert out.ok and out.read_state == "done" and out.error is None
    assert [u["document_type"] for u in fake.uploaded] == ["income_proof", "bank_statement"]
    assert fake.calls.count("POST /api/applications/324991/proof/recalculate/") == 3
    assert out.uploads["d1"]["ok"] and out.uploads["d2"]["ok"]
    assert out.readings["324991_income_proof_abcd1234.pdf"]["state"] == "processed"
    assert out.readings["324991_income_proof_abcd1234.pdf"]["net_pay"] == 1498.2
    # Only the Proof of Income rows are income; the whole ledger is kept too.
    assert [r["label"] for r in out.income] == ["Net wages"]
    assert out.income[0]["status"] == "verified"
    assert len(out.evidence_ledger) == 2


@pytest.mark.asyncio
async def test_bearer_key_skips_the_login():
    fake = FakeCat(key="k1", busy_times=0)
    out = await _pass(fake, cc.Config(base_url="http://cat", api_key="Bearer k1"), [_item()])
    assert out.ok
    assert "POST /api/auth/login/" not in fake.calls


@pytest.mark.asyncio
async def test_a_refused_upload_is_reported_and_nothing_is_read():
    fake = FakeCat(upload_status=403)
    out = await _pass(fake, _cfg(), [_item()])
    assert not out.ok and "403" in out.error
    assert not out.uploads["d1"]["ok"]
    assert not any("recalculate" in c for c in fake.calls)


@pytest.mark.asyncio
async def test_bad_sign_in_sends_nothing():
    fake = FakeCat()
    out = await _pass(fake, _cfg(password="wrong"), [_item()])
    assert not out.ok and "sign-in failed" in out.error
    assert fake.uploaded == []


@pytest.mark.asyncio
async def test_no_sign_in_configured():
    out = await _pass(FakeCat(), cc.Config(base_url="http://cat"), [_item()])
    assert not out.ok and "CAT_API_KEY" in out.error


@pytest.mark.asyncio
async def test_a_read_that_never_finishes_times_out_but_keeps_the_income():
    fake = FakeCat(busy_times=0, never_reads=True)
    timing = svc.Timing(poll_seconds=0, read_timeout_seconds=0.05, busy_retry_seconds=0)
    import asyncio

    async def tick(_):
        await asyncio.sleep(0.02)

    async with cc.CatClient(_cfg(), transport=httpx.MockTransport(fake)) as cat:
        out = await svc.push_and_read(cat, "324991", [_item()], [], force_read=False,
                                      timing=timing, sleep=tick)
    assert not out.ok and out.read_state == "timeout"
    assert out.income   # what CAT had so far is still returned


@pytest.mark.asyncio
async def test_nothing_new_and_no_refresh_does_not_bother_cat():
    fake = FakeCat()
    out = await _pass(fake, _cfg(), [])
    assert out.ok and out.read_state == "not_needed"
    assert not any("recalculate" in c for c in fake.calls)


@pytest.mark.asyncio
async def test_a_manual_refresh_reads_documents_cat_already_has():
    fake = FakeCat(busy_times=0)
    fake.uploaded.append({"name": "324991_income_proof_abcd1234.pdf",
                          "document_type": "income_proof"})
    out = await _pass(fake, _cfg(), [], awaiting=["324991_income_proof_abcd1234.pdf"],
                      force_read=True)
    assert out.ok and out.read_state == "done"
    assert out.readings["324991_income_proof_abcd1234.pdf"]["state"] == "processed"


def test_case_map():
    assert cc.case_for("324991", "324991:414290, 1:2") == "414290"
    assert cc.case_for("555", "324991:414290") == "555"


def test_which_documents_go_to_cat(monkeypatch):
    monkeypatch.setattr(settings, "cat_enabled", True)
    assert svc.wants("income_proof") and svc.wants("cis_slip")
    assert svc.cat_type_for("credit_journal") == "credit_journal"
    assert not svc.wants("id_proof")                       # not an income document
    assert not svc.wants("creditor_liaison")               # call recordings never
    assert svc.wants("bank_statement")
    monkeypatch.setattr(settings, "cat_send_bank_statements", False)
    assert not svc.wants("bank_statement")                 # LedgerFlow only
    monkeypatch.setattr(settings, "cat_enabled", False)
    assert not svc.wants("income_proof")                   # off by default


def test_cat_names_are_unique_per_document():
    import uuid
    a = svc.cat_name_for("324 991", "income_proof", uuid.UUID(int=1), "Pay Slip.PDF")
    b = svc.cat_name_for("324 991", "income_proof", uuid.UUID(int=2), "Pay Slip.PDF")
    assert a != b and a.endswith(".pdf") and a.startswith("324_991_income_proof_")


@pytest.mark.asyncio
async def test_katie_notice_is_signed_and_carries_no_figures(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204)

    monkeypatch.setattr(settings, "results_webhook_url", "http://katie/hook")
    monkeypatch.setattr(settings, "results_webhook_secret", "s3cret")
    ok = await results_notifier.notify("income.ready", "324991", "done",
                                       transport=httpx.MockTransport(handler))
    assert ok
    body = seen[0].content
    assert json.loads(body).keys() == {"event", "owner_ref", "status", "at"}
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert seen[0].headers["x-signature"] == expected


@pytest.mark.asyncio
async def test_no_webhook_configured_sends_nothing(monkeypatch):
    monkeypatch.setattr(settings, "results_webhook_url", "")
    assert await results_notifier.notify("income.ready", "1", "done") is False


def test_every_route_already_served_is_still_served():
    from app.main import app
    paths = {(m, r.path) for r in app.routes for m in getattr(r, "methods", ()) or ()}
    before = {
        ("GET", "/health"), ("POST", "/v1/documents"), ("GET", "/v1/documents"),
        ("GET", "/v1/documents/{doc_id}"), ("POST", "/v1/document-requests"),
        ("POST", "/v1/document-requests/token"), ("GET", "/v1/document-requests"),
        ("GET", "/v1/categorisation"), ("POST", "/v1/categorisation"),
        ("GET", "/staff"), ("POST", "/staff/generate"), ("GET", "/staff/documents"),
        ("GET", "/u/{reference}/{doc_type}/{token}"),
        ("POST", "/u/{reference}/{doc_type}/{token}"), ("GET", "/files/{key:path}"),
    }
    missing = {p for p in before if p not in paths}
    assert not missing, missing
    assert ("GET", "/v1/case-results") in paths and ("POST", "/v1/cat") in paths
