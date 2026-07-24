export interface ChipProps {
  /** filled dark surface + brighter border */
  active?: boolean;
  /** accent var name to color the label, e.g. 'purple' for model chips */
  tone?: 'amber' | 'green' | 'red' | 'purple' | 'blue';
  onClick?: () => void;
  children?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function Chip(props: ChipProps): JSX.Element;
