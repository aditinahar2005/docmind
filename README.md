# DocMind

RAG over PDFs: ingest → embed → retrieve → generate, with page-level citations.

Upload a text PDF, ask a question, get an answer grounded in retrieved chunks.
The UI turns `[Source N]` into clickable tabs that show the source passage and
page. Stack: React, FastAPI, FastEmbed (ONNX MiniLM), FAISS, BM25, Groq (or
OpenAI).

## Architecture

```
PDF ── pypdf ── overlapping chunks (~800 chars, 150 overlap)
                    │
                    ▼
              MiniLM embeddings (384-d, L2-normalized)
                    │
                    ▼
              FAISS IndexFlatIP          BM25 over the same chunks
                    │                              │
                    └──────── RRF (k = 60) ────────┘
                                    │
                                    ▼
                           top-k chunks in the prompt
                                    │
                                    ▼
                           Groq / OpenAI  →  cited answer
```

Core logic lives in [`backend/rag.py`](backend/rag.py). HTTP surface is
[`backend/main.py`](backend/main.py).

| Stage | Implementation | Notes |
| --- | --- | --- |
| Extract | `pypdf` | Text layer only; scanned/image PDFs fail closed |
| Chunk | Character windows, overlap 150 | Page number kept on each chunk |
| Embed | FastEmbed `all-MiniLM-L6-v2` (ONNX) | Same model family as sentence-transformers MiniLM; no PyTorch at runtime |
| Vector index | FAISS `IndexFlatIP` | Cosine via inner product on normalized vectors; exact search |
| Lexical | Okapi BM25 | Catches identifiers / names embeddings miss |
| Fuse | Reciprocal Rank Fusion, `k=60` | Same combiner Azure AI Search uses for hybrid rank |
| Generate | Groq `openai/gpt-oss-20b` (default) or OpenAI | Prompt forbids answering outside the sources |
| Persist | `backend/data/index.faiss` + `meta.json` | Survives local process restart |

The retrieve → prompt → generate loop does not depend on Groq vs Azure OpenAI
vs Azure AI Search. Swapping those is a storage/client change, not a pipeline
rewrite.

## Design choices

**Hybrid retrieval.** Dense search fails on rare tokens (error codes, proper
names). BM25 fails on paraphrase. RRF merges ranked lists without calibrating
score scales.

**Exact FAISS (`IndexFlatIP`), not HNSW.** The working set is a handful of
PDFs. Exact search is simpler to reason about and avoids ANN recall as a
variable in a demo. HNSW (or a hosted vector DB) is the next step at scale.

**ONNX embeddings instead of PyTorch.** MiniLM is ~90MB. FastEmbed keeps the
same 384-d space with a much smaller RAM footprint so a 512MB host is
plausible. Vectors are L2-normalized before `IndexFlatIP`.

**Grounding in the prompt, citations in the UI.** The model is instructed to
cite `[Source N]` matching the numbered context block. The API also returns
the retrieved snippets so the UI can show evidence even if the model
under-cites. Citation markers are normalized (including U+202F) before
render.

**In-process index.** One FastAPI worker owns the FAISS index. That is
correct for a single-node demo and incorrect for serverless (cold instances
do not share memory or disk). Deploy as a long-lived container, not a
Vercel-style function.

## API

| Method | Path | |
| --- | --- | --- |
| `GET` | `/health` | Liveness |
| `GET` | `/auth/config` | `{ demo, googleClientId }` |
| `POST` | `/auth/google` | Google Identity credential → app JWT |
| `GET` | `/documents` | Indexed docs |
| `POST` | `/upload` | `multipart/form-data` field `file` (PDF) |
| `DELETE` | `/documents/{id}` | Drop a doc and rebuild the index from remaining chunks |
| `POST` | `/ask` | `{ "question": "..." }` → `{ answer, sources[] }` |

Document/ask routes require a JWT when `DEMO_MODE=false`. In demo mode they
are open.

## Access

`DEMO_MODE=true` (default): the app is open. Visitors can use it with no
account. Google Sign-In is optional if `GOOGLE_CLIENT_ID` is set.

`DEMO_MODE=false`: Google Sign-In is required. Create an OAuth client in
[Google Cloud Console](https://console.cloud.google.com/apis/credentials)
(type **Web application**). Add authorized JavaScript origins:

- `http://localhost:5173`
- `http://127.0.0.1:5173`
- your Render URL, e.g. `https://docmind-xxxx.onrender.com`

Put the client ID in `GOOGLE_CLIENT_ID` and a random string in `AUTH_SECRET`.
Do not commit the client secret; GIS uses the client ID only on the frontend.

## Limitations

- **Always returns `TOP_K=4` chunks.** Weak matches can still appear as
  sources. A score cutoff belongs here next.
- **No OCR.** Image-only PDFs raise 422.
- **Delete rebuilds the whole index.** `IndexFlatIP` has no stable IDs;
  re-embed is the correct small-N approach. A production index would delete
  by key.
- **Host disk is ephemeral** on typical PaaS. A new instance starts empty.
- **`.env` is read at process start.** Changing a key requires a restart;
  `--reload` does not watch `.env`.

## Run locally

Python 3.12+, Node 18+, Groq key: [console.groq.com/keys](https://console.groq.com/keys).

```bash
cd backend
python -m venv venv
# Windows: .\venv\Scripts\Activate.ps1
# macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Windows: copy .env.example .env
```

`backend/.env`:

```
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
GROQ_MODEL=openai/gpt-oss-20b
```

```bash
uvicorn main:app --reload --port 8000
```

First start downloads the ONNX MiniLM weights once.

```bash
cd frontend
npm install
npm run dev
```

[http://localhost:5173](http://localhost:5173) — Vite proxies `/upload`,
`/ask`, `/documents`, `/health` to port 8000.

`LLM_PROVIDER=openai` is supported. The OpenAI **API** is billed separately
from ChatGPT Plus.

## Deploy

Single image: Node build of the UI, FastAPI serves `frontend/dist`.

```bash
docker build -t docmind .
docker run -p 8000:8000 -e LLM_PROVIDER=groq -e GROQ_API_KEY=gsk_... docmind
```

[`render.yaml`](render.yaml) targets Render’s free plan. Set `GROQ_API_KEY`
in the dashboard. Keep `DEMO_MODE=true` for an open demo. Free instances
sleep after idle; the first request after sleep is slow.

## Layout

```
backend/rag.py      ingest, FAISS, BM25, RRF, generation
backend/main.py     FastAPI + CORS + static UI in production
frontend/src/App.jsx  upload, ask, citation UI
```
