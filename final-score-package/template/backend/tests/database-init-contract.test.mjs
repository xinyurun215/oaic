import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { describe, it, beforeEach, afterEach } from 'vitest';
const require = createRequire(import.meta.url);
const database = require('../src/database/init_db');
const runtime = require('../src/database/db_runtime');
const { createTestDatabaseHarness } = require('../src/database/test_harness');

describe('database initialization contract', () => {
  let directory;
  beforeEach(async () => {
    directory = fs.mkdtempSync(path.join(os.tmpdir(), 'oaic-db-contract-'));
    await database.setDbPath(path.join(directory, 'probe.db'));
  });
  afterEach(async () => {
    try { await database.closeDb(); }
    finally { fs.rmSync(directory, { recursive: true, force: true }); }
  });

  it('returns the connection on sequential initialization calls', async () => {
    const first = await database.initializeDatabase();
    const second = await database.initializeDatabase();
    assert.equal(typeof first.run, 'function');
    assert.strictEqual(second, first);
    assert.equal(typeof second.all, 'function');
  });

  it('default e2e preparation preserves the runtime database sentinel', async () => {
    await database.setDbPath(path.join(directory, 'database.db'));
    await runtime.exec('CREATE TABLE sentinel (value TEXT)');
    await runtime.run('INSERT INTO sentinel VALUES (?)', ['preserve-me']);
    await database.closeDb();
    const environment = { ...process.env };
    delete environment.ARC_DB_FILE;
    delete environment.DATABASE_FILE;
    const prepared = spawnSync(process.execPath,
      [require.resolve('../src/database/prepare_e2e')],
      { cwd: directory, env: environment, encoding: 'utf8' });
    assert.equal(prepared.status, 1, 'implicit test database path must be rejected');
    assert.match(prepared.stderr, /db-path/);
    assert.deepEqual(await runtime.all("SELECT name FROM sqlite_master WHERE name='sentinel'"),
      [{ name: 'sentinel' }], 'default preparation deleted the runtime sentinel table');
    assert.deepEqual(await runtime.all('SELECT value FROM sentinel'), [{ value: 'preserve-me' }]);
  });

  it('public writes survive another transaction rollback', async () => {
    await runtime.exec('CREATE TABLE mixed (value TEXT)');
    let entered;
    let release;
    const ready = new Promise((resolve) => { entered = resolve; });
    const gate = new Promise((resolve) => { release = resolve; });
    const rollback = runtime.withTransaction(async (tx) => {
      await tx.run('INSERT INTO mixed VALUES (?)', ['transaction-A']);
      entered();
      await gate;
      throw new Error('intentional rollback');
    });
    const rejected = assert.rejects(rollback, /intentional rollback/);
    await ready;
    const outside = runtime.run('INSERT INTO mixed VALUES (?)', ['outside-B']);
    try { await new Promise((resolve) => setTimeout(resolve, 30)); }
    finally { release(); }
    await rejected;
    assert.equal((await outside).changes, 1);
    assert.deepEqual(await runtime.all('SELECT value FROM mixed'), [{ value: 'outside-B' }],
      'successful public write was rolled back by another transaction');
  });

  it('rejects public calls and nested transactions inside a transaction immediately', async () => {
    await runtime.withTransaction(async (tx) => {
      await tx.exec('CREATE TABLE nested (value TEXT)');
      await assert.rejects(runtime.run('INSERT INTO nested VALUES (?)', ['bad']), /supplied tx/);
      await assert.rejects(runtime.get('SELECT * FROM nested'), /supplied tx/);
      await assert.rejects(runtime.all('SELECT * FROM nested'), /supplied tx/);
      await assert.rejects(runtime.exec('DELETE FROM nested'), /supplied tx/);
      await assert.rejects(runtime.withTransaction(async () => {}), /supplied tx/);
      await tx.run('INSERT INTO nested VALUES (?)', ['kept']);
    });
    assert.deepEqual(await runtime.all('SELECT value FROM nested'), [{ value: 'kept' }]);
  });

  it('returns the same connection to concurrent callers', async () => {
    const connections = await Promise.all(
      Array.from({ length: 8 }, () => database.initializeDatabase()),
    );
    for (const connection of connections) {
      assert.strictEqual(connection, connections[0]);
      assert.equal(typeof connection.run, 'function');
    }
  });

  it('supports real reads and writes after startup initialization', async () => {
    await database.initializeDatabase();
    await runtime.exec('CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)');
    const inserted = await runtime.run('INSERT INTO probe(value) VALUES (?)', ['first']);
    assert.equal(inserted.changes, 1);
    assert.deepEqual(await runtime.get('SELECT value FROM probe WHERE id=?', inserted.lastID),
      { value: 'first' });
    await runtime.run('UPDATE probe SET value=? WHERE id=?', ['updated', inserted.lastID]);
    assert.deepEqual(await runtime.all('SELECT value FROM probe'), [{ value: 'updated' }]);
    assert.equal((await runtime.run('DELETE FROM probe WHERE id=?', inserted.lastID)).changes, 1);
    assert.deepEqual(await runtime.all('SELECT * FROM probe'), []);
  });

  it('commits successful transactions and rolls back rejected ones', async () => {
    await database.initializeDatabase();
    await runtime.exec('CREATE TABLE probe (value TEXT)');
    const value = await runtime.withTransaction(async (tx) => {
      await tx.run('INSERT INTO probe VALUES (?)', ['kept']);
      return 'committed';
    });
    assert.equal(value, 'committed');
    await assert.rejects(runtime.withTransaction(async (tx) => {
      await tx.run('INSERT INTO probe VALUES (?)', ['discarded']);
      throw new Error('rollback probe');
    }), /rollback probe/);
    assert.deepEqual(await runtime.all('SELECT value FROM probe'), [{ value: 'kept' }]);
  });

  it('serializes concurrent transactions on the shared connection', async () => {
    await runtime.exec('CREATE TABLE probe (value TEXT)');
    let releaseFirst;
    let firstStarted;
    const firstReady = new Promise((resolve) => { firstStarted = resolve; });
    const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
    const first = runtime.withTransaction(async (tx) => {
      await tx.run('INSERT INTO probe VALUES (?)', ['first']);
      firstStarted();
      await firstGate;
      return 'first result';
    });
    await firstReady;
    const second = runtime.withTransaction(async (tx) => {
      await tx.run('INSERT INTO probe VALUES (?)', ['second']);
      return 'second result';
    });
    try {
      await new Promise((resolve) => setTimeout(resolve, 30));
    } finally { releaseFirst(); }
    assert.deepEqual(await Promise.all([first, second]), ['first result', 'second result']);
    assert.deepEqual(await runtime.all('SELECT value FROM probe'),
      [{ value: 'first' }, { value: 'second' }]);
  });

  it('keeps a failed transaction from poisoning the next queued transaction', async () => {
    await runtime.exec('CREATE TABLE probe (value TEXT)');
    const failure = new Error('first failed');
    let releaseFirst;
    let firstStarted;
    const firstReady = new Promise((resolve) => { firstStarted = resolve; });
    const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
    const first = runtime.withTransaction(async (tx) => {
      await tx.run('INSERT INTO probe VALUES (?)', ['rolled back']);
      firstStarted();
      await firstGate;
      throw failure;
    });
    await firstReady;
    const second = runtime.withTransaction(async (tx) => {
      await tx.run('INSERT INTO probe VALUES (?)', ['committed']);
      return 'next result';
    });
    try {
      await new Promise((resolve) => setTimeout(resolve, 30));
    } finally { releaseFirst(); }
    await assert.rejects(first, (error) => error === failure);
    assert.equal(await second, 'next result');
    assert.deepEqual(await runtime.all('SELECT value FROM probe'), [{ value: 'committed' }]);
  });

  it('reopens the file after close and preserves committed data', async () => {
    const first = await database.initializeDatabase();
    await runtime.exec('CREATE TABLE probe (value TEXT)');
    await runtime.run('INSERT INTO probe VALUES (?)', ['saved']);
    await database.closeDb();
    const reopened = await database.initializeDatabase();
    assert.notStrictEqual(reopened, first);
    assert.strictEqual(await database.initializeDatabase(), reopened);
    assert.deepEqual(await runtime.all('SELECT value FROM probe'), [{ value: 'saved' }]);
  });

  it('rejects an initialization failure and permits a later retry', async () => {
    const connection = database.getDb();
    const originalRun = connection.run;
    let failNext = true;
    connection.run = function (sql, ...args) {
      if (failNext && sql === 'PRAGMA foreign_keys = ON;') {
        failNext = false;
        const callback = args[args.length - 1];
        queueMicrotask(() => callback(new Error('injected initialization failure')));
        return this;
      }
      return originalRun.call(this, sql, ...args);
    };
    try {
      await assert.rejects(database.initializeDatabase(), /injected initialization failure/);
      connection.run = originalRun;
      assert.strictEqual(await database.initializeDatabase(), connection);
      assert.strictEqual(await database.initializeDatabase(), connection);
    } finally { connection.run = originalRun; }
  });

  it('keeps the existing harness setup and reset usable', async () => {
    const harness = createTestDatabaseHarness({ dbPath: path.join(directory, 'harness.db') });
    const seed = async (db) => {
      await db.exec('CREATE TABLE probe (value TEXT)');
      await db.run('INSERT INTO probe VALUES (?)', ['seed']);
    };
    try {
      const db = await harness.setup();
      await harness.seed(seed);
      assert.deepEqual(await db.all('SELECT value FROM probe'), [{ value: 'seed' }]);
      await db.run('INSERT INTO probe VALUES (?)', ['extra']);
      await harness.reset(seed);
      assert.deepEqual(await db.all('SELECT value FROM probe'), [{ value: 'seed' }]);
    } finally { await harness.cleanup(); }
  });
});
