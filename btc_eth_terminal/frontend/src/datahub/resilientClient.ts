import {MarketStateStore} from './resilientStore.js';

type Stream = Pick<EventSource,'addEventListener'|'close'|'onerror'>;
type Transport={
  fetch:(path:string,signal:AbortSignal)=>Promise<Response>;
  stream:(path:string)=>Stream;
  wall:()=>number;
  mono:()=>number;
};
const browser:Transport={
  fetch:(path,signal)=>fetch(path,{signal,cache:'no-store',credentials:'omit',redirect:'error'}),
  stream:path=>new EventSource(path),
  wall:()=>Date.now(),mono:()=>performance.now()
};
const LIMIT=512*1024;
async function readBounded(r:Response):Promise<unknown>{
  if(!r.ok||!r.body||!r.headers.get('content-type')?.toLowerCase().startsWith('application/json'))
    throw Error('SNAPSHOT_HTTP');
  const reader=r.body.getReader(), decoder=new TextDecoder('utf-8',{fatal:true});
  let bytes=0,raw='';
  try{
    while(true){
      const next=await reader.read();
      if(next.done)break;
      bytes+=next.value.byteLength;
      if(bytes>LIMIT)throw Error('SNAPSHOT_SIZE');
      raw+=decoder.decode(next.value,{stream:true});
    }
    return JSON.parse(raw+decoder.decode());
  }finally{await reader.cancel().catch(()=>{});}
}
/** Single-owner transport; network and UI lifecycle separate from source capture. */
export class ResilientGatewayClient{
  readonly store=new MarketStateStore();
  private running=false;
  private ticket=0;
  private token=0;
  private retries=0;
  private connection:Stream|null=null;
  private abort:AbortController|null=null;
  private retry:ReturnType<typeof setTimeout>|null=null;
  private watchdog:ReturnType<typeof setInterval>|null=null;
  private requestTimeout:ReturnType<typeof setTimeout>|null=null;
  constructor(private io:Transport=browser){}
  start(){
    if(this.running)return;
    this.running=true;this.retries=0;
    this.watchdog=setInterval(()=>{
      if(this.connection&&!this.store.check(this.io.wall(),this.io.mono()))
        this.reconnect(this.ticket,'FRESHNESS_EXPIRED');
    },100);
    void this.connect();
  }
  stop(){
    this.running=false;this.ticket++;
    if(this.retry!==null)clearTimeout(this.retry);
    if(this.watchdog!==null)clearInterval(this.watchdog);
    this.retry=this.watchdog=null;
    this.cleanup();
    this.token=this.store.begin();this.store.block(this.token,'STOPPED');
  }
  private cleanup(){
    this.connection?.close();this.connection=null;
    this.abort?.abort();this.abort=null;
    if(this.requestTimeout!==null)clearTimeout(this.requestTimeout);
    this.requestTimeout=null;
  }
  private reconnect(ticket:number,reason:string){
    if(!this.running||ticket!==this.ticket)return;
    this.ticket++;
    this.cleanup();
    this.store.block(this.token,reason);
    if(this.retry!==null)clearTimeout(this.retry);
    const delay=Math.min(8000,250*2**Math.min(this.retries++,5));
    this.retry=setTimeout(()=>{this.retry=null;void this.connect();},delay);
  }
  private async connect(){
    if(!this.running)return;
    const ticket=++this.ticket;
    const token=this.token=this.store.begin();
    const ctrl=this.abort=new AbortController();
    this.requestTimeout=setTimeout(()=>this.reconnect(ticket,'SNAPSHOT_TIMEOUT'),2000);
    try{
      const raw=await readBounded(await this.io.fetch('/q292/snapshot',ctrl.signal));
      if(!this.running||ticket!==this.ticket)return;
      if(this.requestTimeout!==null)clearTimeout(this.requestTimeout);
      this.requestTimeout=null;
      if(!this.store.accept(token,raw,this.io.wall(),this.io.mono(),'handshake')){
        this.reconnect(ticket,'UNTRUSTED_SNAPSHOT');return;
      }
      const stream=this.io.stream('/q292/events');
      this.connection=stream;
      stream.addEventListener('snapshot',((event:MessageEvent)=>{
        if(!this.running||ticket!==this.ticket||this.connection!==stream)return;
        try{
          if(typeof event.data!=='string'||event.data.length>LIMIT)throw Error('SSE_SIZE');
          const raw=JSON.parse(event.data);
          if(!this.store.accept(token,raw,this.io.wall(),this.io.mono(),'snapshot',event.lastEventId))
            throw Error('SSE_REJECTED');
          this.retries=0;
        }catch{this.reconnect(ticket,'SSE_REJECTED');}
      }) as EventListener);
      stream.addEventListener('resync_required',()=>this.reconnect(ticket,'RESYNC_REQUIRED'));
      stream.onerror=()=>this.reconnect(ticket,'SSE_DISCONNECTED');
    }catch{this.reconnect(ticket,'SNAPSHOT_OR_CONNECT_FAILED');}
  }
}
