import test from 'node:test';
import assert from 'node:assert/strict';
import {api} from '../../frontend/js/api.js';

test('a lost HTTP response retries the same command without a second identity', async () => {
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (path, init) => {
    requests.push({path, body: JSON.parse(init.body)});
    if (requests.length === 1) throw new TypeError('connection closed after server accepted command');
    return {ok: true, json: async () => ({handled_as: 'answer'})};
  };
  try {
    assert.deepEqual(await api.chatMessage('chat', 'Покажи три варианта'), {handled_as: 'answer'});
    assert.equal(requests.length, 2);
    assert.ok(requests[0].body.command_id);
    assert.deepEqual(requests[0], requests[1]);
  } finally {globalThis.fetch = original;}
});

test('a rejected decision is reported once and is not retried as a new action', async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return {ok: false, statusText: 'Conflict', json: async () => ({detail: 'Карточка устарела'})};
  };
  try {
    await assert.rejects(api.confirm('chat', 'price', true, {}, 'old-card'), /Карточка устарела/);
    assert.equal(calls, 1);
  } finally {globalThis.fetch = original;}
});
