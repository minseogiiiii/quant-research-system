import test from 'node:test';
import assert from 'node:assert/strict';
import {initial,decode,move,toggle,resize,parseCommand} from './workspace-compiled/model.js';
test('versioned persistence and invalid storage fallback',()=>{const l=initial('Market');assert.deepEqual(decode(JSON.stringify(l),'Market'),l);for(const raw of ['{','{"version":2,"panels":[]}','{"version":1,"panels":[{"id":"SYS","wide":true},{"id":"SYS","wide":false}]}','{"version":1,"panels":[{"id":"trade","wide":false}]}'])assert.deepEqual(decode(raw,'Market'),l);});
test('reorder is immutable, resize and remove',()=>{const l=initial('Market');const m=move(l,'BTC','SYS');assert.deepEqual(m.panels.map(p=>p.id),['ETH','SYS','BTC']);assert.equal(l.panels[0].id,'BTC');assert.equal(resize(m,'BTC').panels[2].wide,true);assert.equal(toggle(m,'BTC').panels.length,2);});
test('strict allowlist',()=>{assert.equal(parseCommand(' btc  ofi '),'BTC OFI');for(const s of ['BTC; ls','BUY BTC','BTC\nOFI','BTC && SYS','__proto__'])assert.equal(parseCommand(s),null);});
