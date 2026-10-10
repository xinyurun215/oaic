const { getDb, initializeDatabase } = require('./init_db');
const { AsyncLocalStorage } = require('node:async_hooks');
const transactionContext = new AsyncLocalStorage();
let operationTail = Promise.resolve();
function enqueue(operation) {
  if (transactionContext.getStore()) {
    return Promise.reject(new Error('Use the supplied tx inside withTransaction'));
  }
  const result = operationTail.then(operation);
  operationTail = result.then(() => undefined, () => undefined);
  return result;
}

function normalizeParams(params) {
  if (Array.isArray(params)) {
    return params;
  }
  if (params === undefined) {
    return [];
  }
  return [params];
}

function runOnDatabase(database, sql, params = []) {
  return new Promise((resolve, reject) => {
    database.run(sql, normalizeParams(params), function handleRun(err) {
      if (err) {
        reject(err);
        return;
      }
      resolve({
        lastID: this.lastID,
        changes: this.changes,
      });
    });
  });
}

function getOnDatabase(database, sql, params = []) {
  return new Promise((resolve, reject) => {
    database.get(sql, normalizeParams(params), (err, row) => {
      if (err) {
        reject(err);
        return;
      }
      resolve(row || null);
    });
  });
}

function allOnDatabase(database, sql, params = []) {
  return new Promise((resolve, reject) => {
    database.all(sql, normalizeParams(params), (err, rows) => {
      if (err) {
        reject(err);
        return;
      }
      resolve(rows || []);
    });
  });
}

function execOnDatabase(database, sql) {
  return new Promise((resolve, reject) => {
    database.exec(sql, (err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

function run(sql, params = []) {
  return enqueue(async () => runOnDatabase(await initializeDatabase(), sql, params));
}

function get(sql, params = []) {
  return enqueue(async () => getOnDatabase(await initializeDatabase(), sql, params));
}

function all(sql, params = []) {
  return enqueue(async () => allOnDatabase(await initializeDatabase(), sql, params));
}

function exec(sql) {
  return enqueue(async () => execOnDatabase(await initializeDatabase(), sql));
}

function withTransaction(work) {
  return enqueue(async () => {
    const database = await initializeDatabase();
    await execOnDatabase(database, 'BEGIN IMMEDIATE TRANSACTION;');

    const tx = {
      run: (sql, params = []) => runOnDatabase(database, sql, params),
      get: (sql, params = []) => getOnDatabase(database, sql, params),
      all: (sql, params = []) => allOnDatabase(database, sql, params),
      exec: (sql) => execOnDatabase(database, sql),
    };

    try {
      const result = await transactionContext.run(true, () => work(tx));
      await execOnDatabase(database, 'COMMIT;');
      return result;
    } catch (error) {
      try {
        await execOnDatabase(database, 'ROLLBACK;');
      } catch (rollbackError) {
        error.rollbackError = rollbackError;
      }
      throw error;
    }
  });
}

module.exports = {
  run,
  get,
  all,
  exec,
  withTransaction,
};
