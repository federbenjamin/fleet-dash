Chat transcript message. Parent must be a column flex container (bubbles self-align).

```jsx
<ChatMessage role="user" receipt="delivered">Ship the release when the checks pass.</ChatMessage>
<ChatMessage role="agent">Checks are green. I need a decision on the deploy order.</ChatMessage>
<ChatMessage role="tool">▸ ran test:gates — exit 0 · 41s</ChatMessage>
```