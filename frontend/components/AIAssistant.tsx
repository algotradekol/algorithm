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
      setError(e?.message || 'AI request failed');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed bottom-4 right-4 z-50">
      {open && (
        <section className="mb-3 flex h-[620px] w-[420px] max-w-[calc(100vw-2rem)] flex-col rounded border border-[#1f2937] bg-[#111827]">
          <header className="flex items-center justify-between border-b border-[#1f2937] p-3">
            <div>
              <div className="font-mono text-sm font-semibold text-gray-100">AI COPILOT</div>
              <div className="text-xs text-gray-500">Gemini-backed trading/system assistant</div>
            </div>
            <button onClick={() => setOpen(false)} className="text-gray-500 hover:text-gray-100">X</button>
          </header>

          <div className="flex gap-2 border-b border-[#1f2937] p-3">
            <select value={sessionId} onChange={(e) => setSessionId(e.target.value)} className="control text-xs">
              <option value="">Current chat</option>
              {sessions.map((session) => <option key={session.id} value={session.id}>{session.title}</option>)}
            </select>
            <button onClick={newChat} className="rounded border border-[#3b82f6] px-3 text-xs text-[#3b82f6]">New</button>
          </div>

          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3 text-sm">
            {!messages.length && <p className="text-gray-500">Ask about charts, scan bottlenecks, strategy settings, Fyers status, deployment, or what each UI section means.</p>}
            {messages.map((message, index) => (
              <div key={message.id || index} className={message.role === 'user' ? 'text-right' : 'text-left'}>
                <div className={`inline-block max-w-[92%] whitespace-pre-wrap rounded border px-3 py-2 ${
                  message.role === 'user'
                    ? 'border-[#3b82f6]/40 bg-[#3b82f6]/10 text-gray-100'
                    : 'border-[#1f2937] bg-[#0d1117] text-gray-200'
                }`}>
                  {message.content}
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
            <button onClick={send} disabled={loading} className="mt-2 w-full rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-60">
              {loading ? 'Thinking...' : 'Send'}
            </button>
          </div>
        </section>
      )}

      <button
        onClick={() => setOpen((value) => !value)}
        className="rounded border border-[#3b82f6] bg-[#111827] px-4 py-3 text-sm font-semibold text-[#3b82f6]"
      >
        AI Copilot
      </button>
    </div>
  );
}
