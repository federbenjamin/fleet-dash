export interface NotificationRowProps {
  title: string;
  /** right-aligned mono meta, e.g. "design-probe · 2m" */
  meta?: string;
  tone?: 'amber' | 'green' | 'red' | 'blue';
  unread?: boolean;
  /** shows the attached actions sub-card */
  open?: boolean;
  onTap?: () => void;
  onAction?: (action: string) => void;
  style?: React.CSSProperties;
}
export declare function NotificationRow(props: NotificationRowProps): JSX.Element;
