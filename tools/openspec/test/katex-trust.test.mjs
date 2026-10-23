import assert from 'node:assert/strict';
import test from 'node:test';
import katex from 'katex';

test('math rendering rejects inherited trust and preserves explicit trust', () => {
  const expression = String.raw`\href{https://fixture.invalid/}{x}`;
  const inherited = katex.renderToString(expression, Object.create({ trust: true }));
  assert.doesNotMatch(inherited, /href="https:\/\/fixture\.invalid\//);
  const explicit = katex.renderToString(expression, { trust: true });
  assert.match(explicit, /href="https:\/\/fixture\.invalid\//);
});
