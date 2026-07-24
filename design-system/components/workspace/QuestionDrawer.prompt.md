The pending-question drawer — the ONLY place options are selectable. Sits above the composer; grip collapses; multi-question asks page with ‹ ›; answered shows a receipt until transcript-confirmed.

```jsx
<QuestionDrawer title="DEPLOY ORDER — WAITING ON YOU" page="1 OF 1"
  question="Which deployment target should the release script promote first?"
  options={[{label:'Staging first',desc:'verify API identity, then production',rec:true},{label:'Production directly'},{label:'Hold the release'}]}
  onPick={answer} />
```