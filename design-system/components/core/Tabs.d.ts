export interface TabsProps {
  /** UPPERCASE mono labels, e.g. ['CHAT','FILES','AGENTS','DETAILS'] */
  items: string[];
  active: string;
  onSelect?: (item: string) => void;
  style?: React.CSSProperties;
}
export declare function Tabs(props: TabsProps): JSX.Element;
