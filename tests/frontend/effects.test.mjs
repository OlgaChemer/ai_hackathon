import test from 'node:test';
import assert from 'node:assert/strict';
import {EffectQueue} from '../../frontend/js/effects.js';

test('slow speech does not block state application and queued effects remain ordered', async () => {
  const q = new EffectQueue(); const heard = []; let finish;
  q.enqueue(async () => { heard.push('first'); await new Promise(r => {finish = r;}); });
  let rendered = false;
  q.enqueue(() => {heard.push('second');});
  rendered = true;
  assert.equal(rendered,true); assert.deepEqual(heard,['first']);
  finish(); await new Promise(r => setImmediate(r));
  assert.deepEqual(heard,['first','second']);
});

test('switching dialogue starts a new effect stream while old speech is stalled', async () => {
  const q = new EffectQueue(); let heard = false;
  q.enqueue(() => new Promise(() => {}));
  q.reset(); q.enqueue(() => {heard = true;});
  await new Promise(r => setImmediate(r)); assert.equal(heard,true);
});
