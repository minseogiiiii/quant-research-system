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
      <strong data-testid="market-status" className={view.status==='READY'?'ready':'blocked'}>{view.status}</strong></header>
    <p>Read-only. Coinbase public spot. Not exchange-attested. No trading or alpha certification.</p>
    {view.frame?<><section className="identity" data-testid="route" data-run={view.frame.gateway_run} data-generation={view.frame.generation} data-session={view.frame.source_session} data-source={view.frame.active_source}>
      <div>Active source <b>{view.frame.active_source}</b></div>
      <div>Generation <b>{view.frame.generation}</b></div>
      <div>Session <code>{view.frame.source_session}</code></div>
    </section><section className="market">{PRODUCTS.map(p=>{
      const q=view.frame!.states[p];
      return <article key={p} data-product={p} data-version={q.state_version} data-sequence={q.source_sequence} data-hash={q.source_raw_sha256} data-receiver-ms={q.receiver_time_ms}><h2>{p}</h2><div className="quotes"><div><small>BEST BID</small><b data-testid="bid" className="ready">{q.best_bid}</b></div>
        <div><small>BEST ASK</small><b data-testid="ask" className="ask">{q.best_ask}</b></div></div>
        <dl><dt>Source sequence</dt><dd>{q.source_sequence}</dd><dt>State version</dt><dd>{q.state_version}</dd>
          <dt>Receiver time</dt><dd>{new Date(q.receiver_time_ms).toISOString()}</dd></dl>
        <small>Source payload SHA-256</small><code>{q.source_raw_sha256}</code>
      </article>;
    })}</section></>:<section role="status" className="empty"><h2>No currently trusted quote</h2><p>{view.reason}</p></section>}
    <footer>F1 candidate. Existing Funnest charts and historical research routes are unchanged. No synthetic prices.</footer>
  </main>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><Candidate/></React.StrictMode>);
