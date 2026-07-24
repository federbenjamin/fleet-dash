Needs-you QUESTION card for the Now queue. LOCKED: shows only the question + "N options · recommended: X — ANSWER IN PANE →". Never render option selectors on this card — answering happens in QuestionDrawer.

```jsx
<QuestionCard session="design-probe — deploy order" meta="fleet-dash · main · claude"
  question="Which deployment target should the release script promote first?"
  optionCount={3} recommended="Staging first" quiet="quiet 2m" onOpen={openPane} />
```