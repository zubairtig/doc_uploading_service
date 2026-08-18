# Document Portal — Integration Guide

A generic, multi-tenant document service. It stores case documents on the
server filesystem, optionally OCRs them, exposes preview URLs, and can mint
one-time client upload links. This guide is everything you need to integrate.

- **Base URL:** `http://192.168.80.52:2311`
- **Content types:** JSON for API bodies; `multipart/form-data` for uploads.
- **Auth:** every `/v1/*` call needs `Authorization: Bearer <API_KEY>`.

> Replace `<API_KEY>` throughout with the key issued for your tenant (ask ops
> for it — it is a secret). The web pages (`/staff`, `/u/...`, `/files/...`)
> are public and need no key.

---

## 1. Core concepts

| Term | Meaning |
| --- | --- |
| **Tenant** | The owning application. Identified solely by its API key. Never appears in URLs/bodies. |
| **Case / `reference`** | Your client/case identifier (e.g. `324991`). All documents are grouped under it. |
| **`client_name`** | Human name for the case; used to name the case folder on disk. |
| **`document_type`** | Which document a file is. One of the 27 slugs below, or `creditor_liaison` for call recordings, or any free-text value. |
| **Upload link** | A tokenised one-time URL a client uses to upload one document from a browser. |
| **OCR** | Optional. When requested, the file is OCR'd (OCR.space) and a JSON sidecar is stored next to it. |

### 1.1 Document types (27)
`id_proof`, `proof_of_name`, `proof_of_address`, `income_proof`, `bank_statement`,
`creditor_statement`, `creditor_document`, `creditor_report`, `credit_journal`,
`vehicle_finance`, `vehicle_valuation`, `mortgage_statement`, `tenancy_agreement`,
`boarding_letter`, `lodging_letter`, `proof_of_rent`, `council_tax_bill`,
`immigration_status`, `hmrc_webchat`, `tax_return`, `cis_slip`,
`student_loan_statement`, `pension_statement`, `employment_contract`, `gamstop`,
`criteria_check`, `other`.

Plus the special type **`creditor_liaison`** for **call recordings** (audio).

### 1.2 Storage layout (on the server)
```
<reference>__<client_name>/
  docs/
    id_proof/
      passport.png
      passport.json          # OCR result (only if ocr=true)
    bank_statement/
      statement.pdf
    ... (27 doc-type folders, created when a case is initiated)
  creditor_liaison/
    call_2026-07-28.mp3      # recordings (no OCR)
    ...
```
Filenames are sanitised (spaces/specials → `_`). Files persist on a host volume.

### 1.3 Limits
| | Max size | Allowed types |
| --- | --- | --- |
| Documents | 50 MB | PDF, DOC, DOCX, XLS, XLSX, JPG, PNG, WebP |
| Recordings (`creditor_liaison`) | 200 MB | MP3, WAV, M4A, AAC, OGG |

---

## 2. Authentication

Send the tenant key as a bearer token on every `/v1` request:
```
Authorization: Bearer <API_KEY>
```
- Missing/invalid key → `401`.
- A key scopes you to your tenant. You can only see your tenant's data; other
  tenants' records return `404` (never `403` — existence is not leaked).

### 2.1 CORS (browser uploads)
The service sends CORS headers, so a browser frontend on another origin can
call the API and POST uploads directly. Origins are configured via
`CORS_ALLOW_ORIGINS` (comma-separated; `*` by default). For a client to upload
directly to a minted link (bytes not routed through your backend), either:
- point the client at the portal-hosted page `/u/{ref}/{type}/{token}`, or
- have your frontend `fetch()` the file straight to that same URL (CORS allows it).

---

## 3. Integration flows

### Flow A — Direct upload from your app (most common)
Upload a document straight to a case, optionally OCR it:
1. `POST /v1/documents` with the file + `owner_ref` + `document_type` + `uploaded_by` (+ `ocr=true`).
2. Response contains `s3_url` (preview link) and, if OCR ran, `ocr_text` +
   `s3_url_ocr_json`.

### Flow B — Client self-service upload links
1. `POST /v1/document-requests` with `{name, reference}` → returns 27 tokenised
   upload URLs (one per document type) and pre-creates the case folders.
2. Send the relevant link(s) to your client.
3. Client opens the link in a browser, uploads → the link is spent (one-time),
   file stored, optionally previewable.
4. `GET /v1/document-requests?reference=...&uploaded_only=true` to see which
   links have documents, with their preview URLs.

### Flow C — Call recordings
Same as Flow A but `document_type=creditor_liaison` and an audio file. Stored
under `creditor_liaison/`, never OCR'd, up to 200 MB.

### Flow D — Read back
- `GET /v1/documents?owner_ref=<ref>` → list a case's documents (+ preview URLs).
- `GET /v1/documents/{id}` → one document.
- Open a document's `s3_url` (or `GET /v1/documents/{id}/file`) → inline preview.

---

## 4. Endpoint reference

### 4.1 `POST /v1/document-requests` — generate upload links
Mints one link per document type (27) for a case and pre-creates its folders.

Request (JSON):
```json
{ "name": "Theresa Topp", "reference": "324991" }
```
Response `201`:
```json
{
  "batch_id": "uuid",
  "reference": "324991",
  "client_name": "Theresa Topp",
  "links": [
    {
      "doc_type": "id_proof",
      "label": "ID Proof (Legacy)",
      "url": "http://192.168.80.52:2311/u/324991/id_proof/<token>",
      "token": "<token>",
      "status": "pending",
      "expires_at": "2026-08-04T10:46:37Z"
    }
    // ... 27 items
  ]
}
```
Links expire after 7 days or once used.

---

### 4.1a `POST /v1/document-requests/token` — mint ONE fresh link
For on-demand / repeated uploads without re-minting all 27. Returns a single
fresh tokenised link for one `(reference, doc_type)`.

Request (JSON):
```json
{ "reference": "324991", "doc_type": "id_proof", "name": "Theresa Topp" }
```
`name` is optional (falls back to the case's existing name). Response `201`:
```json
{
  "doc_type": "id_proof",
  "label": "ID Proof (Legacy)",
  "url": "http://192.168.80.52:2311/u/324991/id_proof/<token>",
  "token": "<token>",
  "status": "pending",
  "expires_at": "2026-08-04T10:46:37Z"
}
```
The client then uploads via that URL (portal-hosted page, or your frontend
POSTing the file straight to it — see CORS note in §2.1). Bytes never pass
through your backend.

---

### 4.2 `GET /v1/document-requests` — list a case's links
Query params: `reference` (required), `status` (`pending|submitted|expired`, optional),
`uploaded_only` (bool, optional).

Response `200`:
```json
{
  "reference": "324991",
  "total": 1,
  "results": [
    {
      "doc_type": "immigration_status",
      "label": "Immigration Status",
      "upload_url": "http://192.168.80.52:2311/u/324991/immigration_status/<token>",
      "token": "<token>",
      "status": "submitted",
      "expires_at": "2026-08-04T10:46:37Z",
      "document": {
        "doc_id": "uuid",
        "file_name": "image_5_.png",
        "mime_type": "image/png",
        "file_url": "http://192.168.80.52:2311/files/324991__Theresa_Topp/docs/immigration_status/image_5_.png",
        "uploaded_at": "2026-07-20T10:55:06Z"
      }
    }
  ]
}
```
`document` is `null` for links not yet uploaded.

---

### 4.3 `POST /v1/documents` — upload a document (with optional OCR)
`multipart/form-data`.

| Field | Required | Notes |
| --- | --- | --- |
| `file` | yes | the file (see limits in §1.3) |
| `owner_ref` | yes | case reference |
| `document_type` | yes | a slug from §1.1, or `creditor_liaison`, or free text |
| `uploaded_by` | yes | who uploaded |
| `ocr` | no | `true` → OCR + store JSON sidecar (default `false`; ignored for recordings) |
| `client_name` | no | folder name for the case (else taken from generated links, else `uploaded_by`) |
| `metadata` | no | JSON object string |

Response `201` (example, `ocr=true`):
```json
{
  "id": "uuid",
  "tenant_id": "uuid",
  "owner_ref": "324991",
  "document_type": "id_proof",
  "original_document_type": "id_proof",
  "file_name": "passport.png",
  "mime_type": "image/png",
  "file_size": 106236,
  "s3_url": "http://192.168.80.52:2311/files/324991__Theresa_Topp/docs/id_proof/passport.png",
  "s3_url_ocr_json": "http://192.168.80.52:2311/files/324991__Theresa_Topp/docs/id_proof/passport.json",
  "ocr_status": "done",
  "ocr_text": "…extracted text…",
  "ocr_error": null,
  "group_tag": null,
  "metadata": {},
  "uploaded_by": "Theresa Topp",
  "created_at": "2026-07-28T10:00:00Z",
  "updated_at": "2026-07-28T10:00:00Z"
}
```
Re-uploading the same `(owner_ref, file_name, document_type)` overwrites in place.

**OCR status values:** `not_requested` (ocr not asked), `done`, `failed`
(file stored anyway; see `ocr_error`), `pending` (async only — not used here).

---

### 4.4 `GET /v1/documents` — list a case's documents
Query params (≥1 required): `owner_ref`, `document_type`, `group_tag`, `ocr_status`.
Also `limit` (1–200, default 50), `offset` (default 0).

Response `200`:
```json
{ "results": [ { /* document object, same shape as §4.3 */ } ], "total": 1, "next_offset": null }
```

### 4.5 `GET /v1/documents/{id}` — one document
Returns the document object (`404` if not found for your tenant).

### 4.6 `GET /v1/documents/{id}/file` — stream / preview
Streams the file inline with its real content type. Requires the bearer key.
Public alternative: open the document's `s3_url` (`/files/...`) directly.

### 4.7 `PATCH /v1/documents/{id}` — update
JSON body; editable fields only: `document_type`, `metadata`, `uploaded_by`.
Optional `?metadata_mode=merge|replace` (default `merge`). Returns the document.

### 4.7a `DELETE /v1/documents/{id}` — delete a document
Removes the stored file, its OCR JSON sidecar (if any), and the DB record.
Returns `204`. Any upload link that pointed at it is unlinked.
```bash
curl -s -X DELETE "http://192.168.80.52:2311/v1/documents/<doc_id>" \
  -H "Authorization: Bearer <API_KEY>" -w "%{http_code}"
```

### 4.8 `GET /v1/documents/{id}/text` — OCR text status
```json
{ "ocr_status": "done", "ocr_text": "…", "ocr_error": null }
```

---

### 4.9 Public pages (browser, no auth)
| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/staff` | GET | Search form → generate links |
| `/staff/documents?reference=<ref>` | GET | View a case's uploaded docs + preview buttons |
| `/u/{reference}/{doc_type}/{token}` | GET | Client upload page (live / submitted / expired) |
| `/u/{reference}/{doc_type}/{token}` | POST (multipart `file`) | Submit an upload → JSON `{ok, message, doc_id}`; one-time |
| `/files/{key}` | GET | Public inline file preview |

### 4.10 Health (no auth)
`GET /health` (liveness), `GET /health/db`, `GET /ready`.

---

## 5. Errors

| Status | Meaning |
| --- | --- |
| `400` | Bad request (e.g. no filter on list; invalid metadata JSON) |
| `401` | Missing/invalid API key |
| `404` | Not found (or not yours — existence not leaked) |
| `409` | Upload link already used/expired |
| `413` | File too large (see §1.3) |
| `415` | Unsupported file type (see §1.3) |
| `422` | Validation error |

Error body shape: `{ "detail": "<message>" }`.

---

## 6. Worked examples

### Upload a document + OCR
```bash
curl -s -X POST "http://192.168.80.52:2311/v1/documents" \
  -H "Authorization: Bearer <API_KEY>" \
  -F "file=@passport.png;type=image/png" \
  -F "owner_ref=324991" \
  -F "document_type=id_proof" \
  -F "uploaded_by=Theresa Topp" \
  -F "ocr=true"
```

### Upload a call recording
```bash
curl -s -X POST "http://192.168.80.52:2311/v1/documents" \
  -H "Authorization: Bearer <API_KEY>" \
  -F "file=@call.mp3;type=audio/mpeg" \
  -F "owner_ref=324991" \
  -F "document_type=creditor_liaison" \
  -F "uploaded_by=Agent Smith"
```

### Generate client upload links
```bash
curl -s -X POST "http://192.168.80.52:2311/v1/document-requests" \
  -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  -d '{"name":"Theresa Topp","reference":"324991"}'
```

### List a case's uploaded documents
```bash
curl -s "http://192.168.80.52:2311/v1/documents?owner_ref=324991" \
  -H "Authorization: Bearer <API_KEY>"
```

### Python (requests)
```python
import requests

BASE = "http://192.168.80.52:2311"
HEAD = {"Authorization": "Bearer <API_KEY>"}

# Upload + OCR
with open("passport.png", "rb") as f:
    r = requests.post(
        f"{BASE}/v1/documents",
        headers=HEAD,
        files={"file": ("passport.png", f, "image/png")},
        data={
            "owner_ref": "324991",
            "document_type": "id_proof",
            "uploaded_by": "Theresa Topp",
            "ocr": "true",
        },
    )
doc = r.json()
print(doc["s3_url"], doc["ocr_status"], doc["s3_url_ocr_json"])

# List a case
docs = requests.get(f"{BASE}/v1/documents", headers=HEAD,
                    params={"owner_ref": "324991"}).json()
for d in docs["results"]:
    print(d["document_type"], d["s3_url"])
```

---

## 7. Behaviour notes for integrators

- **Idempotent uploads:** same `(owner_ref, file_name, document_type)` overwrites
  the existing document (and its OCR JSON) rather than duplicating.
- **OCR is synchronous:** the upload response already contains `ocr_text` and
  `s3_url_ocr_json`. Large/multi-page files make the request take longer.
- **OCR failure never loses the file:** the document is stored with
  `ocr_status: "failed"` and an `ocr_error`.
- **Recordings never OCR:** `document_type=creditor_liaison` ignores `ocr`.
- **OCR overlay included:** the stored OCR JSON contains word x/y positions
  (`ParsedResults[].TextOverlay`), so table docs (payslips) can be parsed by
  coordinates without re-OCRing.
- **Preview URLs** (`s3_url`, `file_url`) are directly openable in a browser and
  render inline (correct content type). Filenames with spaces are sanitised.
- **One-time links:** a client link works once; re-use returns `409`; it also
  expires 7 days after creation.
- **Multi-tenant:** your key sees only your tenant's data.
