export interface ChatMessageProps {
  /** user (right bubble) | agent (left, AUTHOR eyebrow) | tool (mono dim row) */
  role?: 'user' | 'agent' | 'tool';
  /** eyebrow label, default CLAUDE */
  author?: string;
  /** delivery receipt under a user message */
  receipt?: 'delivered' | 'sending' | 'queued' | 'offline';
  children?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function ChatMessage(props: ChatMessageProps): JSX.Element;
