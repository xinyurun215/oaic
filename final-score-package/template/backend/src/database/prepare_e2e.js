const { parseArgs } = require('node:util');
const { createRuntimeDatabaseHarness } = require('./test_harness');

async function prepareE2EDatabase(options = {}) {
  const harness = createRuntimeDatabaseHarness({
    dbPath: options.dbPath,
    label: options.label || process.env.ARC_E2E_DB_LABEL || 'playwright-e2e',
    seedDefault: options.seedDefault !== false,
  });

  try {
    const runtime = await harness.setup();
    return { dbPath: runtime.dbPath, seeded: options.seedDefault !== false };
  } finally { await harness.cleanup(); }
}

if (require.main === module) {
  const { values } = parseArgs({ options: { 'db-path': { type: 'string' } } });
  prepareE2EDatabase({ dbPath: values['db-path'] })
    .then((result) => {
      console.log(`E2E database prepared at: ${result.dbPath}`);
      console.log(`Seed default: ${result.seeded ? 'yes' : 'no'}`);
    })
    .catch((error) => {
      console.error('E2E database preparation failed:', error);
      process.exitCode = 1;
    });
}

module.exports = prepareE2EDatabase;
module.exports.prepareE2EDatabase = prepareE2EDatabase;
