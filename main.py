"""MedLens: a small, privacy-conscious lab report organizer for Vercel."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv
from bson import ObjectId
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

MAX_UPLOAD = 15 * 1024 * 1024
ALLOWED = {"application/pdf", "image/png", "image/jpeg"}
app = FastAPI(title="MedLens", description="Organize and review lab reports. Not for diagnosis.")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def db():
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise HTTPException(503, "MongoDB is not configured. Set MONGODB_URI to a MongoDB Atlas connection string.")
    try:
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
        client.admin.command("ping")
        database = client[os.getenv("MONGODB_DATABASE", "medlens")]
        database.reports.create_index([("workspace", 1), ("sha256", 1)], unique=True)
        database.reminders.create_index([("workspace", 1), ("due", 1)])
        database.upload_sessions.create_index("expiresAt", expireAfterSeconds=0)
        database.upload_chunks.create_index([("uploadId", 1), ("index", 1)], unique=True)
        database.upload_chunks.create_index("expiresAt", expireAfterSeconds=0)
        return database
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "Could not connect to MongoDB. Check MONGODB_URI and Atlas network access.") from exc


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = __import__("hashlib").scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "." + digest.hex()


def check_password(password: str) -> bool:
    configured = os.getenv("WORKSPACE_PASSWORD", "")
    encoded = os.getenv("WORKSPACE_PASSWORD_HASH", "")
    if not configured and not encoded:
        raise HTTPException(503, "Workspace sign-in is not configured. Set WORKSPACE_PASSWORD before deployment.")
    if encoded:
        try:
            salt_hex, expected = encoded.split(".", 1)
            actual = password_hash(password, bytes.fromhex(salt_hex)).split(".", 1)[1]
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(password, configured)


def sign(value: str) -> str:
    key = os.getenv("SESSION_SECRET", "")
    if len(key) < 32:
        raise HTTPException(503, "Set SESSION_SECRET to a random value of at least 32 characters.")
    sig = hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()
    return value + "." + sig


def authorized(request: Request) -> str:
    token = request.cookies.get("medlens_session", "")
    try:
        value, signature = token.rsplit(".", 1)
        expected = hmac.new(os.getenv("SESSION_SECRET", "").encode(), value.encode(), hashlib.sha256).hexdigest()
        expiry, workspace = value.split(":", 1)
        if int(expiry) < int(time.time()) or not hmac.compare_digest(expected, signature):
            raise ValueError()
        return workspace
    except (ValueError, TypeError):
        raise HTTPException(401, "Please sign in to your workspace.")


@app.get("/api/health")
def health():
    return {"ok": True, "storage": "MongoDB Atlas" if os.getenv("MONGODB_URI") else "not configured"}


@app.get("/api/config")
def config():
    return {"bedrockAvailable": bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY") and os.getenv("BEDROCK_MODEL_ID")), "maxUploadBytes": MAX_UPLOAD}


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if not check_password(str(body.get("password", ""))):
        raise HTTPException(401, "The workspace password is incorrect.")
    workspace = "default"
    token = sign(f"{int(time.time()) + 60 * 60 * 12}:{workspace}")
    response = JSONResponse({"ok": True})
    response.set_cookie("medlens_session", token, httponly=True, secure=os.getenv("VERCEL", "").lower() == "1" or os.getenv("APP_ENV") == "production", samesite="strict", max_age=60 * 60 * 12, path="/")
    return response


@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("medlens_session", path="/")
    return response


@app.get("/api/session")
def session(request: Request):
    authorized(request)
    return {"authenticated": True}


def range_status(value: Any, ref_low: Any, ref_high: Any) -> str:
    try:
        n = float(value)
        lo = float(ref_low) if ref_low not in (None, "") else None
        hi = float(ref_high) if ref_high not in (None, "") else None
        if lo is not None and n < lo: return "low"
        if hi is not None and n > hi: return "high"
        if lo is not None or hi is not None: return "in_range"
    except (ValueError, TypeError): pass
    return "unknown"


def extract_text(raw: bytes, kind: str) -> tuple[str, str]:
    """Extract PDF text; try local Tesseract for scanned PDFs/images when installed."""
    text = ""
    if kind == "application/pdf":
        try:
            from pypdf import PdfReader
            text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages)
        except Exception:
            text = ""
    if not text.strip():
        try:
            import pytesseract
            from PIL import Image
            if kind == "application/pdf":
                from pdf2image import convert_from_bytes
                pages = convert_from_bytes(raw, first_page=1, last_page=5, dpi=180)
            else:
                pages = [Image.open(io.BytesIO(raw))]
            text = "\n".join(pytesseract.image_to_string(page) for page in pages)
            return text[:100_000], "ocr" if text.strip() else "unavailable"
        except Exception:
            return text[:100_000], "digital_pdf" if text.strip() else "unavailable"
    return text[:100_000], "digital_pdf"


def suggest_results(text: str) -> list[dict[str, Any]]:
    """Conservative heuristic extraction. Every suggestion must be verified by the user."""
    results = []
    # Common tabular format: Test name, numeric result, optional unit, optional a-b reference range.
    pattern = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /().,+-]{1,55}?)\s{1,}([<>]?[+-]?\d+(?:\.\d+)?)\s*([A-Za-zµμ/%][A-Za-z0-9µμ/%.^*-]*)?\s*(?:\(?\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*\)?)?\s*$")
    for line in text.splitlines():
        m = pattern.match(line)
        if not m: continue
        name, val, unit, low, high = m.groups()
        if re.search(r"\b(date|phone|page|age|zip|invoice)\b", name, re.I): continue
        results.append({"test": name.strip(), "value": val, "unit": unit or "", "refLow": low or "", "refHigh": high or "", "confidence": 0.55 if low or high else 0.35})
        if len(results) >= 100: break
    return results


@app.post("/api/extract")
async def extract(request: Request, file: UploadFile = File(...)):
    authorized(request)
    if file.content_type not in ALLOWED:
        raise HTTPException(415, "Choose a PDF, PNG, JPG, or JPEG report.")
    raw = await file.read(MAX_UPLOAD + 1)
    if not raw: raise HTTPException(400, "The selected file is empty.")
    if len(raw) > MAX_UPLOAD: raise HTTPException(413, "The file exceeds the 15 MB limit.")
    # Verify actual file signatures rather than trusting the browser-provided MIME type.
    if file.content_type == "application/pdf" and not raw.startswith(b"%PDF-"):
        raise HTTPException(415, "The file does not appear to be a valid PDF.")
    if file.content_type == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(415, "The file does not appear to be a valid PNG image.")
    if file.content_type == "image/jpeg" and not raw.startswith(b"\xff\xd8\xff"):
        raise HTTPException(415, "The file does not appear to be a valid JPEG image.")
    workspace = authorized(request)
    digest = hashlib.sha256(raw).hexdigest()
    duplicate = db().reports.find_one({"workspace": workspace, "sha256": digest}, {"_id": 1})
    text, method = extract_text(raw, file.content_type)
    return {"sha256": digest, "duplicate": str(duplicate["_id"]) if duplicate else None, "filename": file.filename or "Lab report", "extraction": method, "sourceText": text, "results": suggest_results(text)}


@app.post("/api/upload/start")
async def start_upload(request: Request):
    """Create a short-lived upload session. Chunking keeps Vercel requests below 4.5 MB."""
    workspace = authorized(request)
    body = await request.json()
    filename = str(body.get("filename", "Lab report"))[:200]
    content_type = str(body.get("contentType", ""))
    size = int(body.get("size", 0))
    if content_type not in ALLOWED: raise HTTPException(415, "Choose a PDF, PNG, JPG, or JPEG report.")
    if size < 1 or size > MAX_UPLOAD: raise HTTPException(413, "Choose a non-empty file up to 15 MB.")
    upload_id = secrets.token_hex(20)
    database = db()
    database.upload_sessions.insert_one({"_id": upload_id, "workspace": workspace, "filename": filename, "contentType": content_type, "size": size, "expiresAt": datetime.fromtimestamp(time.time() + 3600, timezone.utc)})
    return {"uploadId": upload_id, "chunkSize": 3 * 1024 * 1024}


@app.post("/api/upload/{upload_id}/chunk")
async def upload_chunk(upload_id: str, request: Request, index: int = Form(...), chunk: UploadFile = File(...)):
    workspace = authorized(request)
    database = db()
    session = database.upload_sessions.find_one({"_id": upload_id, "workspace": workspace, "expiresAt": {"$gt": utcnow()}})
    if not session: raise HTTPException(404, "Upload session expired. Choose the file again.")
    if index < 0 or index >= 6: raise HTTPException(400, "Invalid upload chunk.")
    raw = await chunk.read(3 * 1024 * 1024 + 1)
    if not raw or len(raw) > 3 * 1024 * 1024: raise HTTPException(413, "Upload chunk is empty or too large.")
    database.upload_chunks.update_one({"uploadId": upload_id, "index": index}, {"$set": {"uploadId": upload_id, "workspace": workspace, "index": index, "data": raw, "expiresAt": session["expiresAt"]}}, upsert=True)
    return {"received": index}


@app.post("/api/upload/{upload_id}/finish")
def finish_upload(upload_id: str, request: Request):
    workspace = authorized(request)
    database = db()
    session = database.upload_sessions.find_one({"_id": upload_id, "workspace": workspace, "expiresAt": {"$gt": utcnow()}})
    if not session: raise HTTPException(404, "Upload session expired. Choose the file again.")
    chunks = list(database.upload_chunks.find({"uploadId": upload_id, "workspace": workspace}).sort("index", 1))
    raw = b"".join(chunk["data"] for chunk in chunks)
    database.upload_chunks.delete_many({"uploadId": upload_id, "workspace": workspace})
    database.upload_sessions.delete_one({"_id": upload_id, "workspace": workspace})
    if len(raw) != session["size"]: raise HTTPException(400, "Upload was incomplete. Please choose the file again.")
    kind = session["contentType"]
    if kind == "application/pdf" and not raw.startswith(b"%PDF-"): raise HTTPException(415, "The file does not appear to be a valid PDF.")
    if kind == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"): raise HTTPException(415, "The file does not appear to be a valid PNG image.")
    if kind == "image/jpeg" and not raw.startswith(b"\xff\xd8\xff"): raise HTTPException(415, "The file does not appear to be a valid JPEG image.")
    digest = hashlib.sha256(raw).hexdigest()
    duplicate = database.reports.find_one({"workspace": workspace, "sha256": digest}, {"_id": 1})
    source_text, method = extract_text(raw, kind)
    return {"sha256": digest, "duplicate": str(duplicate["_id"]) if duplicate else None, "filename": session["filename"], "extraction": method, "sourceText": source_text, "results": suggest_results(source_text)}


class Result(BaseModel):
    test: str = Field(min_length=1, max_length=120)
    value: str = Field(max_length=80)
    unit: str = Field(default="", max_length=40)
    refLow: str = Field(default="", max_length=40)
    refHigh: str = Field(default="", max_length=40)
    confidence: float | None = None


class ReportInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    lab: str = Field(default="", max_length=160)
    reportType: str = Field(default="", max_length=100)
    reportDate: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    filename: str = Field(default="", max_length=200)
    sourceText: str = Field(default="", max_length=100000)
    extraction: str = "manual"
    results: list[Result] = Field(default_factory=list, max_length=300)


def public_report(doc: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(doc["_id"]), "title": doc.get("title", ""), "lab": doc.get("lab", ""), "reportType": doc.get("reportType", ""), "reportDate": doc.get("reportDate", ""), "sha256": doc.get("sha256", ""), "filename": doc.get("filename", ""), "sourceText": doc.get("sourceText", ""), "extraction": doc.get("extraction", "manual"), "createdAt": doc.get("createdAt"), "results": doc.get("results", [])}


@app.get("/api/reports")
def reports(request: Request, q: str = "", from_date: str = "", to_date: str = ""):
    workspace = authorized(request)
    query: dict[str, Any] = {"workspace": workspace}
    if q:
        safe = re.escape(q[:100])
        query["$or"] = [{key: {"$regex": safe, "$options": "i"}} for key in ("title", "lab", "reportType")]
    date_range: dict[str, str] = {}
    if from_date: date_range["$gte"] = from_date
    if to_date: date_range["$lte"] = to_date
    if date_range: query["reportDate"] = date_range
    docs = db().reports.find(query).sort([("reportDate", -1), ("createdAt", -1)]).limit(500)
    return [public_report(doc) for doc in docs]


@app.post("/api/reports")
def save_report(data: ReportInput, request: Request):
    workspace = authorized(request)
    try: date.fromisoformat(data.reportDate)
    except ValueError: raise HTTPException(422, "Enter a valid report date.")
    doc = data.model_dump()
    doc["workspace"] = workspace
    doc["createdAt"] = utcnow()
    doc["results"] = [{**r, "status": range_status(r["value"], r["refLow"], r["refHigh"])} for r in doc["results"]]
    try:
        inserted = db().reports.insert_one(doc)
    except Exception as exc:
        if "duplicate" in str(exc).lower(): raise HTTPException(409, "This report file has already been saved.")
        raise
    doc["_id"] = inserted.inserted_id
    return public_report(doc)


@app.delete("/api/reports/{report_id}")
def delete_report(report_id: str, request: Request):
    workspace = authorized(request)
    if not ObjectId.is_valid(report_id): raise HTTPException(404, "Report not found.")
    result = db().reports.delete_one({"_id": ObjectId(report_id), "workspace": workspace})
    if not result.deleted_count: raise HTTPException(404, "Report not found.")
    return {"deleted": True}


@app.delete("/api/data")
def delete_all(request: Request):
    workspace = authorized(request)
    database = db()
    reports_count = database.reports.delete_many({"workspace": workspace}).deleted_count
    reminders_count = database.reminders.delete_many({"workspace": workspace}).deleted_count
    return {"deletedReports": reports_count, "deletedReminders": reminders_count}


class ReminderInput(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    due: str
    kind: str = Field(default="Follow-up", max_length=50)
    notes: str = Field(default="", max_length=1000)


@app.get("/api/reminders")
def reminders(request: Request):
    workspace = authorized(request)
    return [{"id": str(d["_id"]), "title": d["title"], "due": d["due"], "kind": d["kind"], "notes": d["notes"], "done": d["done"]} for d in db().reminders.find({"workspace": workspace}).sort("due", 1).limit(500)]


@app.post("/api/reminders")
def add_reminder(data: ReminderInput, request: Request):
    workspace = authorized(request)
    try: date.fromisoformat(data.due)
    except ValueError: raise HTTPException(422, "Enter a valid reminder date.")
    doc = {**data.model_dump(), "workspace": workspace, "done": False, "createdAt": utcnow()}
    doc["_id"] = db().reminders.insert_one(doc).inserted_id
    return {"id": str(doc["_id"]), **{k: doc[k] for k in ("title", "due", "kind", "notes", "done")}}


@app.patch("/api/reminders/{reminder_id}")
async def update_reminder(reminder_id: str, request: Request):
    workspace = authorized(request)
    if not ObjectId.is_valid(reminder_id): raise HTTPException(404, "Reminder not found.")
    body = await request.json()
    result = db().reminders.update_one({"_id": ObjectId(reminder_id), "workspace": workspace}, {"$set": {"done": bool(body.get("done"))}})
    if not result.matched_count: raise HTTPException(404, "Reminder not found.")
    return {"updated": True}


@app.delete("/api/reminders/{reminder_id}")
def delete_reminder(reminder_id: str, request: Request):
    workspace = authorized(request)
    if not ObjectId.is_valid(reminder_id): raise HTTPException(404, "Reminder not found.")
    db().reminders.delete_one({"_id": ObjectId(reminder_id), "workspace": workspace})
    return {"deleted": True}


@app.get("/api/export/json")
def export_json(request: Request):
    workspace = authorized(request)
    data = {"exportedAt": utcnow().isoformat(), "reports": [public_report(x) for x in db().reports.find({"workspace": workspace})], "reminders": reminders(request)}
    return JSONResponse(jsonable_encoder(data), headers={"Content-Disposition": "attachment; filename=medlens-backup.json"})


@app.get("/api/export/csv")
def export_csv(request: Request):
    workspace = authorized(request)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["report_date", "report", "laboratory", "test", "value", "unit", "reference_low", "reference_high", "range_flag"])
    for report in db().reports.find({"workspace": workspace}).sort("reportDate", -1):
        for result in report.get("results", []):
            writer.writerow([report.get("reportDate"), report.get("title"), report.get("lab"), result.get("test"), result.get("value"), result.get("unit"), result.get("refLow"), result.get("refHigh"), result.get("status")])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=medlens-results.csv"})


@app.post("/api/bedrock/{report_id}")
async def bedrock_summary(report_id: str, request: Request):
    workspace = authorized(request)
    body = await request.json()
    if body.get("consent") is not True:
        raise HTTPException(400, "Explicit consent is required before sending report results to Amazon Bedrock.")
    if not all(os.getenv(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "BEDROCK_MODEL_ID")):
        raise HTTPException(503, "Optional Bedrock summaries are not configured.")
    if not ObjectId.is_valid(report_id): raise HTTPException(404, "Report not found.")
    report = db().reports.find_one({"_id": ObjectId(report_id), "workspace": workspace})
    if not report: raise HTTPException(404, "Report not found.")
    try:
        import boto3
        client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
        values = [{k: r.get(k) for k in ("test", "value", "unit", "refLow", "refHigh")} for r in report.get("results", [])]
        prompt = "Summarize these user-verified laboratory report entries in plain language. Do not diagnose, infer diseases, recommend treatment, or replace a clinician. State that ranges are report-specific. Data: " + json.dumps(values, ensure_ascii=False)
        result = client.converse(modelId=os.environ["BEDROCK_MODEL_ID"], messages=[{"role": "user", "content": [{"text": prompt}]}], inferenceConfig={"maxTokens": 500, "temperature": 0.2})
        return {"summary": result["output"]["message"]["content"][0]["text"]}
    except Exception as exc:
        raise HTTPException(502, "The optional Bedrock summary could not be generated.") from exc


@app.get("/", response_class=HTMLResponse)
def index():
    try:
        with open(os.path.join(os.path.dirname(__file__), "static", "index.html"), encoding="utf-8") as f: return f.read()
    except FileNotFoundError: raise HTTPException(500, "MedLens frontend is missing.")


@app.get("/{path:path}", response_class=HTMLResponse)
def spa_fallback(path: str):
    if path.startswith("api/"): raise HTTPException(404, "Not found")
    return index()
