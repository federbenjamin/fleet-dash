export interface QuestionCardProps {
  /** session title */
  session: string;
  /** lowercase repo · branch line */
  meta?: string;
  /** UPPERCASE, default "QUESTION WAITING" */
  heading?: string;
  question: string;
  optionCount: number;
  /** recommended option label */
  recommended: string;
  /** e.g. "quiet 2m" */
  quiet?: string;
  onOpen?: () => void;
  style?: React.CSSProperties;
}
export declare function QuestionCard(props: QuestionCardProps): JSX.Element;
