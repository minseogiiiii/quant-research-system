/* Deterministic synthetic WebSocket for collector integration tests; NEVER market data. */
const products=['BTC-USD','ETH-USD'];
class MockWebSocket extends EventTarget {
  constructor(url) {
    super();
    if (url !== 'wss://advanced-trade-ws.coinbase.com') throw Error('Wrong endpoint');
    this.closed=false;this.seq=0;
    setTimeout(()=>{this.dispatchEvent(new Event('open'));this.sendSample();},5);
  }
  send(message){
    const m=JSON.parse(message);
    if(m.type!=='subscribe'||!['level2','market_trades','heartbeats'].includes(m.channel))throw Error('Wrong subscription');
  }
  sendSample(){
    const send=(channel,events)=>{
      const m={channel,sequence_num:this.seq++,timestamp:new Date().toISOString(),events};
      this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(m)}));
    };
    send('subscriptions',[]);
    for(const p of products){
      const btc=p==='BTC-USD';
      send('l2_data',[{type:'snapshot',product_id:p,updates:[
        {side:'bid',price_level:btc?'100':'200',new_quantity:'4',event_time:new Date().toISOString()},
        {side:'offer',price_level:btc?'101':'202',new_quantity:'5',event_time:new Date().toISOString()},
      ]}]);
    }
    for(const p of products){
      send('l2_data',[{type:'update',product_id:p,updates:[
        {side:'bid',price_level:p==='BTC-USD'?'100':'200',new_quantity:'3',event_time:new Date().toISOString()},
      ]}]);
    }
    send('heartbeats',[{current_time:new Date().toISOString(),heartbeat_counter:'1'}]);
    send('market_trades',[{type:'update',trades:[{product_id:'BTC-USD',price:'100.4',size:'0.1',time:new Date().toISOString(),trade_id:'mock-t1',side:'BUY'}]}]);
  }
  close(){this.closed=true;this.dispatchEvent(new CloseEvent('close',{code:1000}));}
}
globalThis.WebSocket = MockWebSocket;
