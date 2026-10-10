import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { describe, it } from 'vitest';

const source = fs.readFileSync(new URL('../src/index.js', import.meta.url), 'utf8');

async function startup(seedFailure) {
  const events = [];
  const process = { env: {}, exitCode: 0 };
  vm.runInNewContext(source, {
    require(name) {
      if (name === './app') return { listen() { events.push('listen'); } };
      if (name === './database/init_db') return {
        async initializeDatabase() { events.push('schema'); },
        async closeDb() { events.push('close'); },
      };
      if (name === './database/seed_db') return {
        async seedDatabase() { events.push('seed'); if (seedFailure) throw seedFailure; },
      };
      throw new Error(`Unexpected import: ${name}`);
    },
    process,
    console: { error(...args) { events.push(args); }, log() {} },
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { events, process };
}

describe('production startup seed hook', () => {
  it('initializes schema then seeds before listening', async () => {
    assert.deepEqual((await startup()).events, ['schema', 'seed', 'listen']);
  });
  it('preserves the seed failure and never listens', async () => {
    const failure = new Error('seed marker');
    const result = await startup(failure);
    assert.deepEqual(result.events.slice(0, 2), ['schema', 'seed']);
    assert.strictEqual(result.events[2][1], failure);
    assert.equal(result.events[3], 'close');
    assert.equal(result.process.exitCode, 1);
    assert.ok(!result.events.includes('listen'));
  });
});
