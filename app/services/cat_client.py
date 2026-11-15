"""HTTP client for CAT (the case assessment tool): hand it a case's documents,
have it read them, and get the income it worked out back.

Pure: no database, so it can be tested with a mock transport
(tests/test_cat_client.py). The database side is cat_sync_service.py.

CAT's endpoints, as Katie (AI_Call_Agent/backend/services/cat_service.py)
already uses them against CAT:

  GET  /api/auth/me/                                sets the csrftoken cookie
  POST /api/auth/login/                             email + password -> session cookie
  POST /api/applications/{case}/proof/upload/       multipart: file, document_type, notes -> 201
  POST /api/applications/{case}/proof/recalculate/  -> 202 queued; 409 while one runs
  GET  /api/applications/{case}/                    the case, its documents, what was read
  GET  /api/applications/{case}/proof/              the evidence ledger ("Proof of Income" ...)

Sign-in: a Bearer key when CAT issues one, else a CAT user's email and
password (CAT's screen API; for testing). Every call returns a CatResult and
never raises, so nothing here can fail a client's upload.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import httpx


@dataclass
class Config:
    base_url: str
    api_key: str = ""
    email: str = ""
    password: str = ""
    request_timeout_seconds: float = 60.0

    @property
    def key(self) -> str:
        return re.sub(r"^bearer\s+", "", (self.api_key or "").strip(), flags=re.I)

    def problem(self) -> str | None:
        """None when CAT can be called, else why not."""
        if not (self.base_url or "").strip():
            return "CAT_URL is not set."
        if self.key or (self.email and self.password):
            return None
        return "No CAT sign-in: set CAT_API_KEY, or CAT_EMAIL and CAT_PASSWORD."


@dataclass
class CatResult:
    ok: bool
    status: int | None = None
    error: str | None = None
    data: dict = field(default_factory=dict)


def case_for(reference: str, case_map: str) -> str:
    """The case number CAT knows this reference by ("ref:cat,ref:cat")."""
    reference = str(reference or "").strip()
    for pair in (case_map or "").split(","):
        ours, _, theirs = pair.partition(":")
        if ours.strip() == reference and theirs.strip():
            return theirs.strip()
    return reference


class CatClient:
    """One signed-in conversation with CAT. `transport` is for tests."""

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        # CAT answers a case reference with a 308 to its own application id;
        # 308 keeps the method and body, so uploads follow it too.
        self._http = httpx.AsyncClient(
            base_url=self.cfg.base_url.strip().rstrip("/"),
            timeout=self.cfg.request_timeout_seconds, trust_env=False,
            transport=self._transport, follow_redirects=True)
        return self

    async def __aexit__(self, *exc):
        await self._http.aclose()

    def _headers(self) -> dict:
        if self.cfg.key:
            return {"Authorization": f"Bearer {self.cfg.key}"}
        return {"X-CSRFToken": self._http.cookies.get("csrftoken", ""),
                "Referer": self.cfg.base_url.rstrip("/") + "/"}

    async def _post(self, path: str, **kw) -> httpx.Response:
        """A POST, waiting once if CAT says too many requests (429)."""
        for attempt in range(2):
            r = await self._http.post(path, headers=self._headers(), **kw)
            if r.status_code != 429 or attempt:
                return r
            await asyncio.sleep(min(int(r.headers.get("Retry-After") or 10), 60))
        return r

    async def sign_in(self) -> CatResult:
        problem = self.cfg.problem()
        if problem:
            return CatResult(False, error=problem)
        if self.cfg.key:
            return CatResult(True)
        try:
            await self._http.get("/api/auth/me/")
            if not self._http.cookies.get("csrftoken"):
                return CatResult(False, error="CAT gave no CSRF cookie.")
            r = await self._post("/api/auth/login/",
                                 json={"email": self.cfg.email, "password": self.cfg.password})
        except httpx.HTTPError as exc:
            return CatResult(False, error=f"CAT could not be reached: {exc}")
        if r.status_code != 200:
            return CatResult(False, r.status_code,
                             f"CAT sign-in failed: HTTP {r.status_code} {r.text[:160]}")
        return CatResult(True, r.status_code)

    async def upload(self, cat_case: str, filename: str, content: bytes, content_type: str,
                     document_type: str, notes: str = "") -> CatResult:
        try:
            r = await self._post(
                f"/api/applications/{cat_case}/proof/upload/",
                data={"document_type": document_type, "notes": notes},
                files={"file": (filename, content, content_type or "application/octet-stream")})
        except httpx.HTTPError as exc:
            return CatResult(False, error=f"CAT could not be reached: {exc}")
        if r.status_code not in (200, 201):
            return CatResult(False, r.status_code,
                             f"CAT refused the upload: HTTP {r.status_code} {r.text[:200]}")
        try:
            body = r.json()
        except ValueError:
            body = {}
        return CatResult(True, r.status_code,
                         data=body if isinstance(body, dict) else {"reply": body})

    async def recalculate(self, cat_case: str) -> CatResult:
        """ok with data['state'] 'started', or 'busy' when one already runs (409)."""
        try:
            r = await self._post(f"/api/applications/{cat_case}/proof/recalculate/")
        except httpx.HTTPError as exc:
            return CatResult(False, error=f"CAT could not be reached: {exc}")
        if r.status_code == 409:
            return CatResult(True, 409, data={"state": "busy"})
        if r.status_code not in (200, 202):
            return CatResult(False, r.status_code,
                             f"CAT did not start the read: HTTP {r.status_code} {r.text[:200]}")
        return CatResult(True, r.status_code, data={"state": "started"})

    async def _get(self, path: str) -> CatResult:
        try:
            r = await self._http.get(path, headers=self._headers() if self.cfg.key else None)
        except httpx.HTTPError as exc:
            return CatResult(False, error=f"CAT could not be reached: {exc}")
        if r.status_code != 200:
            return CatResult(False, r.status_code, f"CAT: HTTP {r.status_code} {r.text[:160]}")
        try:
            return CatResult(True, 200, data=r.json())
        except ValueError:
            return CatResult(False, 200, "CAT sent something that is not JSON.")

    async def application(self, cat_case: str) -> CatResult:
        return await self._get(f"/api/applications/{cat_case}/")

    async def proof(self, cat_case: str) -> CatResult:
        return await self._get(f"/api/applications/{cat_case}/proof/")


# ── reading what CAT worked out ──────────────────────────────────────────────

def find_document(application: dict, filename: str) -> dict | None:
    """Our upload among the case's documents, by the unique name we gave it."""
    stem = filename.rsplit(".", 1)[0].lower()
    for doc in application.get("documents") or []:
        names = [str(doc.get(k) or "") for k in ("document_name", "name", "file_name")]
        names.append(str(doc.get("s3_url") or "").rsplit("/", 1)[-1].split("?", 1)[0])
        names.append(str((doc.get("extracted_data") or {}).get("document_name") or ""))
        if any(n and n.rsplit(".", 1)[0].lower() == stem for n in names):
            return doc
    return None


_DOC_FIELDS = ("employer_name", "employer", "gross_pay", "net_pay", "pay_period", "pay_date",
               "tax_code", "tax_paid", "issues")

#: CAT's processing_status values that mean it is done with a document.
READ_DONE = ("processed",)
READ_FAILED = ("failed", "error")


def document_reading(doc: dict | None) -> dict:
    """What CAT read from one document: its state and the figures it found."""
    if not doc:
        return {"state": "not_found"}
    data = doc.get("extracted_data") or {}
    out = {"state": str(doc.get("processing_status") or "pending").lower(),
           "document_type": doc.get("document_type")}
    if isinstance(data, dict):
        for key in _DOC_FIELDS:
            if data.get(key) not in (None, "", []):
                out[key] = data[key]
    return out


def income_summary(proof: dict) -> list[dict]:
    """CAT's "Proof of Income" rows: what Aryza says, what the documents and
    bank show, and CAT's verdict on each."""
    rows = []
    for row in proof.get("evidence_ledger") or []:
        if row.get("group") != "Proof of Income":
            continue
        rows.append({k: row.get(k) for k in (
            "label", "status", "crm_value", "doc_value", "bank_value", "found_value",
            "variance", "pay_frequency", "narrative")})
    return rows
