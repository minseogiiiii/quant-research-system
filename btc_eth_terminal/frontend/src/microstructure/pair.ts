import {decode} from '../heatmap/contract.js';
import {decodeFlow} from '../orderflow/contract.js';
export function pairProjections(depth:unknown,flow:unknown){
 const d=decode(depth),f=decodeFlow(flow);
 if(d.pin!==f.pin||d.product!==f.product||d.start!==f.start||d.end!==f.end||d.step!==f.step)
  throw Error('PROJECTION_EPOCH_PRODUCT_OR_TIME_GRID_MISMATCH');
 return {depth:d,flow:f};
}
export type Pair=ReturnType<typeof pairProjections>;
