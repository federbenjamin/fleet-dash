/* Preview loader for design-system cards.
   Prefers the compiled _ds_bundle.js; if absent, fetches + transpiles the component
   sources with Babel standalone. Exposes window.__dsReady → Promise<namespace>. */
(() => {
  const self = document.currentScript.src;
  const root = self.slice(0, self.lastIndexOf('/') + 1);
  const MODULES = {
    'components/core/Button.jsx': 'Button', 'components/core/IconButton.jsx': 'IconButton',
    'components/core/Chip.jsx': 'Chip', 'components/core/SectionHeader.jsx': 'SectionHeader',
    'components/core/StatusDot.jsx': 'StatusDot', 'components/core/UsageBar.jsx': 'UsageBar',
    'components/core/Tabs.jsx': 'Tabs', 'components/core/Receipt.jsx': 'Receipt',
    'components/session/AgentRow.jsx': 'AgentRow', 'components/session/SessionCard.jsx': 'SessionCard',
    'components/session/QuestionCard.jsx': 'QuestionCard', 'components/session/ApprovalCard.jsx': 'ApprovalCard',
    'components/workspace/ChatMessage.jsx': 'ChatMessage', 'components/workspace/Composer.jsx': 'Composer',
    'components/workspace/QuestionDrawer.jsx': 'QuestionDrawer',
    'components/shell/NavRail.jsx': 'NavRail', 'components/shell/NotificationRow.jsx': 'NotificationRow',
  };
  const normalize = (from, spec) => {
    const parts = from.split('/').slice(0, -1).concat(spec.split('/'));
    const out = [];
    for (const p of parts) {
      if (p === '.' || p === '') continue;
      else if (p === '..') out.pop();
      else out.push(p);
    }
    return out.join('/');
  };
  window.__dsReady = (async () => {
    try {
      await new Promise((res, rej) => {
        const s = document.createElement('script');
        s.src = root + '_ds_bundle.js'; s.onload = res; s.onerror = rej;
        document.head.appendChild(s);
      });
      for (const k of Object.getOwnPropertyNames(window)) {
        try { const v = window[k]; if (v && typeof v === 'object' && typeof v.SessionCard === 'function' && typeof v.Button === 'function') return v; } catch (e) {}
      }
    } catch (e) { /* bundle not generated yet — fall back to sources */ }
    const srcs = {};
    await Promise.all(Object.keys(MODULES).map(async p => {
      const r = await fetch(root + p);
      if (!r.ok) throw new Error('missing ' + p);
      srcs[p] = await r.text();
    }));
    const cache = {};
    const load = path => {
      if (cache[path]) return cache[path].exports;
      const code = Babel.transform(srcs[path], { presets: [['react', { runtime: 'classic' }]], plugins: [['transform-modules-commonjs']] }).code;
      const mod = cache[path] = { exports: {} };
      const req = spec => spec === 'react' ? window.React : spec === 'react/jsx-runtime' ? window.ReactJSXRuntime || window.React : load(normalize(path, spec));
      new Function('require', 'module', 'exports', code)(req, mod, mod.exports);
      return mod.exports;
    };
    const ns = {};
    for (const p of Object.keys(MODULES)) Object.assign(ns, load(p));
    return (window.FleetDashDS = ns);
  })();
})();
