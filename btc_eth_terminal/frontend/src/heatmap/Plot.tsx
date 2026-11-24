import {useEffect,useRef} from 'react';
import type {Projection,Row} from './contract';
export default function Plot({data,log,select}:{data:Projection;log:boolean;select:(row:Row|null)=>void}){
 const canvas=useRef<HTMLCanvasElement>(null);
 const count=(data.end-data.start)/data.step;
 const max=data.rows.reduce((m,r)=>r.cells.reduce((v,c)=>Math.max(v,c.value),m),0);
 useEffect(()=>{
  const el=canvas.current;if(!el)return;const ctx=el.getContext('2d');if(!ctx)return;
  const w=count*2,h=data.bins;el.width=w;el.height=h;
  ctx.fillStyle='#101c28';ctx.fillRect(0,0,w,h);
  for(const t of data.missing){const x=(t-data.start)/data.step;ctx.fillStyle='#3a4250';ctx.fillRect(x*2,0,2,h);}
  for(const r of data.rows){
   const x=(r.time-data.start)/data.step;
   for(const c of r.cells){
    const y=h-1-c.bin;const ratio=max===0?0:log?Math.log1p(c.value)/Math.log1p(max):c.value/max;
    ctx.fillStyle=c.side==='bid'?'rgba(54,211,170,'+(0.15+0.85*ratio)+')':'rgba(243,118,112,'+(0.15+0.85*ratio)+')';
    ctx.fillRect(x*2+(c.side==='offer'?1:0),y,1,1);
   }
   if(r.midBin>=0&&r.midBin<h){ctx.fillStyle='#f4df87';ctx.fillRect(x*2,h-1-Math.floor(r.midBin),2,1);}
  }
 },[data,count,max,log]);
 return <><div className="axis">Price high (exclusive): {data.high}</div><canvas ref={canvas} aria-label="Historical observed liquidity heatmap" onPointerMove={e=>{const box=e.currentTarget.getBoundingClientRect();const i=Math.min(count-1,Math.max(0,Math.floor((e.clientX-box.left)/box.width*count)));select(data.rows.find(r=>r.time===data.start+i*data.step)??null);}} onPointerLeave={()=>select(null)}/><div className="axis">Price low: {data.low} · {new Date(data.start).toISOString()} to {new Date(data.end).toISOString()}</div><p>Green bid / red offer / yellow midpoint / gray no L2 observation. Peak cell quantity: {max} base units. Bid and offer are drawn in separate half-columns; exact quantities remain in the inspector.</p></>;
}
