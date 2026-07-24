/** @startingPoint section="Shell" subtitle="136px rail with usage footer" viewport="160x480" */
export interface NavRailItem { label: string; badge?: number | string; }
export interface NavRailUsage { label: string; pct: number; }
export interface NavRailProps {
  /** NOW, NOTIFICATIONS, SEARCH, WORKSTREAMS, INSIGHTS, SETTINGS, ＋ NEW SESSION — no HISTORY */
  items?: NavRailItem[];
  active?: string;
  onSelect?: (label: string) => void;
  /** per-window usage rows; amber ≥70, red ≥90 */
  usage?: NavRailUsage[];
  user?: string;
  daemonOk?: boolean;
  style?: React.CSSProperties;
}
export declare function NavRail(props: NavRailProps): JSX.Element;
