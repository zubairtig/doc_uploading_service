"""
Storage key layout for the collection portal.

A case gets one folder named ``<reference>__<name>``; inside it, one folder per
document type (27 of them); inside each, the uploaded file and — when OCR is
requested — a JSON sidecar with the same base name:

    <reference>__<name>/
        proof_of_id/
            passport.png
            passport.json          # OCR result (if ocr=true)
        bank_statement/
            statement.pdf
            statement.json
        ...
"""
import re


def slugify(value: str | None, fallback: str = "unknown") -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", (value or "").strip()).strip("_")
    return s or fallback


def sanitize_filename(name: str | None) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "file").strip()).strip("._")
    return base or "file"


# Layout within a case folder:
#   <case>/docs/<doc_type>/...        normal documents (+ .json if OCR)
#   <case>/creditor_liaison/...       call recordings (sibling of docs/)
DOCS_DIR = "docs"
RECORDINGS_TYPE = "creditor_liaison"


def case_folder(reference: str, client_name: str | None) -> str:
    return f"{slugify(reference)}__{slugify(client_name)}"


def folder_for(reference: str, client_name: str | None, doc_type: str) -> str:
    """The folder a given doc_type lives in, inside the case folder."""
    case = case_folder(reference, client_name)
    if doc_type == RECORDINGS_TYPE:
        return f"{case}/{RECORDINGS_TYPE}"
    return f"{case}/{DOCS_DIR}/{doc_type}"


def doc_key(reference: str, client_name: str | None, doc_type: str, file_name: str) -> str:
    return f"{folder_for(reference, client_name, doc_type)}/{sanitize_filename(file_name)}"


def json_key(reference: str, client_name: str | None, doc_type: str, file_name: str) -> str:
    fn = sanitize_filename(file_name)
    stem = fn.rsplit(".", 1)[0] if "." in fn else fn
    return f"{folder_for(reference, client_name, doc_type)}/{stem}.json"
