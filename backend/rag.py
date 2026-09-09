"""
Core RAG engine.

Design note (for reviewers): every piece here is deliberately swappable for
its managed-Azure counterpart with no change to the API surface:

    This project (free/local)        ->  Production (Azure)
    ---------------------------------------------------------------
    pypdf text extraction            ->  Azure AI Document Intelligence
    sentence-transformers embeddings ->  Azure OpenAI text-embedding-3
    FAISS + hybrid (vector + BM25)   ->  Azure AI Search (hybrid retrieval)
    OpenAI / Groq chat API           ->  Azure OpenAI GPT-4o
    FAISS + JSON on disk             ->  Azure SQL / Cosmos DB + Search index

The retrieval + prompting logic below would not need to change if you
swapped every row on the right in; only the four classes below would.
"""

from __future__ import annotations

import io
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import faiss
import numpy as np
from fastembed import TextEmbedding
from pypdf import PdfReader

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CHUNK_SIZE = 800          # characters per chunk
CHUNK_OVERLAP = 150       # characters of overlap between chunks
TOP_K = 4                 # chunks returned to the LLM
CANDIDATE_K = 16          # per-retriever pool before hybrid fusion
RRF_K = 60                # Azure AI Search uses Reciprocal Rank Fusion with k=60
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
PLACEHOLDER_KEYS = {"", "your_key_here", "gsk_...", "sk-..."}
DATA_DIR = Path(__file__).resolve().parent / "data"
INDEX_PATH = DATA_DIR / "index.faiss"
META_PATH = DATA_DIR / "meta.json"
_WORD = re.compile(r"[a-z0-9]+")

_embedder: TextEmbedding | None = None


def get_embedder() -> TextEmbedding:
    """Lazy-load the lightweight ONNX embedding model."""
    global _embedder
    if _embedder is None:
        _embedder = TextEmbedding(
            model_name=EMBEDDING_MODEL,
            threads=1,
            extra_session_options={"enable_cpu_mem_arena": False},
        )
    return _embedder


def encode(texts: list[str]) -> np.ndarray:
    """Embed and L2-normalize text for cosine search with IndexFlatIP."""
    vectors = np.asarray(
        list(get_embedder().embed(texts, batch_size=8)),
        dtype="float32",
    )
    faiss.normalize_L2(vectors)
    return vectors


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    id: str
    doc_id: str
    doc_name: str
    page: int
    text: str


@dataclass
class Document:
    id: str
    name: str
    num_pages: int
    num_chunks: int


@dataclass
class Store:
    """Local stand-in for Azure AI Search.

    Vector half: FAISS IndexFlatIP on L2-normalized embeddings (cosine).
    Keyword half: BM25 over the same chunks.
    Fusion: Reciprocal Rank Fusion — the same family of ranking Azure uses
    for hybrid (keyword + vector) search.

    The index is written to disk so a restart does not wipe uploads. Swap
    this class for Azure AI Search to get scale, filters, and HA.
    """

    index: faiss.IndexFlatIP | None = None
    chunks: list[Chunk] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)

    def is_empty(self) -> bool:
        return self.index is None or self.index.ntotal == 0


STORE = Store()


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _save_store() -> None:
    if STORE.index is None:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    faiss.write_index(STORE.index, str(INDEX_PATH))
    META_PATH.write_text(
        json.dumps(
            {
                "chunks": [asdict(c) for c in STORE.chunks],
                "documents": [asdict(d) for d in STORE.documents],
            }
        ),
        encoding="utf-8",
    )


def _load_store() -> None:
    if not INDEX_PATH.exists() or not META_PATH.exists():
        return
    STORE.index = faiss.read_index(str(INDEX_PATH))
    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    STORE.chunks = [Chunk(**c) for c in meta.get("chunks", [])]
    STORE.documents = [Document(**d) for d in meta.get("documents", [])]
    if STORE.index.ntotal != len(STORE.chunks):
        STORE.index = None
        STORE.chunks = []
        STORE.documents = []


_load_store()


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def extract_pages(pdf_bytes: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [page.extract_text() or "" for page in reader.pages]


def chunk_text(text: str, page: int) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end])
        start = end - CHUNK_OVERLAP
        if start <= 0:
            break
    return chunks


def add_document(filename: str, pdf_bytes: bytes) -> Document:
    pages = extract_pages(pdf_bytes)
    doc_id = str(uuid.uuid4())[:8]

    new_chunks: list[Chunk] = []
    for page_num, page_text in enumerate(pages, start=1):
        for piece in chunk_text(page_text, page_num):
            new_chunks.append(
                Chunk(
                    id=str(uuid.uuid4())[:8],
                    doc_id=doc_id,
                    doc_name=filename,
                    page=page_num,
                    text=piece,
                )
            )

    if not new_chunks:
        raise ValueError("No extractable text found in this PDF (is it a scanned image?).")

    vectors = encode([c.text for c in new_chunks])

    if STORE.index is None:
        STORE.index = faiss.IndexFlatIP(int(vectors.shape[1]))
    STORE.index.add(vectors)
    STORE.chunks.extend(new_chunks)

    doc = Document(id=doc_id, name=filename, num_pages=len(pages), num_chunks=len(new_chunks))
    STORE.documents.append(doc)
    _save_store()
    return doc


# ---------------------------------------------------------------------------
# Retrieval + generation
# ---------------------------------------------------------------------------


def _vector_ranks(question: str, n: int) -> list[int]:
    q_vec = encode([question])
    _scores, idxs = STORE.index.search(q_vec, n)
    return [int(i) for i in idxs[0] if i != -1]


def _bm25_ranks(question: str, n: int) -> list[int]:
    """Okapi BM25 — the keyword side of Azure AI Search hybrid retrieval."""
    docs = [tokenize(c.text) for c in STORE.chunks]
    N = len(docs)
    if N == 0:
        return []
    avgdl = sum(len(d) for d in docs) / N
    df: dict[str, int] = {}
    for doc in docs:
        for t in set(doc):
            df[t] = df.get(t, 0) + 1

    q_tokens = tokenize(question)
    scores = np.zeros(N, dtype=np.float32)
    k1, b = 1.5, 0.75
    for i, doc in enumerate(docs):
        if not doc:
            continue
        tf: dict[str, int] = {}
        for t in doc:
            tf[t] = tf.get(t, 0) + 1
        dl = len(doc)
        s = 0.0
        for t in q_tokens:
            if t not in tf:
                continue
            n_q = df.get(t, 0)
            idf = np.log((N - n_q + 0.5) / (n_q + 0.5) + 1.0)
            s += idf * (tf[t] * (k1 + 1)) / (tf[t] + k1 * (1 - b + b * dl / avgdl))
        scores[i] = s

    order = np.argsort(-scores)
    return [int(i) for i in order[:n] if scores[i] > 0]


def _rrf(rank_lists: list[list[int]], k: int = RRF_K) -> list[int]:
    """Reciprocal Rank Fusion — Azure AI Search's default hybrid combiner."""
    scores: dict[int, float] = {}
    for ranks in rank_lists:
        for rank, idx in enumerate(ranks, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    return [idx for idx, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


def retrieve(question: str, k: int = TOP_K) -> list[Chunk]:
    if STORE.is_empty():
        return []
    n = min(CANDIDATE_K, len(STORE.chunks))
    fused = _rrf([_vector_ranks(question, n), _bm25_ranks(question, n)])
    if not fused:
        fused = _vector_ranks(question, n)
    return [STORE.chunks[i] for i in fused[:k]]


def build_prompt(question: str, chunks: list[Chunk]) -> str:
    context_block = "\n\n".join(
        f"[Source {i + 1}: {c.doc_name}, p.{c.page}]\n{c.text}" for i, c in enumerate(chunks)
    )
    return f"""You are a careful research assistant. Answer the question using ONLY the
sources below. If the sources do not contain the answer, say so plainly.
Cite sources inline like [Source 1], [Source 2] matching the numbers given.

SOURCES:
{context_block}

QUESTION: {question}

ANSWER (with inline [Source N] citations):"""


def _completion_text(completion) -> str:
    message = completion.choices[0].message
    content = (getattr(message, "content", None) or "").strip()
    if content:
        return content
    reasoning = getattr(message, "reasoning", None) or ""
    if reasoning.strip():
        return reasoning.strip()
    raise RuntimeError("The model returned an empty answer. Try again, or switch MODEL / GROQ_MODEL.")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _has_key(provider: str) -> bool:
    key = _env("OPENAI_API_KEY" if provider == "openai" else "GROQ_API_KEY")
    return key not in PLACEHOLDER_KEYS


def _llm_provider() -> str:
    forced = _env("LLM_PROVIDER").lower()
    if forced in {"openai", "groq"}:
        if not _has_key(forced):
            var = "OPENAI_API_KEY" if forced == "openai" else "GROQ_API_KEY"
            where = (
                "https://platform.openai.com/api-keys"
                if forced == "openai"
                else "https://console.groq.com/keys"
            )
            raise RuntimeError(
                f"LLM_PROVIDER={forced} but {var} is not set. Get a key at {where}, "
                "put it in backend/.env, then restart the server."
            )
        return forced
    if _has_key("openai"):
        return "openai"
    if _has_key("groq"):
        return "groq"
    raise RuntimeError(
        "No LLM key found. Add OPENAI_API_KEY (or GROQ_API_KEY) to backend/.env "
        "and restart the server. See .env.example."
    )


def _llm_model(provider: str) -> str:
    if provider == "openai":
        return _env("OPENAI_MODEL") or _env("MODEL") or DEFAULT_OPENAI_MODEL
    return _env("GROQ_MODEL") or _env("MODEL") or DEFAULT_GROQ_MODEL


def _chat(client, model: str, prompt: str, *, token_kw: str):
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        token_kw: 1200,
    }
    try:
        return client.chat.completions.create(**kwargs)
    except TypeError:
        kwargs.pop(token_kw, None)
        other = "max_tokens" if token_kw == "max_completion_tokens" else "max_completion_tokens"
        return client.chat.completions.create(**kwargs, **{other: 1200})


def _generate(prompt: str) -> str:
    provider = _llm_provider()
    model = _llm_model(provider)
    if provider == "openai":
        from openai import OpenAI, APIError

        client = OpenAI(api_key=_env("OPENAI_API_KEY"))
        try:
            completion = _chat(client, model, prompt, token_kw="max_tokens")
        except APIError as e:
            raise RuntimeError(f"OpenAI API error: {e.message}") from e
        return _completion_text(completion)

    from groq import Groq, APIError

    client = Groq(api_key=_env("GROQ_API_KEY"))
    try:
        completion = _chat(client, model, prompt, token_kw="max_completion_tokens")
    except APIError as e:
        raise RuntimeError(f"Groq API error: {e.message}") from e
    return _completion_text(completion)


def answer_question(question: str) -> dict:
    chunks = retrieve(question)
    if not chunks:
        return {
            "answer": "Upload a PDF first -- there's nothing indexed yet.",
            "sources": [],
        }

    prompt = build_prompt(question, chunks)
    answer_text = _generate(prompt)
    # Models sometimes emit "[Source\u202f1]" with a narrow no-break space, which
    # breaks citation parsing in the UI. Normalize to a plain space.
    answer_text = re.sub(r"\[\s*Source\s*(\d+)\s*\]", r"[Source \1]", answer_text)

    return {
        "answer": answer_text,
        "sources": [
            {
                "n": i + 1,
                "doc_name": c.doc_name,
                "page": c.page,
                "snippet": c.text[:220] + ("..." if len(c.text) > 220 else ""),
            }
            for i, c in enumerate(chunks)
        ],
    }


def list_documents() -> list[Document]:
    return STORE.documents


def delete_document(doc_id: str) -> None:
    """Drop a document and rebuild the index from the surviving chunks.

    IndexFlatIP has no stable per-vector id, so the cheapest correct move at
    this scale is a full re-embed. Azure AI Search would take a delete by key.
    """
    if not any(d.id == doc_id for d in STORE.documents):
        raise ValueError(f"No document with id {doc_id}.")

    STORE.documents = [d for d in STORE.documents if d.id != doc_id]
    STORE.chunks = [c for c in STORE.chunks if c.doc_id != doc_id]

    if not STORE.chunks:
        STORE.index = None
        INDEX_PATH.unlink(missing_ok=True)
        META_PATH.unlink(missing_ok=True)
        return

    vectors = encode([c.text for c in STORE.chunks])
    STORE.index = faiss.IndexFlatIP(int(vectors.shape[1]))
    STORE.index.add(vectors)
    _save_store()
