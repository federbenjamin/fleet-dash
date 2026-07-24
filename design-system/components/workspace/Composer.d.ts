/** @startingPoint section="Workspace" subtitle="＋ | input | Send — Cmd-Return sends" viewport="560x70" */
export interface ComposerProps {
  value?: string;
  onChange?: (v: string) => void;
  onSend?: () => void;
  placeholder?: string;
  /** read-only surfaces show no send controls — hide instead where possible */
  disabled?: boolean;
  style?: React.CSSProperties;
}
export declare function Composer(props: ComposerProps): JSX.Element;
