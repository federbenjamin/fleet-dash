Workspace section tabs — active gets fg text + 2px amber underline (the only 2px border in the system).

```jsx
<Tabs items={['CHAT','FILES','AGENTS','DETAILS']} active="CHAT" onSelect={setTab} />
```