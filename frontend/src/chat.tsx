import { createContext, useContext, useState, useRef, type RefObject, type ReactNode, type Dispatch, type SetStateAction } from 'react';
import type { AgentAction, AgentFollowup, AgentTurnState } from './api';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  actions?: AgentAction[];
  /** Stable id for this send; retries reuse it so the turn runs once. */
  requestId?: string;
  sendState?: 'sending' | 'sent' | 'failed';
  /** Why a failed send must not auto-retry: rejected (deterministic) or outcome unknown. */
  failureKind?: 'rejected' | 'unknown';
}

export interface PendingFollowups {
  entryId: string;
  followups: AgentFollowup[];
}

interface ChatState {
  input: string;
  setInput: Dispatch<SetStateAction<string>>;
  busy: boolean;
  setBusy: Dispatch<SetStateAction<boolean>>;
  sending: RefObject<boolean>;
  pending: PendingFollowups | null;
  setPending: Dispatch<SetStateAction<PendingFollowups | null>>;
  followAnswers: Record<string, string>;
  setFollowAnswers: Dispatch<SetStateAction<Record<string, string>>>;
  messages: ChatMessage[];
  appendMessage: (message: Omit<ChatMessage, 'id'>) => string;
  updateMessage: (id: string, patch: Partial<ChatMessage>) => void;
  clearMessages: () => void;
  justLoggedId: string | null;
  setJustLoggedId: (id: string | null) => void;
  /** Last turn's validated state, echoed back so the assistant keeps focus. */
  turnState: AgentTurnState | null;
  setTurnState: (state: AgentTurnState | null) => void;
}

const ChatContext = createContext<ChatState | null>(null);

let nextId = 1;

/**
 * Conversation lives above the navigator: the assistant opens as a modal
 * that unmounts on dismiss, but the transcript survives for the session.
 */
export function ChatProvider({ children }: { children: ReactNode }) {
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const [pending, setPending] = useState<PendingFollowups | null>(null);
  const [followAnswers, setFollowAnswers] = useState<Record<string, string>>({});
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [justLoggedId, setJustLoggedId] = useState<string | null>(null);
  const [turnState, setTurnState] = useState<AgentTurnState | null>(null);
  const appendMessage = (message: Omit<ChatMessage, 'id'>) => {
    const id = `m${nextId++}`;
    setMessages((prev) => [...prev, { ...message, id }]);
    return id;
  };
  const updateMessage = (id: string, patch: Partial<ChatMessage>) =>
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  const clearMessages = () => setMessages([]);
  return (
    <ChatContext.Provider
      value={{ input, setInput, busy, setBusy, sending, pending, setPending, followAnswers, setFollowAnswers, messages, appendMessage, updateMessage, clearMessages, justLoggedId, setJustLoggedId, turnState, setTurnState }}
    >
      {children}
    </ChatContext.Provider>
  );
}

export function useChat(): ChatState {
  const state = useContext(ChatContext);
  if (!state) throw new Error('useChat must be used within ChatProvider');
  return state;
}
