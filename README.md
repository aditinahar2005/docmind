# DocMind

Sourced Q&A over PDFs. Upload a document, ask a question, get an answer with
clickable citations back to the exact page.

A **RAG (retrieval-augmented generation)** demo: React + FastAPI, local
embeddings, hybrid search (FAISS + BM25), Groq for generation. Built to mirror
how production GenAI systems are wired on Azure.

<br>

## Resume one-liner

> Built a RAG Q&A app over PDFs: chunking, MiniLM embeddings, hybrid retrieval
> (FAISS vector search + BM25, fused with Reciprocal Rank Fusion), and an LLM
> that cites sources by page. React + FastAPI. Mapped 1:1 to Azure OpenAI +
> Azure AI Search.

Skills this shows: **RAG, embeddings, vector search, hybrid retrieval, FastAPI,
React, prompt design, citations / grounding.**

<br>

## Why this architecture

Every piece is a stand-in for a managed Azure service. The retrieve → prompt →
generate loop in `backend/rag.py` would not change if you swapped the left
column for the right — only the four classes for ingest, embed, store, and
generate would.

| This project (runs locally / cheap)    | Production equivalent                  |
|----------------------------------------|----------------------------------------|
| `pypdf` text extraction                | Azure AI Document Intelligence         |
| FastEmbed ONNX (MiniLM)                | Azure OpenAI `text-embedding-3-*`      |
| FAISS + BM25, fused with RRF           | Azure AI Search hybrid retrieval       |
| Groq (or OpenAI) chat API              | Azure OpenAI GPT-4o                    |
| FAISS index + JSON on disk             | Azure SQL / Cosmos DB + Search index   |

That mapping is the interview story: this is not “I called ChatGPT on a PDF.”
It is ingest → index → retrieve → generate, with grounding.

<br>

## Pipeline

```
 PDF
  │
  ▼
 extract text (pypdf)  →  ~800-char overlapping chunks
  │
  ▼
 MiniLM embeddings  →  FAISS IndexFlatIP (cosine via inner product)
  │
  ├─ vector search (semantic)
  └─ BM25 (keyword / names / error codes)
           │
           ▼
    Reciprocal Rank Fusion (k=60)  →  top chunks
           │
           ▼
    Groq LLM answers with [Source N] citations
           │
           ▼
    React UI: answer + clickable source tabs
```

<br>

## Setup (local)

Python 3.12+, Node 18+, and a Groq key (free, no card):
[console.groq.com/keys](https://console.groq.com/keys).

```bash
# backend
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1          # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env               # macOS/Linux: cp .env.example .env
```

Put the key in `backend/.env`:

```
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
GROQ_MODEL=openai/gpt-oss-20b
```

```bash
uvicorn main:app --reload --port 8000
```

First run downloads the ONNX `all-MiniLM-L6-v2` model (~90MB) once.

```bash
# frontend — second terminal
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**. The Vite proxy forwards `/upload`, `/ask`, and
`/documents` to the API.

The server reads `.env` at startup — restart after changing a key.

OpenAI also works (`LLM_PROVIDER=openai`) but the API is billed separately
from ChatGPT Plus and a new key has no credits until you add them.

<br>

## Using it

1. Drop a **text** PDF on the upload zone (scanned image-only PDFs have no
   text layer; pypdf cannot read those).
2. Ask a question. Suggested prompts appear after a file is indexed.
3. Click a numbered citation tab to see the source passage and page.
4. Click **×** next to a document to drop it from the index.

<br>

## Host it

One Docker image serves the built React app from FastAPI.

**Render:** push to GitHub, create a Web Service from `render.yaml`, and set
`GROQ_API_KEY`. The ONNX embedding runtime is intentionally lightweight enough
to target Render's 512 MB free tier. Free services sleep after 15 idle minutes,
so the first visit can take about a minute.

```bash
docker build -t docmind .
docker run -p 8000:8000 -e LLM_PROVIDER=groq -e GROQ_API_KEY=gsk_... docmind
```

The FAISS index lives in `backend/data/`. Local restarts keep uploads.
Ephemeral hosts (Render/Railway) start empty after a new instance — the
managed analog is Azure AI Search.

<br>

## What I'd add with more time

- Point the same `retrieve()` API at Azure AI Search
- Score threshold so weak matches are not returned as “sources”
- Streaming tokens instead of waiting for the full answer
- Azure AI Document Intelligence for scanned / image PDFs
