The universal shell's fixed 136px nav rail: F-tile wordmark, mono nav items with amber badges, footer = per-window usage bars + signed-in user + daemon dot. History is NOT an item (it's Search TYPE=SESSION).

```jsx
<NavRail active="NOW"
  items={[{label:'NOW',badge:2},{label:'NOTIFICATIONS',badge:5},{label:'SEARCH'},{label:'WORKSTREAMS'},{label:'INSIGHTS'},{label:'SETTINGS'},{label:'＋ NEW SESSION'}]}
  usage={[{label:'CLAUDE 5H',pct:75},{label:'CLAUDE WK',pct:41},{label:'CODEX 5H',pct:1}]}
  user="jordan" daemonOk />
```