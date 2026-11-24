export const workspaces=['Market','Microstructure','Research','Backtesting','Execution','Risk','System'] as const;
export type Workspace=typeof workspaces[number];
export const widgets=['BTC','ETH','BTC OFI','ETH OFI','SYS'] as const;
export type Widget=typeof widgets[number];
export type Panel={id:Widget;wide:boolean};
export type Layout={version:1;panels:Panel[]};
const defaults:Record<Workspace,Widget[]>={Market:['BTC','ETH','SYS'],Microstructure:['BTC OFI','ETH OFI','SYS'],Research:[],Backtesting:[],Execution:[],Risk:[],System:['SYS']};
export function initial(w:Workspace):Layout{return {version:1,panels:defaults[w].map(id=>({id,wide:false}))};}
export function decode(raw:string|null,w:Workspace):Layout{
 try{if(!raw||raw.length>4096)return initial(w);const x=JSON.parse(raw);
 if(x?.version!==1||!Array.isArray(x.panels)||x.panels.length>widgets.length)return initial(w);
 const seen=new Set<Widget>();const panels:Panel[]=[];
 for(const p of x.panels){if(!p||!widgets.includes(p.id)||typeof p.wide!=='boolean'||seen.has(p.id))return initial(w);seen.add(p.id);panels.push({id:p.id,wide:p.wide});}
 return {version:1,panels};
 }catch{return initial(w);}
}
export function move(l:Layout,from:Widget,to:Widget):Layout{
 const i=l.panels.findIndex(p=>p.id===from),j=l.panels.findIndex(p=>p.id===to);
 if(i<0||j<0||i===j)return l;
 const panels=[...l.panels];const [p]=panels.splice(i,1);panels.splice(j,0,p);return {version:1,panels};
}
export function toggle(l:Layout,id:Widget):Layout{
 return {version:1,panels:l.panels.some(p=>p.id===id)?l.panels.filter(p=>p.id!==id):[...l.panels,{id,wide:false}]};
}
export function resize(l:Layout,id:Widget):Layout{return {version:1,panels:l.panels.map(p=>p.id===id?{...p,wide:!p.wide}:p)};}
export const commands=['BTC','ETH','BTC OFI','ETH OFI','BTC DEPTH','BTC REPLAY','BT RUNS','BT DSR','BT PBO','MC','SYS','GAPS','RISK'] as const;
export type Command=typeof commands[number];
export function parseCommand(raw:string):Command|null{
 if(raw.length>64)return null;
 const normalized=raw.trim().toUpperCase().replace(/ +/g,' ');
 return commands.find(c=>c===normalized)??null;
}
