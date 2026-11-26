'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {createReadStream} = require('node:fs');
const {DatabaseSync, backup} = require('node:sqlite');
const {mods, defaultMod} = require('../mods.cjs');

const databasePaths = mods.filter(mod => !mod.readOnly).map(mod =>
  mod.id === defaultMod ? 'state.sqlite' : 'mods/' + mod.id + '.sqlite');
const assetName = /^[a-f0-9]{24}\.(?:png|jpg|webp|gif|svg)$/;
const hashString = /^[a-f0-9]{64}$/;

async function checksum(file) {
  const hash = crypto.createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

function inspect(file) {
  const db = new DatabaseSync(file, {readOnly:true});
  try {
    if (Object.values(db.prepare('PRAGMA quick_check').get())[0] !== 'ok') throw Error('SQLite integrity check failed');
    const revision = db.prepare("SELECT value FROM metadata WHERE key='revision'").get()?.value;
    if (revision === undefined) throw Error('Workbench database is uninitialized');
    const assets = db.prepare('SELECT DISTINCT filename FROM assets').all().map(row => row.filename);
    if (!assets.every(name => assetName.test(name))) throw Error('Invalid workbench asset name');
    return {revision:Number(revision), assets, history:db.prepare('SELECT COUNT(*) AS count FROM history').get().count,
      receipts:db.prepare('SELECT COUNT(*) AS count FROM receipts').get().count};
  } finally {
    db.close();
  }
}

async function createBackup({stateDirectory, assetDirectory}, target) {
  target = path.resolve(target);
  if (fs.lstatSync(target, {throwIfNoEntry:false})) throw Error('Backup destination already exists');
  fs.mkdirSync(path.dirname(target), {recursive:true, mode:0o700});
  const staging = fs.mkdtempSync(path.join(path.dirname(target), '.workbench-backup-'));
  try {
    const manifest = {version:1, databases:{}, assets:{}};
    const referenced = new Set();
    for (const relative of databasePaths) {
      const output = path.join(staging, relative);
      fs.mkdirSync(path.dirname(output), {recursive:true, mode:0o700});
      const source = new DatabaseSync(path.join(stateDirectory, relative), {readOnly:true});
      try { await backup(source, output); }
      finally { source.close(); }
      const exam = inspect(output);
      for (const name of exam.assets) referenced.add(name);
      manifest.databases[relative] = {revision:exam.revision, history:exam.history, receipts:exam.receipts,
        sha256:await checksum(output)};
    }
    fs.mkdirSync(path.join(staging, 'assets'), {mode:0o700});
    for (const name of [...referenced].sort()) {
      const source = path.join(assetDirectory, name);
      const output = path.join(staging, 'assets', name);
      fs.copyFileSync(source, output);
      const digest = await checksum(output);
      if (!digest.startsWith(name.split('.')[0])) throw Error('Workbench asset checksum mismatch');
      manifest.assets[name] = digest;
    }
    fs.writeFileSync(path.join(staging, 'manifest.json'), JSON.stringify(manifest, null, 2)+'\n', {mode:0o600});
    fs.renameSync(staging, target);
    return manifest;
  } finally {
    if (fs.lstatSync(staging, {throwIfNoEntry:false})?.isDirectory()) fs.rmSync(staging, {recursive:true});
  }
}

async function verifyBackup(directory) {
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'manifest.json'), 'utf8'));
  if (manifest.version !== 1 || Object.keys(manifest.databases).length !== databasePaths.length) {
    throw Error('Invalid workbench backup manifest');
  }
  const assets = new Set();
  for (const relative of databasePaths) {
    const evidence = manifest.databases[relative];
    const file = path.join(directory, relative);
    if (!evidence || await checksum(file) !== evidence.sha256) throw Error('Workbench database checksum mismatch');
    const exam = inspect(file);
    for (const key of ['revision','history','receipts']) {
      if (exam[key] !== evidence[key]) throw Error('Workbench backup metadata mismatch');
    }
    for (const name of exam.assets) assets.add(name);
  }
  if (assets.size !== Object.keys(manifest.assets).length) throw Error('Workbench asset references do not match');
  for (const name of assets) {
    const digest = manifest.assets[name];
    if (!hashString.test(digest || '') || !digest.startsWith(name.split('.')[0]) ||
        await checksum(path.join(directory, 'assets', name)) !== digest) {
      throw Error('Workbench asset checksum mismatch');
    }
  }
  return manifest;
}

async function main() {
  const [command, directory] = process.argv.slice(2);
  if (command === 'verify' && directory) {
    const manifest = await verifyBackup(directory);
    console.log('Verified workbench backup:', Object.keys(manifest.databases).length, 'workspaces');
    return;
  }
  if (command !== 'create' || !directory) throw Error('Usage: node backup.cjs create <new-directory> | verify <backup-directory>');
  const config = JSON.parse(fs.readFileSync(path.join(__dirname, 'relay.config.json'), 'utf8'));
  await createBackup({stateDirectory:config.stateDirectory || path.dirname(config.backendConfig),
    assetDirectory:path.join(config.webDirectory || '/var/www/junna', 'assets')}, directory);
  console.log('Created workbench backup');
}

if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode=1; });
module.exports = {createBackup, verifyBackup};
