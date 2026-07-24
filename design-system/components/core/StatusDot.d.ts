export interface StatusDotProps {
  tone?: 'amber' | 'green' | 'red' | 'blue' | 'purple' | 'dim';
  /** px; 7 section headers, 6 inline */
  size?: number;
  style?: React.CSSProperties;
}
export declare function StatusDot(props: StatusDotProps): JSX.Element;
