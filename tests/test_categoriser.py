"""The LedgerFlow client's rules, against a fake LedgerFlow (httpx.MockTransport).

No database and no network: categoriser_client is pure on purpose.
"""
import json

import httpx
import pytest

from app.services import categoriser_client as cc

KEY = "svc-key"
LEDGER = {"groups": [{"name": "Housing", "labels": [{"label": "Rent", "transactions": [
    {"description": "RENT LTD", "amount": -650.0}]}]}], "total_money_out": 650.0}

SUMMARY = {"period": {"latest_month": "2026-10"}, "expenditure": {"groups": [
    {"name": "Housing", "total_latest_month": 650.0, "labels": [
        {"label": "Rent", "total_latest_month": 650.0,
         "transactions": [{"date": "02 Oct 26", "description": "RENT LTD", "amount": -650.0}]}]}]}}


def _cfg(**kw):
    return cc.Config(base_url="http://ledgerflow", api_key=KEY, poll_seconds=0,
                     job_timeout_seconds=60, **kw)


async def _no_sleep(_):
    return None


class FakeLedgerFlow:
    """Scripted job statuses; records every request it receives."""

    def __init__(self, statuses, *, lose_first_job=False, submit_status=202, error=None,
                 summary_status=200):
        self.summary_status = summary_status
        self.statuses = list(statuses)
        self.lose_first_job = lose_first_job
        self.submit_status = submit_status
        self.error = error
        self.requests: list[httpx.Request] = []
        self.jobs = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"detail": "bad key"})
        path = request.url.path
        if request.method == "POST" and path == "/v1/statement-jobs":
            if self.submit_status != 202:
                return httpx.Response(self.submit_status, json={"error": "nope"})
            self.jobs += 1
            return httpx.Response(202, json={"job_id": f"job-{self.jobs}"})
        if path.endswith("/ledger"):
            return httpx.Response(200, json=LEDGER)
        if path.endswith("/summary"):
            if self.summary_status != 200:
                return httpx.Response(self.summary_status, json={"error": "no"})
            return httpx.Response(200, json=SUMMARY)
        job_id = path.rsplit("/", 1)[-1]
        if self.lose_first_job and job_id == "job-1":
            return httpx.Response(404, json={"error": "Not found"})
        state = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(200, json={"id": job_id, "status": state, "match_rate": 0.9,
                                         "error": self.error if state == "FAILED" else None,
                                         "file_errors": []})


STATEMENTS = [cc.Statement("jan.pdf", b"%PDF-1", "application/pdf"),
              cc.Statement("feb.pdf", b"%PDF-2", "application/pdf")]


async def _go(fake, cfg=None, **kw):
    seen = []

    async def on_submitted(job_id):
        seen.append(job_id)

    out = await cc.categorise(kw.pop("statements", STATEMENTS), holder="Jo Bloggs",
                              ref="324991", cfg=cfg or _cfg(), on_submitted=on_submitted,
                              transport=httpx.MockTransport(fake), sleep=_no_sleep, **kw)
    return out, seen


async def test_all_statements_go_in_one_job_with_holder_and_case_ref():
    fake = FakeLedgerFlow(["QUEUED", "CATEGORIZING", "REVIEW_READY"])
    out, seen = await _go(fake)
    assert out.ok and out.job_id == "job-1" and seen == ["job-1"]
    assert out.ledger == LEDGER and out.match_rate == 0.9
    submit = fake.requests[0]
    body = submit.read()
    assert body.count(b'name="files"') == 2
    assert b"Jo Bloggs" in body and b"324991" in body
    assert fake.jobs == 1


async def test_a_failed_job_reports_ledgerflows_reason():
    fake = FakeLedgerFlow(["FAILED"], error="No transactions could be extracted. scan.jpg")
    out, _ = await _go(fake)
    assert not out.ok and "No transactions" in out.error
    assert not any(r.url.path.endswith("/ledger") for r in fake.requests)


async def test_a_job_lost_to_a_ledgerflow_restart_is_resent_once():
    fake = FakeLedgerFlow(["FAILED", "REVIEW_READY"],
                          error="LedgerFlow restarted while this statement was being read.")
    out, seen = await _go(fake)
    assert out.ok and seen == ["job-1", "job-2"]


async def test_a_404_job_is_resent():
    fake = FakeLedgerFlow(["REVIEW_READY"], lose_first_job=True)
    out, seen = await _go(fake)
    assert out.ok and seen == ["job-1", "job-2"]


async def test_resent_at_most_max_resubmits_times():
    fake = FakeLedgerFlow(["FAILED"], error="LedgerFlow restarted ...")
    out, seen = await _go(fake, cfg=_cfg(max_resubmits=1))
    assert not out.ok and "lost the job" in out.error and len(seen) == 2


async def test_resuming_an_existing_job_does_not_resubmit():
    fake = FakeLedgerFlow(["REVIEW_READY"])
    out, seen = await _go(fake, existing_job_id="job-7")
    assert out.ok and out.job_id == "job-7" and seen == []
    assert all(r.method == "GET" for r in fake.requests)


async def test_a_wrong_key_is_named_in_the_error():
    fake = FakeLedgerFlow(["REVIEW_READY"])
    out, _ = await _go(fake, cfg=cc.Config(base_url="http://ledgerflow", api_key="wrong",
                                           poll_seconds=0))
    assert not out.ok and "CATEGORISER_API_KEY" in out.error


async def test_missing_configuration_fails_without_a_call():
    out = await cc.categorise(STATEMENTS, holder=None, ref="1",
                              cfg=cc.Config(base_url="", api_key=""))
    assert not out.ok and "CATEGORISER_URL" in out.error


async def test_ledgerflow_down_is_a_failure_not_a_crash():
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    out = await cc.categorise(STATEMENTS, holder=None, ref="1", cfg=_cfg(),
                              transport=httpx.MockTransport(down), sleep=_no_sleep)
    assert not out.ok and "unreachable" in out.error


async def test_no_statements_is_a_clear_failure():
    out, _ = await _go(FakeLedgerFlow(["REVIEW_READY"]), statements=[])
    assert not out.ok and "No bank statements" in out.error


def test_only_configured_types_trigger(monkeypatch):
    from app.config import settings
    from app.services import categorisation_service as svc
    monkeypatch.setattr(settings, "categoriser_enabled", True)
    assert svc.wants("bank_statement")
    assert not svc.wants("id_proof") and not svc.wants(None)
    monkeypatch.setattr(settings, "categoriser_enabled", False)
    assert not svc.wants("bank_statement")


async def test_the_summary_comes_with_the_ledger():
    out, _ = await _go(FakeLedgerFlow(["REVIEW_READY"]))
    assert out.ok and out.summary == SUMMARY and out.ledger == LEDGER


@pytest.mark.parametrize("code", [404, 500])
async def test_a_ledgerflow_without_a_summary_still_gives_the_ledger(code):
    out, _ = await _go(FakeLedgerFlow(["REVIEW_READY"], summary_status=code))
    assert out.ok and out.ledger == LEDGER and out.summary is None
