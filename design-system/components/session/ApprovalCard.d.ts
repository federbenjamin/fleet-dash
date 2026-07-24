export interface ApprovalCardProps {
  session: string;
  meta?: string;
  /** the command awaiting permission, mono block */
  command: string;
  quiet?: string;
  onApproveOnce?: () => void;
  onApproveSession?: () => void;
  onDeny?: () => void;
  style?: React.CSSProperties;
}
export declare function ApprovalCard(props: ApprovalCardProps): JSX.Element;
