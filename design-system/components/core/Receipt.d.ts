export interface ReceiptProps {
  /** delivered | sending (spinner) | queued | offline (amber) */
  state?: 'delivered' | 'sending' | 'queued' | 'offline';
  /** override the default copy */
  text?: string;
  style?: React.CSSProperties;
}
export declare function Receipt(props: ReceiptProps): JSX.Element;
