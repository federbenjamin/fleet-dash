export interface IconButtonProps {
  /** unicode glyph: ＋ ‹ › ⤡ ✕ ⋮ ▾ — never an SVG icon */
  glyph: string;
  /** px square; 30 desktop chrome, 34 composer ＋, 44 mobile */
  size?: number;
  /** amber border + amber glyph */
  active?: boolean;
  onClick?: () => void;
  style?: React.CSSProperties;
}
export declare function IconButton(props: IconButtonProps): JSX.Element;
