const app = require('./app');
const { initializeDatabase, closeDb } = require('./database/init_db');
const { seedDatabase } = require('./database/seed_db');
const defaultPort = 3000;
const port = Number(process.env.PORT || defaultPort);

async function start() {
  await initializeDatabase();
  await seedDatabase();
  app.listen(port, '0.0.0.0', () => {
    console.log(`Backend listening on port ${port}`);
  });
}

start().catch(async (error) => {
  console.error('Database startup failed:', error);
  try {
    await closeDb();
  } finally {
    process.exitCode = 1;
  }
});
