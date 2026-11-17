/** Public-only, no-subscription, no-credentials WSS upgrade diagnostic.
 * This is connectivity evidence, NEVER live source admission or market data.
 */
import { randomBytes, createHash } from 'node:crypto';
import { lookup } from 'node:dns/promises';
import https from 'node:https';

const HOST = 'advanced-trade-ws.coinbase.com';
const diagnostic = { schema:'v36_public_websocket_transport_probe_v1',
  target:'Coinbase Advanced Trade PUBLIC WSS', dns:'NOT_TESTED',
  http_upgrade_status:null, accepted_upgrade:false, network_error_code:null,
  live_source_admitted:false, market_messages_received:0 };
try {
  const result=await lookup(HOST);
  diagnostic.dns='RESOLVED'; diagnostic.dns_ip_family=result.family;
} catch (error) {
  diagnostic.dns='FAILED'; diagnostic.network_error_code=String(error?.code||'UNKNOWN');
}
const timeoutMs=8000;
if (diagnostic.dns==='RESOLVED') {
  const key=randomBytes(16).toString('base64');
  const want=createHash('sha1').update(key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  await new Promise(resolve=>{
    let finished=false;
    function done(){if(finished)return;finished=true;resolve();}
    const req=https.request({hostname:HOST,port:443,path:'/',method:'GET',
      timeout:timeoutMs,agent:false,headers:{
        'Connection':'Upgrade','Upgrade':'websocket',
        'Sec-WebSocket-Version':'13','Sec-WebSocket-Key':key,
      }},res=>{
        diagnostic.http_upgrade_status=res.statusCode||null;
        res.resume();res.on('end',done);req.destroy();done();
      });
    req.on('upgrade',(res,socket)=>{
      diagnostic.http_upgrade_status=res.statusCode||null;
      diagnostic.accepted_upgrade=(res.statusCode===101&&
        res.headers['sec-websocket-accept']===want);
      socket.destroy();done();
    });
    req.on('timeout',()=>{if(finished)return;diagnostic.network_error_code='TIMEOUT';req.destroy();done();});
    req.on('error',err=>{if(finished)return;diagnostic.network_error_code=String(err?.code||'NETWORK_ERROR');done();});
    req.end();
  });
}
console.log('WSS_TRANSPORT_DIAGNOSTIC '+JSON.stringify(diagnostic));
// This diagnostic never treats a successful upgrade as actual live-market admission.
