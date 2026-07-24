/** @startingPoint section="Core" subtitle="Primary, secondary, amber, danger buttons" viewport="360x60" */
export interface ButtonProps {
  /** 'primary' inverted fg-on-bg · 'secondary' hairline (default) · 'amber' needs-you · 'danger' red */
  variant?: 'primary' | 'secondary' | 'amber' | 'danger';
  /** 6x12 padding, 11.5px */
  small?: boolean;
  /** full-width (e.g. the single wide "Spawn session" button) */
  wide?: boolean;
  disabled?: boolean;
  onClick?: () => void;
  children?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function Button(props: ButtonProps): JSX.Element;
