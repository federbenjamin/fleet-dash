export interface AgentRowProps {
  /** agent type, e.g. "explore", "test-runner" */
  name: string;
  /** never truncated */
  desc: string;
  /** "haiku · low" — renders purple, right-aligned */
  model?: string;
  tone?: 'green' | 'amber' | 'red';
  /** nested AgentRows (spawn-order tree) */
  children?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function AgentRow(props: AgentRowProps): JSX.Element;
