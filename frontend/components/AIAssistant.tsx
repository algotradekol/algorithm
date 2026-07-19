'use client';
import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';

export default function AIAssistant() {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState<any[]>([]);
  const [sessionId, setSessionId] = useState('');
  const [messages, setMessages] = useState<any[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

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

  useEffect(() => {
    if (!open) return;
    requestAnimationFrame(() => messagesEndRef.current?.scrollIntoView({ block: 'end' }));
  }, [open, messages, sessionId]);

  async function newChat() {
    const session = await api.aiCreateSession('New chat');
    setSessions((current) => [session, ...current]);
    setSessionId(session.id);
    setMessages([]);
  }

  async function deleteChat() {
    if (!sessionId) return;
    const selected = sessions.find((session) => session.id === sessionId);
    if (!window.confirm(`Delete "${selected?.title || 'this chat'}"?`)) return;
    try {
      await api.aiDeleteSession(sessionId);
      const remaining = sessions.filter((session) => session.id !== sessionId);
      setSessions(remaining);
      setSessionId(remaining[0]?.id || '');
      setMessages([]);
      setError('');
    } catch (e: any) {
      setError(e?.message || 'Failed to delete chat');
    }
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
        page_context: getLivePageContext(),
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
          <header className="flex items-center justify-between border-b border-[#1f2937] px-3 py-2">
            <div>
              <div className="font-mono text-sm font-semibold text-gray-100">AI COPILOT</div>
              <div className="text-xs text-gray-500">Gemini-backed trading/system assistant</div>
            </div>
            <button onClick={() => setOpen(false)} className="min-h-10 px-2 text-gray-500 hover:text-gray-100">
              <i className="ri-close-circle-fill text-base" />
            </button>
          </header>

          <div className="flex gap-2 border-b border-[#1f2937] px-3 py-2">
            <select value={sessionId} onChange={(e) => setSessionId(e.target.value)} className="control min-h-9 py-1.5 text-xs">
              <option value="">Current chat</option>
              {sessions.map((session) => <option key={session.id} value={session.id}>{session.title}</option>)}
            </select>
            <button onClick={newChat} className="min-h-9 rounded border border-[#3b82f6] px-3 text-xs text-[#3b82f6]">New</button>
            <button
              onClick={deleteChat}
              disabled={!sessionId}
              aria-label="Delete selected AI chat"
              title="Delete selected chat"
              className="min-h-9 rounded border border-[#ef4444]/50 px-3 text-xs text-[#ef4444] disabled:cursor-not-allowed disabled:border-[#1f2937] disabled:text-gray-600"
            >
              <i className="ri-delete-bin-fill text-sm" />
            </button>
          </div>

          <div className="relative min-h-0 flex-[1_1_auto]">
            <ChatRail sessions={sessions} activeId={sessionId} onSelect={setSessionId} />
            <div className="chat-scrollbar h-full space-y-3 overflow-y-auto py-2 pl-8 pr-3 text-sm">
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
            <div ref={messagesEndRef} />
            </div>
          </div>

          <div className="border-t border-[#1f2937] px-3 py-2">
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
              className="control h-12 resize-none py-2"
            />
            <button onClick={send} disabled={loading} className="mt-2 min-h-9 w-full rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-1.5 text-sm font-semibold text-white disabled:opacity-60">
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

function ChatRail({
  sessions,
  activeId,
  onSelect,
}: {
  sessions: any[];
  activeId: string;
  onSelect: (id: string) => void;
}) {
  if (!sessions.length) return null;
  return (
    <div className="absolute bottom-2 left-2 top-2 z-10 flex w-4 flex-col items-center gap-1 overflow-hidden">
      {sessions.slice(0, 28).map((session) => (
        <button
          key={session.id}
          type="button"
          onClick={() => onSelect(session.id)}
          title={session.title || 'AI chat'}
          className={`group relative h-2 w-2 shrink-0 rounded-sm transition-all hover:w-4 ${
            session.id === activeId ? 'bg-gray-200' : 'bg-gray-600 hover:bg-[#3b82f6]'
          }`}
        >
          <span className="pointer-events-none absolute left-5 top-1/2 hidden w-64 -translate-y-1/2 rounded border border-[#1f2937] bg-[#0d1117] p-2 text-left shadow-xl group-hover:block">
            <span className="block truncate text-xs font-semibold text-gray-100">{session.title || 'AI chat'}</span>
            <span className="mt-1 block line-clamp-2 text-[11px] leading-4 text-gray-500">
              {session.preview || session.title || 'Open this previous chat'}
            </span>
          </span>
        </button>
      ))}
    </div>
  );
}

function getLivePageContext() {
  if (typeof document === 'undefined') return {};
  const main = document.querySelector('main');
  const activeTab = main?.getAttribute('data-ai-active-tab') || '';
  const activeSection = document.querySelector('[data-ai-section]');
  const chart = document.querySelector('[data-ai-chart]');

  return {
    url: typeof window !== 'undefined' ? window.location.href : '',
    document_title: document.title,
    active_tab: activeTab,
    active_section: activeSection?.getAttribute('data-ai-section') || activeTab,
    history: activeSection?.getAttribute('data-ai-section') === 'History' ? {
      algo: activeSection.getAttribute('data-ai-history-algo'),
      days: activeSection.getAttribute('data-ai-history-days'),
      symbol: activeSection.getAttribute('data-ai-history-symbol'),
      resolution: activeSection.getAttribute('data-ai-history-resolution'),
      candle_count: activeSection.getAttribute('data-ai-history-candle-count'),
    } : null,
    chart: chart ? {
      type: chart.getAttribute('data-ai-chart'),
      symbol: chart.getAttribute('data-ai-chart-symbol'),
      resolution: chart.getAttribute('data-ai-chart-resolution'),
      total_candles: chart.getAttribute('data-ai-chart-total-candles'),
      visible_range: chart.getAttribute('data-ai-chart-visible-range'),
      open: chart.getAttribute('data-ai-chart-open'),
      high: chart.getAttribute('data-ai-chart-high'),
      low: chart.getAttribute('data-ai-chart-low'),
      close: chart.getAttribute('data-ai-chart-close'),
      change: chart.getAttribute('data-ai-chart-change'),
      first_time: chart.getAttribute('data-ai-chart-first-time'),
      last_time: chart.getAttribute('data-ai-chart-last-time'),
    } : null,
    visible_page_text: document.body.innerText.slice(0, 12000),
  };
}

function FormattedMessage({ content }: { content: string }) {
  const normalized = normalizeAssistantMarkdown(content);
  return (
    <div className="space-y-2 whitespace-normal text-left leading-relaxed">
      {normalized.split(/\n{2,}/).map((block, index) => {
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
        if (/^\d+\.\s+/.test(lines[0].trim())) {
          const title = lines[0].trim().replace(/^\d+\.\s+/, '');
          const rest = lines.slice(1).filter(Boolean);
          return (
            <section key={index} className="space-y-1">
              <h3 className="text-sm font-semibold text-gray-100">{formatInline(title)}</h3>
              {rest.length > 0 && (
                <ul className="space-y-1 pl-4 text-gray-300">
                  {rest.map((line, lineIndex) => (
                    <li key={lineIndex} className="list-disc">{formatInline(line.trim().replace(/^(\*|-)\s+/, ''))}</li>
                  ))}
                </ul>
              )}
            </section>
          );
        }
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

function normalizeAssistantMarkdown(content: string) {
  return content
    .replace(/\r\n/g, '\n')
    .replace(/(^|\n)(\d+\.\s+)/g, '\n\n$2')
    .replace(/(\d+\.\s+[^*\n]+?)\s+\*\s+/g, '$1\n- ')
    .replace(/\s+\*\s+/g, '\n- ')
    .replace(/([.!?])\s+(What would you like to do next\??)/g, '$1\n\n### $2')
    .replace(/\n{3,}/g, '\n\n');
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
