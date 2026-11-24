import React,{useEffect,useMemo,useState,useSyncExternalStore} from 'react';
import {createRoot} from 'react-dom/client';
import {ResilientGatewayClient} from '../datahub/resilientClient';
import {workspaces,widgets,initial,decode,move,toggle,resize,commands,parseCommand} from './model';
import type {Workspace,Widget,Layout} from './model';
import './workspace.css';
const key=(w:Workspace)=>'fincept.workspace.v1.'+w;
function load(w:Workspace){try{return decode(localStorage.getItem(key(w)),w);}catch{return initial(w);}}
function save(w:Workspace,l:Layout){try{localStorage.setItem(key(w),JSON.stringify(l));return true;}catch{return false;}}
function App(){
 const client=useMemo(()=>new ResilientGatewayClient(),[]);
 const view=useSyncExternalStore(client.store.subscribe,client.store.getSnapshot);
 const [w,setW]=useState<Workspace>('Market'),[layout,setLayout]=useState<Layout>(()=>load('Market'));
 const [query,setQuery]=useState(''),[message,setMessage]=useState(''),[palette,setPalette]=useState(false);
 const active=layout.panels.length>0;
 useEffect(()=>{const change=()=>{if(document.hidden||!active)client.stop();else client.start();};document.addEventListener('visibilitychange',change);change();return()=>{document.removeEventListener('visibilitychange',change);client.stop();};},[client,active]);
 useEffect(()=>{const onKey=(e:KeyboardEvent)=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();setPalette(v=>!v);}if(e.key==='Escape')setPalette(false);};window.addEventListener('keydown',onKey);return()=>window.removeEventListener('keydown',onKey);},[]);
 function update(l:Layout){setLayout(l);setMessage(save(w,l)?'Layout saved':'Storage unavailable; layout is temporary');}
 function switchTo(next:Workspace){setW(next);setLayout(load(next));setMessage('');}
 function run(raw:string){const c=parseCommand(raw);if(!c){setMessage('Unknown command; no action executed');return;}
 if(c==='BTC'||c==='ETH'||c==='BTC OFI'||c==='ETH OFI'||c==='SYS'){
 const target:Workspace=c==='SYS'?'System':c.endsWith('OFI')?'Microstructure':'Market';
 const l=load(target);const next=l.panels.some(p=>p.id===c)?l:toggle(l,c);
 setW(target);setLayout(next);setMessage(save(target,next)?'Opened '+c:'Opened; storage unavailable');setPalette(false);return;}
 if(c==='RISK'||c.startsWith('BT ')||c==='MC'){switchTo(c==='RISK'?'Risk':'Backtesting');}
 setMessage(c+' not connected to qualified data in this candidate');setPalette(false);
 }
 function content(id:Widget){
 if(id==='SYS')return <dl><dt>Admission</dt><dd>{view.status}</dd><dt>Reason</dt><dd>{view.reason}</dd><dt>Source</dt><dd>{view.frame?.active_source??'N/A'}</dd><dt>Generation</dt><dd>{view.frame?.generation??'N/A'}</dd><dt>Session</dt><dd>{view.frame?.source_session??'N/A'}</dd></dl>;
 if(!view.frame)return <p role="status">No currently trusted quote</p>;
 const q=view.frame.states[id.startsWith('BTC')?'BTC-USD':'ETH-USD'];
 if(id.endsWith('OFI')){const ofi=q.ofi_since_connection_base;return <><p>Connection-scoped OFI (base units)</p><strong>{typeof ofi==='string'&&/^-?\d+(\.\d+)?$/.test(ofi)?ofi:'N/A'}</strong><p>Session {q.session_id}</p></>;}
 return <><div className="f3quotes"><div>BEST BID <strong>{q.best_bid}</strong></div><div>BEST ASK <strong>{q.best_ask}</strong></div></div><dl><dt>Version</dt><dd>{q.state_version}</dd><dt>Receiver UTC</dt><dd>{new Date(q.receiver_time_ms).toISOString()}</dd><dt>Source hash</dt><dd>{q.source_raw_sha256}</dd></dl></>;
 }
 return <main><header><div><small>F3 / READ-ONLY CANDIDATE</small><h1>Research Workspace</h1></div><button onClick={()=>setPalette(v=>!v)}>Commands (Ctrl/⌘ K)</button></header>
 <nav aria-label="Workspaces">{workspaces.map(x=><button key={x} aria-pressed={w===x} onClick={()=>switchTo(x)}>{x}</button>)}</nav>
 <div className="f3status" role="status">Source: {active?view.status:'INACTIVE'} · NO CONFIRMED EXECUTABLE ALPHA · Orders disabled</div>
 {palette&&<section aria-label="Command palette"><form onSubmit={e=>{e.preventDefault();run(query);}}><input autoFocus aria-label="Command" value={query} onChange={e=>setQuery(e.target.value)} maxLength={64}/><button type="submit">Run</button></form><div>{commands.filter(c=>c.includes(query.trim().toUpperCase())).map(c=><button key={c} onClick={()=>run(c)}>{c}</button>)}</div></section>}
 <section aria-label="Layout controls"><button onClick={()=>update(initial(w))}>Reset layout</button>{widgets.map(id=><button key={id} aria-pressed={layout.panels.some(p=>p.id===id)} onClick={()=>update(toggle(layout,id))}>{id}</button>)}<p role="status">{message}</p></section>
 <div className="f3grid">{layout.panels.map((p,i)=><article key={p.id} data-widget={p.id} className={p.wide?'wide':''} onDragOver={e=>e.preventDefault()} onDrop={e=>{e.preventDefault();const from=widgets.find(id=>id===e.dataTransfer.getData('text/plain'));if(from)update(move(layout,from,p.id));}}>
 <div className="f3panel"><h2 draggable onDragStart={e=>e.dataTransfer.setData('text/plain',p.id)}>{p.id}</h2><button aria-label={'Move '+p.id+' earlier'} disabled={i===0} onClick={()=>update(move(layout,p.id,layout.panels[i-1].id))}>↑</button><button aria-label={'Resize '+p.id} onClick={()=>update(resize(layout,p.id))}>{p.wide?'Half':'Wide'}</button><button aria-label={'Remove '+p.id} onClick={()=>update(toggle(layout,p.id))}>×</button></div>{content(p.id)}</article>)}</div>
 {!layout.panels.length&&<p>No qualified widgets connected. Add an available widget or open Market. Existing Funnest routes remain separate.</p>}
 </main>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
