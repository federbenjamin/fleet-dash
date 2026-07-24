/** @startingPoint section="Session" subtitle="Desktop meta-rail card / mobile inline-status card" viewport="620x170" */
export interface SessionCardProps {
  title: string;
  /** lowercase mono, · separated: "quirk · refactor/offset-op-lock" */
  repo?: string;
  /** ~5-line conversation peek (max 800 chars) */
  peek?: string;
  /** working | needs | stalled | paused | limit | viewonly | closed */
  status?: string;
  /** model name — renders purple */
  model?: string;
  /** context-used 0–100 */
  ctx?: number;
  /** e.g. "7s", "6m 12s" */
  quiet?: string;
  agentCount?: number;
  /** amber selection ring */
  selected?: boolean;
  pinned?: boolean;
  /** inline status row instead of the 118px meta rail */
  mobile?: boolean;
  onClick?: () => void;
  /** AgentRow tree, action buttons, etc. */
  children?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function SessionCard(props: SessionCardProps): JSX.Element;
