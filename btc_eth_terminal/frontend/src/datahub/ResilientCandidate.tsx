import React,{useEffect,useMemo,useSyncExternalStore} from 'react';
import {createRoot} from 'react-dom/client';
import {ResilientGatewayClient} from './resilientClient';
import {PRODUCTS} from './resilientStore';
import './resilient.css';

function Candidate(){
  const client=useMemo(()=>new ResilientGatewayClient(),[]);
  const view=useSyncExternalStore(client.store.subscribe,client.store.getSnapshot);
  useEffect(()=>{
    const change=()=>{if(document.hidden)client.stop();else client.start();};
    document.addEventListener('visibilitychange',change);change();
    return ()=>{document.removeEventListener('visibilitychange',change);client.stop();};
  },[client]);
  return <main>
    <header><div><small>RESEARCH INFRASTRUCTURE / F1 CANDIDATE</small><h1>Verified Market Monitor</h1></div>
      <strong className={view.status==='READY'?'ready':'blocked'}>{view.status}</strong></header>
    <p>Read-only. Coinbase public spot. Not exchange-attested. No trading or alpha certification.</p>
    {view.frame?<><section className="identity">
      <div>Active source <b>{view.frame.active_source}</b></div>
      <div>Generation <b>{view.frame.generation}</b></div>
      <div>Session <code>{view.frame.source_session}</code></div>
    </section><section className="market">{PRODUCTS.map(p=>{
      const q=view.frame!.states[p];
      return <article key={p}><h2>{p}</h2><div className="quotes"><div><small>BEST BID</small><b className="ready">{q.best_bid}</b></div>
        <div><small>BEST ASK</small><b className="ask">{q.best_ask}</b></div></div>
        <dl><dt>Source sequence</dt><dd>{q.source_sequence}</dd><dt>State version</dt><dd>{q.state_version}</dd>
          <dt>Receiver time</dt><dd>{new Date(q.receiver_time_ms).toISOString()}</dd></dl>
        <small>Source payload SHA-256</small><code>{q.source_raw_sha256}</code>
      </article>;
    })}</section></>:<section role="status" className="empty"><h2>No currently trusted quote</h2><p>{view.reason}</p></section>}
    <footer>F1 candidate. Existing Funnest charts and historical research routes are unchanged. No synthetic prices.</footer>
  </main>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><Candidate/></React.StrictMode>);
