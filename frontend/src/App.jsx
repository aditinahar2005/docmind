import { useEffect, useRef, useState } from 'react'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

function useDocuments() {
  const [documents, setDocuments] = useState([])

  const refresh = async () => {
    const res = await fetch(`${API_BASE}/documents`)
    if (res.ok) setDocuments(await res.json())
  }

  useEffect(() => {
    refresh()
  }, [])

  return { documents, refresh }
}

function UploadZone({ onUploaded }) {
  const [dragging, setDragging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const inputRef = useRef(null)

  const upload = async (file) => {
    if (!file) return
    setBusy(true)
    setError(null)
    const form = new FormData()
    form.append('file', file)
    try {
      const res = await fetch(`${API_BASE}/upload`, { method: 'POST', body: form })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || 'Upload failed')
      }
      await onUploaded()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className={`upload-zone ${dragging ? 'is-dragging' : ''} ${busy ? 'is-busy' : ''}`}
      onDragOver={(e) => {
        e.preventDefault()
        setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragging(false)
        upload(e.dataTransfer.files[0])
      }}
      onClick={() => inputRef.current?.click()}
    >
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        hidden
        onChange={(e) => upload(e.target.files[0])}
      />
      <span className="upload-mark">+</span>
      <p>{busy ? 'Reading and indexing…' : 'Drop a PDF, or click to browse'}</p>
      {error && <p className="upload-error">{error}</p>}
    </div>
  )
}

function CaseFile({ documents, onRemoved }) {
  const remove = async (id) => {
    await fetch(`${API_BASE}/documents/${id}`, { method: 'DELETE' })
    await onRemoved()
  }

  return (
    <div className="case-file">
      <h2>Case file</h2>
      {documents.length === 0 && <p className="muted">No documents indexed yet.</p>}
      <ul>
        {documents.map((d) => (
          <li key={d.id}>
            <div className="doc-row">
              <span className="doc-name">{d.name}</span>
              <button
                className="doc-remove"
                title="Remove from index"
                onClick={() => remove(d.id)}
              >
                ×
              </button>
            </div>
            <span className="doc-meta mono">
              {d.num_pages}p · {d.num_chunks} chunks
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// Models cite with varying whitespace (including U+202F), so match loosely.
const CITATION = /\[\s*Source\s*\d+\s*\]/
const INLINE_SPLIT = /(\[\s*Source\s*\d+\s*\]|\*\*[^*]+\*\*)/g

function AnswerBlock({ entry }) {
  const [activeSource, setActiveSource] = useState(null)

  const renderInline = (text, keyPrefix) =>
    text.split(INLINE_SPLIT).map((part, i) => {
      const key = `${keyPrefix}-${i}`
      if (CITATION.test(part)) {
        const n = Number(part.match(/\d+/)[0])
        return (
          <button
            key={key}
            className={`cite-tab ${activeSource === n ? 'is-active' : ''}`}
            onClick={() => setActiveSource(n === activeSource ? null : n)}
          >
            {n}
          </button>
        )
      }
      const bold = part.match(/^\*\*([^*]+)\*\*$/)
      if (bold) return <strong key={key}>{bold[1]}</strong>
      return <span key={key}>{part}</span>
    })

  // Minimal markdown: paragraphs, bullet and numbered lists, bold.
  const renderAnswer = (text) =>
    text
      .split(/\n{2,}/)
      .map((block) => block.split('\n').map((l) => l.trim()).filter(Boolean))
      .filter((lines) => lines.length > 0)
      .map((lines, b) => {
        if (lines.every((l) => /^[-*]\s+/.test(l))) {
          return (
            <ul key={b}>
              {lines.map((l, i) => (
                <li key={i}>{renderInline(l.replace(/^[-*]\s+/, ''), `${b}-${i}`)}</li>
              ))}
            </ul>
          )
        }
        if (lines.every((l) => /^\d+[.)]\s+/.test(l))) {
          return (
            <ol key={b}>
              {lines.map((l, i) => (
                <li key={i}>{renderInline(l.replace(/^\d+[.)]\s+/, ''), `${b}-${i}`)}</li>
              ))}
            </ol>
          )
        }
        return <p key={b}>{renderInline(lines.join(' '), String(b))}</p>
      })

  return (
    <div className="answer-block">
      <div className="question-line">
        <span className="q-mark">Q</span>
        <p>{entry.question}</p>
      </div>
      <div className="answer-text">{renderAnswer(entry.answer)}</div>

      {entry.sources.length > 0 && (
        <div className="sources-strip">
          {entry.sources.map((s) => (
            <div
              key={s.n}
              className={`source-card ${activeSource === s.n ? 'is-active' : ''}`}
              onClick={() => setActiveSource(s.n === activeSource ? null : s.n)}
            >
              <div className="source-head">
                <span className="source-num">{s.n}</span>
                <span className="mono">{s.doc_name} · p.{s.page}</span>
              </div>
              {activeSource === s.n && <p className="source-snippet">{s.snippet}</p>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function App() {
  const { documents, refresh } = useDocuments()
  const [thread, setThread] = useState([])
  const [question, setQuestion] = useState('')
  const [asking, setAsking] = useState(false)
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [thread])

  const ask = async (e) => {
    e.preventDefault()
    if (!question.trim() || asking) return
    const q = question.trim()
    setQuestion('')
    setAsking(true)
    try {
      const res = await fetch(`${API_BASE}/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Something went wrong')
      setThread((t) => [...t, { question: q, answer: data.answer, sources: data.sources }])
    } catch (err) {
      setThread((t) => [...t, { question: q, answer: `Error: ${err.message}`, sources: [] }])
    } finally {
      setAsking(false)
    }
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">DM</span>
          <div>
            <h1>DocMind</h1>
            <p className="tagline">Ask your documents. Every answer is sourced.</p>
          </div>
        </div>
        <UploadZone onUploaded={refresh} />
        <CaseFile documents={documents} onRemoved={refresh} />
        <div className="stack-note mono">
          MiniLM embeddings → FAISS + BM25 (RRF) → Groq
        </div>
      </aside>

      <main className="thread">
        {thread.length === 0 && (
          <div className="empty-state">
            <p className="empty-eyebrow mono">RAG pipeline</p>
            <h2>Upload a PDF, then ask it anything.</h2>
            <p className="muted">
              Answers cite the exact page they came from — click a numbered tab to
              see the source passage.
            </p>
            <ol className="pipeline">
              <li>
                <span className="mono">01</span> Extract &amp; chunk text
              </li>
              <li>
                <span className="mono">02</span> Embed into a FAISS index
              </li>
              <li>
                <span className="mono">03</span> Hybrid retrieve (vector + BM25)
              </li>
              <li>
                <span className="mono">04</span> Generate with inline citations
              </li>
            </ol>
            {documents.length > 0 && (
              <div className="prompt-hints">
                <p className="muted">Try asking:</p>
                {['Summarize this document.', 'What skills are listed?', 'What projects are mentioned?'].map(
                  (hint) => (
                    <button
                      key={hint}
                      type="button"
                      className="hint-chip"
                      onClick={() => setQuestion(hint)}
                    >
                      {hint}
                    </button>
                  ),
                )}
              </div>
            )}
          </div>
        )}

        {thread.map((entry, i) => (
          <AnswerBlock key={i} entry={entry} />
        ))}
        <div ref={bottomRef} />

        <form className="ask-bar" onSubmit={ask}>
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={documents.length ? 'Ask a question about your documents…' : 'Upload a PDF first…'}
            disabled={asking}
          />
          <button type="submit" disabled={asking || !question.trim()}>
            {asking ? '···' : 'Ask'}
          </button>
        </form>
      </main>
    </div>
  )
}
