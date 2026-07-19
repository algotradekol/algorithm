'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

export default function AIAssistant() {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState<any[]>([]);
  const [sessionId, setSessionId] = useState('');
  const [messages, setMessages] = useState<any[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    api.aiSessions().then((result) => {
      setSessions(result || []);
      if (!sessionId && result?.[0]) setSessionId(result[0].id);
    }).catch((e: any) => setError(e?.message || 'Failed to load AI chats'));
  }, [open, sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    api.aiMessages(sessionId).then(setMessages).catch((e: any) => setError(e?.message || 'Failed to load messages'));
  }, [sessionId]);

  async function newChat() {
    const session = await api.aiCreateSession('New chat');
    setSessions((current) => [session, ...current]);
    setSessionId(session.id);
    setMessages([]);
  }

  async function send() {
    const message = input.trim();
    if (!message || loading) return;
    setInput('');
    setLoading(true);
    setError('');
    const optimistic = [...messages, { role: 'user', content: message }];
    setMessages(optimistic);
    try {
      const result = await api.aiChat({
        session_id: sessionId || null,
        message,
        page_context: {
          url: typeof window !== 'undefined' ? window.location.href : '',
          active_tab_text: typeof document !== 'undefined' ? document.title : '',
          visible_page_text: typeof document !== 'undefined' ? document.body.innerText.slice(0, 12000) : '',
        },
      });
      setSessionId(result.session.id);
      setMessages(result.messages || [...optimistic, { role: 'assistant', content: result.answer }]);
      api.aiSessions().then(setSessions).catch(() => {});
    } catch (e: any) {
      const messageText = e?.message || 'AI request failed';
      setError(messageText.includes('429') || messageText.toLowerCase().includes('quota')
        ? 'Gemini quota/rate limit hit. Wait a bit, avoid repeated sends, or check the API key quota/billing in Google AI Studio or Google Cloud.'
        : messageText);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed bottom-3 right-3 z-50 flex flex-col items-end sm:bottom-4 sm:right-4">
      {open && (
        <section className="mb-3 flex h-[min(620px,calc(100vh-6rem))] w-[calc(100vw-1.5rem)] max-w-[420px] flex-col rounded border border-[#1f2937] bg-[#111827]">
          <header className="flex items-center justify-between border-b border-[#1f2937] p-3">
            <div>
              <div className="font-mono text-sm font-semibold text-gray-100">AI COPILOT</div>
              <div className="text-xs text-gray-500">Gemini-backed trading/system assistant</div>
            </div>
            <button onClick={() => setOpen(false)} className="min-h-10 px-2 text-gray-500 hover:text-gray-100">
              <i className="ri-close-circle-fill text-base" />
            </button>
          </header>

          <div className="flex gap-2 border-b border-[#1f2937] p-3">
            <select value={sessionId} onChange={(e) => setSessionId(e.target.value)} className="control text-xs">
              <option value="">Current chat</option>
              {sessions.map((session) => <option key={session.id} value={session.id}>{session.title}</option>)}
            </select>
            <button onClick={newChat} className="min-h-10 rounded border border-[#3b82f6] px-3 text-xs text-[#3b82f6]">New</button>
          </div>

          <div className="scrollbar-hidden min-h-0 flex-1 space-y-3 overflow-y-auto p-3 text-sm">
            {!messages.length && <p className="text-gray-500">Ask about charts, scan bottlenecks, strategy settings, Fyers status, deployment, or what each UI section means.</p>}
            {messages.map((message, index) => (
              <div key={message.id || index} className={message.role === 'user' ? 'text-right' : 'text-left'}>
                <div className={`inline-block max-w-[92%] whitespace-pre-wrap rounded border px-3 py-2 ${
                  message.role === 'user'
                    ? 'border-[#3b82f6]/40 bg-[#3b82f6]/10 text-gray-100'
                    : 'border-[#1f2937] bg-[#0d1117] text-gray-200'
                }`}>
                  {message.role === 'assistant' ? <FormattedMessage content={message.content} /> : message.content}
                </div>
              </div>
            ))}
            {loading && <p className="text-xs text-gray-500">Thinking with current app context...</p>}
            {error && <p className="text-xs text-[#ef4444]">{error}</p>}
          </div>

          <div className="border-t border-[#1f2937] p-3">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              placeholder="Ask what happened, what to fix, or explain this page..."
              className="control h-20 resize-none"
            />
            <button onClick={send} disabled={loading} className="mt-2 min-h-10 w-full rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-60">
              {loading ? 'Thinking...' : 'Send'}
            </button>
          </div>
        </section>
      )}

      <button
        onClick={() => setOpen((value) => !value)}
        aria-label="Open AI Copilot"
        title="AI Copilot"
        className="flex h-12 w-12 shrink-0 items-center justify-center rounded border border-[#3b82f6] bg-[#111827] text-[#3b82f6]"
      >
        <i className="ri-robot-2-fill text-xl" />
      </button>
    </div>
  );
}

function FormattedMessage({ content }: { content: string }) {
  return (
    <div className="space-y-2 whitespace-normal text-left leading-relaxed">
      {content.split(/\n{2,}/).map((block, index) => {
        const trimmed = block.trim();
        if (!trimmed) return null;

        if (trimmed.startsWith('### ')) {
          return <h3 key={index} className="mt-3 text-sm font-semibold text-gray-100">{formatInline(trimmed.slice(4))}</h3>;
        }
        if (trimmed.startsWith('## ')) {
          return <h2 key={index} className="mt-3 text-base font-semibold text-gray-100">{formatInline(trimmed.slice(3))}</h2>;
        }
        if (trimmed.startsWith('# ')) {
          return <h1 key={index} className="mt-3 text-base font-semibold text-gray-100">{formatInline(trimmed.slice(2))}</h1>;
        }

        const lines = trimmed.split('\n');
        if (lines.every((line) => /^(\*|-)\s+/.test(line.trim()))) {
          return (
            <ul key={index} className="space-y-1 pl-4 text-gray-300">
              {lines.map((line, lineIndex) => (
                <li key={lineIndex} className="list-disc">{formatInline(line.trim().replace(/^(\*|-)\s+/, ''))}</li>
              ))}
            </ul>
          );
        }

        return (
          <p key={index} className="text-gray-300">
            {lines.map((line, lineIndex) => (
              <span key={lineIndex}>
                {formatInline(line)}
                {lineIndex < lines.length - 1 && <br />}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}

function formatInline(text: string) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return parts.map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={index} className="font-semibold text-gray-100">{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('`') && part.endsWith('`')) {
      return <code key={index} className="rounded border border-[#1f2937] bg-[#111827] px-1 py-0.5 font-mono text-xs text-[#93c5fd]">{part.slice(1, -1)}</code>;
    }
    return part;
  });
}
