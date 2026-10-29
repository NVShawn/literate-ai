
function evaluate(check) {
  if (check === 'shell') return PAGE.startsWith('<!doctype html>') && PAGE.includes('<canvas') && PAGE.includes('data-controls');
  if (check === 'syntax') {
    for (const m of PAGE.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/gi)) {
      if (!m[1].includes('application/json')) new (require('node:vm').Script)(m[2]);
    }
    return true;
  }
  if (check === 'offline') return !/<script[^>]+src=/i.test(PAGE) && !/<link[^>]+href=["']https?:/i.test(PAGE);
  throw new Error('Unknown check: ' + check);
}
function testAll() {
  return {schema: 'literate-ai/generated-test-results@1', cases: ['shell', 'syntax', 'offline'].map(id => {
    if (!evaluate(id)) throw new Error(id + ' failed');
    return {case_id: 'lab-' + id, outcome: 'passed'};
  })};
}
