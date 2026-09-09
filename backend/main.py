from dataclasses import asdict
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent / ".env")

import auth
import rag

app = FastAPI(title="DocMind API", version="0.1.0")

default_origins = "http://localhost:5173,http://127.0.0.1:5173"
extra = os.environ.get("CORS_ORIGINS", default_origins)
allow_origins = [o.strip() for o in extra.split(",") if o.strip()]
if os.environ.get("CORS_ALLOW_ALL", "").lower() in {"1", "true", "yes"}:
    allow_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str


class GoogleLoginRequest(BaseModel):
    credential: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/auth/config")
def auth_config():
    return {
        "demo": auth.is_demo(),
        "googleClientId": auth.google_client_id() or None,
    }


@app.post("/auth/google")
def auth_google(req: GoogleLoginRequest):
    token, email = auth.login_with_google(req.credential)
    return {"token": token, "username": email}


@app.get("/auth/me")
def auth_me(user: str = Depends(auth.current_user)):
    return {"username": user, "demo": auth.is_demo()}


@app.get("/documents")
def get_documents(_: str = Depends(auth.current_user)):
    return [asdict(d) for d in rag.list_documents()]


@app.post("/upload")
async def upload(file: UploadFile = File(...), _: str = Depends(auth.current_user)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported right now.")
    pdf_bytes = await file.read()
    try:
        doc = rag.add_document(file.filename, pdf_bytes)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return asdict(doc)


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str, _: str = Depends(auth.current_user)):
    try:
        rag.delete_document(doc_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"deleted": doc_id}


@app.post("/ask")
def ask(req: AskRequest, _: str = Depends(auth.current_user)):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    try:
        return rag.answer_question(req.question)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        target = FRONTEND_DIST / path
        if path and target.is_file():
            return FileResponse(target)
        return FileResponse(FRONTEND_DIST / "index.html")
