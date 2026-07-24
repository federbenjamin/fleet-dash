/** @startingPoint section="Workspace" subtitle="The canonical answer surface — resizable amber drawer" viewport="620x260" */
export interface QuestionDrawerOption { label: string; desc?: string; rec?: boolean; }
export interface QuestionDrawerProps {
  /** UPPERCASE amber header, e.g. "DEPLOY ORDER — WAITING ON YOU" */
  title: string;
  question: string;
  options?: QuestionDrawerOption[];
  /** e.g. "1 OF 1" */
  page?: string;
  onPick?: (o: QuestionDrawerOption) => void;
  onOther?: () => void;
  collapsed?: boolean;
  onToggle?: () => void;
  /** chosen label — renders the answered receipt instead of options */
  answered?: string;
  style?: React.CSSProperties;
}
export declare function QuestionDrawer(props: QuestionDrawerProps): JSX.Element;
