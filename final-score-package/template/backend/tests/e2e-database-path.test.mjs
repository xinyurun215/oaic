import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { it } from 'vitest';
const require = createRequire(import.meta.url);
const entry = require.resolve('../src/database/prepare_e2e');

it('protects implicit, explicit default, environment and symlink runtime paths', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'oaic-e2e-path-'));
  try {
    const normal = path.join(directory, 'database.db');
    const alternate = path.join(directory, 'production.sqlite');
    fs.writeFileSync(normal, 'runtime sentinel');
    fs.writeFileSync(alternate, 'environment sentinel');
    const digest = () => createHash('sha256').update(fs.readFileSync(normal)).digest('hex');
    const original = digest();
    const env = { ...process.env, ARC_DB_FILE: alternate, DATABASE_FILE: alternate };
    for (const target of [null, normal, alternate]) {
      const result = spawnSync(process.execPath, [entry, ...(target ? ['--db-path', target] : [])],
        { cwd: directory, env, encoding: 'utf8' });
      assert.equal(result.status, 1, result.stdout);
      assert.equal(digest(), original);
      assert.equal(fs.readFileSync(alternate, 'utf8'), 'environment sentinel');
    }
    if (process.platform !== 'win32') {
      const alias = path.join(directory, 'alias.sqlite');
      fs.symlinkSync(normal, alias);
      assert.equal(spawnSync(process.execPath, [entry, '--db-path', alias], { cwd: directory, env }).status, 1);
      assert.equal(digest(), original);
    }
    const independent = path.join(directory, 'fresh-testing', 'independent.sqlite');
    for (let count = 0; count < 2; count++) {
      const result = spawnSync(process.execPath, [entry, '--db-path', independent],
        { cwd: directory, env, encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr);
      assert.ok(fs.existsSync(independent));
      assert.equal(digest(), original);
    }
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
