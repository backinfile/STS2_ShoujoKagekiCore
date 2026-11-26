'use strict';
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const crypto = require('node:crypto');
const {mods,defaultMod} = require('../mods.cjs');
const {createRegistry} = require('../workspace-registry.cjs');

const releaseName = /^shoujo-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;

function linkedDirectory(link) {
  try { return fs.realpathSync(link); }
  catch (error) { if (error.code !== 'ENOENT') throw error; return null; }
}

function switchLink(link, target) {
  const temporary = link + '.' + crypto.randomUUID();
  fs.symlinkSync(target, temporary, 'junction');
  try { fs.renameSync(temporary, link); }
  finally { if (fs.lstatSync(temporary, {throwIfNoEntry:false})) fs.unlinkSync(temporary); }
}

function removeRelease(root, directory) {
  const safeRoot = fs.realpathSync(root);
  if (!releaseName.test(path.basename(directory)) ||
      fs.realpathSync(path.dirname(directory)) !== safeRoot ||
      !fs.lstatSync(directory, {throwIfNoEntry:false})?.isDirectory() ||
      fs.realpathSync(directory) !== path.join(safeRoot, path.basename(directory))) {
    return;
  }
  fs.rmSync(directory, {recursive:true});
}

function createPublisher({stateDirectory='/var/lib/junna-workbench', web='/var/www/junna',
    templateDirectory=__dirname, now=Date.now}={}) {
  const registry = createRegistry({stateDirectory, assetDirectory:path.join(web,'assets')});
  const root = path.join(web, 'releases');
  const current = path.join(web, 'current');
  const previous = path.join(web, 'previous');
  let published = null;
  let pending = null;

  function publish() {
    const key = JSON.stringify(registry.list().map(mod => [mod.id, mod.revision]));
    if (key === published) { pending = null; return false; }
    if (published !== null) {
      const time = now();
      if (!pending || key !== pending.key) pending = {key, since:time, first:pending?.first ?? time};
      if (time - pending.since < 1000 && time - pending.first < 5000) return false;
    }

    const release = path.join(root, 'shoujo-' + crypto.randomUUID());
    const old = linkedDirectory(current);
    fs.mkdirSync(release, {recursive:true});
    try {
      fs.symlinkSync(path.resolve(web,'assets'), path.join(release,'assets'), 'junction');
      const exportedAt = new Date(now()).toISOString();
      const summary = {exportedAt, mods:[]};
      fs.writeFileSync(path.join(release, '.workbench-release'), '1\n');
      for (const mod of mods) {
        const relative = mod.id === defaultMod ? '' : path.join('mods',mod.id);
        const directory = path.join(release, relative);
        const snapshot = registry.get(mod.id).state({assetURLs:true});
        const data = JSON.stringify({...snapshot.bundle, versions:snapshot.versions, exportedAt}).replace(/</g,'\\u003c');
        const html = fs.readFileSync(path.join(templateDirectory, relative,'public-template.html'),'utf8');
        if (!html.trim()) throw Error('Empty public template for ' + mod.id);
        fs.mkdirSync(directory, {recursive:true});
        for (const [name, content] of [['index.html',html], ['snapshot.json',data]]) {
          fs.writeFileSync(path.join(directory, name), content);
          fs.writeFileSync(path.join(directory, name+'.gz'), zlib.gzipSync(content));
        }
        const info = {id:mod.id, revision:snapshot.revision, cards:snapshot.bundle.data.cards.length, assets:snapshot.assetIds.length};
        fs.writeFileSync(path.join(directory,'manifest.json'), JSON.stringify({...info,exportedAt})+'\n');
        summary.mods.push(info);
      }
      fs.writeFileSync(path.join(release,'workspaces.json'), JSON.stringify(summary)+'\n');
      if (old) switchLink(previous, old);
      switchLink(current, path.resolve(release));
      published = JSON.stringify(summary.mods.map(mod => [mod.id, mod.revision]));
      pending = null;
      console.log('Published Shoujo workspaces ' + published);
    } catch (error) {
      if (linkedDirectory(current) !== fs.realpathSync(release)) removeRelease(root, release);
      throw error;
    }

    const keep = new Set([linkedDirectory(current), linkedDirectory(previous)]);
    for (const entry of fs.readdirSync(root, {withFileTypes:true})) {
      const directory = path.join(root, entry.name);
      if (entry.isDirectory() && releaseName.test(entry.name) && !keep.has(fs.realpathSync(directory)) &&
          fs.existsSync(path.join(directory,'.workbench-release'))) {
        removeRelease(root, directory);
      }
    }
    return true;
  }

  return {publish, close:() => registry.close()};
}

function start() {
  const config = JSON.parse(fs.readFileSync(path.join(__dirname,'relay.config.json'),'utf8'));
  const publisher = createPublisher({stateDirectory:config.stateDirectory || path.dirname(config.backendConfig),
    web:config.webDirectory || '/var/www/junna'});
  const tick = () => {
    try { publisher.publish(); }
    catch (error) { console.error('Workspaces publish failed:', error.message); }
  };
  tick();
  const timer = setInterval(tick, 1000);
  process.on('SIGTERM', () => { clearInterval(timer); publisher.close(); process.exit(0); });
}

if (require.main === module) start();
module.exports = {createPublisher,start};
