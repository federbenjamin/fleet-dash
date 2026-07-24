export interface UsageBarProps {
  /** 0–100 */
  pct?: number;
  /** override auto threshold color */
  color?: string;
  height?: number;
  style?: React.CSSProperties;
}
export declare function UsageBar(props: UsageBarProps): JSX.Element;
