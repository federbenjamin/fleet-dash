export interface SectionHeaderProps {
  /** UPPERCASE, e.g. "NEEDS YOU" */
  label: string;
  count?: number;
  /** amber | green | red | 'dim' (no dot) */
  tone?: 'amber' | 'green' | 'red' | 'dim';
  style?: React.CSSProperties;
}
export declare function SectionHeader(props: SectionHeaderProps): JSX.Element;
