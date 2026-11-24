/* Synthetic transport only, inject a single skipped sequence into original collector. */
import './q292_dual_source_mock.mjs';
if(process.env.SOAK_TEST_FIXTURE!=='1')throw Error('SYNTHETIC_ONLY');
const Base=globalThis.WebSocket;
let injected=false;
globalThis.WebSocket=class extends Base{
 emit(channel,events){
  if(!injected&&this.seq>=12){injected=true;this.seq++;}
  return super.emit(channel,events);
 }
};
