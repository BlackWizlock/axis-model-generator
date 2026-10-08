import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {Sha256, hashSlices, BLOCK_BYTES} from '../src/sha256.js';
for (const value of ['', 'abc', 'a'.repeat(1_000_000)]) {
  test(`SHA256 known vector length ${value.length}`, () => {
    const bytes = new TextEncoder().encode(value);
    const hash = new Sha256();
    for (let start = 0; start < bytes.length; start += 137) hash.update(bytes.subarray(start, start + 137));
    assert.equal(hash.digestHex(), createHash('sha256').update(bytes).digest('hex'));
  });
}
test('random chunk boundaries and digest padding', () => {
  for (const length of [1, 55, 56, 63, 64, 65, 511, 4097]) {
    const bytes = Uint8Array.from({length}, (_, n) => (n * 71 + 17) % 256);
    const hash = new Sha256();
    for (let n = 0; n < length; n += 17) hash.update(bytes.subarray(n, n + 17));
    assert.equal(hash.digestHex(), createHash('sha256').update(bytes).digest('hex'));
  }
});
test('file hashing reads only bounded slices and reports hashed bytes', async () => {
  const source = new Uint8Array(BLOCK_BYTES + 13).fill(99); const calls = [];
  const file = {size: source.length, arrayBuffer() {throw new Error('whole file prohibited');},
    slice(start, end) {calls.push(end - start); return new Blob([source.subarray(start, end)]);}};
  const progress = [];
  assert.equal(await hashSlices(file, (done) => progress.push(done)), createHash('sha256').update(source).digest('hex'));
  assert.ok(calls.every(n => n <= BLOCK_BYTES)); assert.equal(progress.at(-1), file.size);
});
test('hash cancellation stops before next slice', async () => {
  const controller = new AbortController(); const file = new Blob([new Uint8Array(BLOCK_BYTES + 1)]);
  await assert.rejects(hashSlices(file, () => controller.abort(), controller.signal), {name: 'AbortError'});
});
