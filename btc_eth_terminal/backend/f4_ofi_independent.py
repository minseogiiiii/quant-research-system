"""Independent Decimal/bisect audit of causal features and future labels.

Only shared input parser and previously verified source bytes, not the candidate
projection or its book, aggregation, decision, or future-label logic.
"""
from bisect import bisect_left
from decimal import Decimal, localcontext
from v23_causal_microstructure import Book


def require(ok, reason):
    if not ok:
        raise ValueError("OFI_WITNESS_"+reason)


def audit(packets,report):
    policy=report["policy"]
    product=report["product"]
    book=Book()
    timestamps,seqs,quotes,prefix,update_counts=[],[],[],[Decimal(0)],[0]
    source_first=source_last=snapshot=None
    count=0
    with localcontext() as ctx:
        ctx.prec=80
        for seq,t,wire in packets:
            require(seq==count and (source_last is None or t>=source_last),"CHRONOLOGY")
            count+=1
            source_first=t if source_first is None else source_first
            source_last=t
            if wire["channel"]!="l2_data":
                continue
            events=[e for e in wire["events"] if e["product_id"]==product]
            if not events:
                continue
            old=book.best()
            updated=False
            for e in events:
                if e["type"]=="snapshot":
                    snapshot=t
                else:
                    updated=True
                book.apply(e["type"],e["updates"])
                # Bound lazy heap accumulation independently of candidate.
                import heapq
                for side,levels in book.level.items():
                    if len(book.heap[side])>3*len(levels)+1024:
                        book.heap[side]=[(-p if side=="bid" else p) for p in levels]
                        heapq.heapify(book.heap[side])
            new=book.best()
            require(new is not None,"BOOK")
            delta=Decimal(0)
            if old is not None and updated:
                (p,q),(a,v)=new
                (p0,q0),(a0,v0)=old
                bid=q if p>p0 else -q0 if p<p0 else q-q0
                ask=-v if a<a0 else v0 if a>a0 else v0-v
                delta=bid+ask
            timestamps.append(t)
            seqs.append(seq)
            quotes.append(new)
            prefix.append(prefix[-1]+delta)
            update_counts.append(update_counts[-1]+int(updated))
        require(count==report["source_messages"] and source_first==report["source_first_receiver_ms"]
                and source_last==report["source_last_receiver_ms"],"SOURCE_COUNT")
        grid=range(policy["start_ms"],policy["end_ms"],policy["step_ms"])
        require([f["decision_ms"] for f in report["features"]]==list(grid),"GRID")
        expected_labels={}
        valid=0
        for f in report["features"]:
            t=f["decision_ms"]
            j=bisect_left(timestamps,t)
            i=bisect_left(timestamps,t-policy["window_ms"])
            if t>source_last:
                status="OUTSIDE_OBSERVED_SOURCE"
            elif snapshot is None or t-policy["window_ms"]<max(source_first,snapshot):
                status="INSUFFICIENT_OBSERVED_WARMUP"
            elif j==0 or t-timestamps[j-1]>policy["max_age_ms"]:
                status="NO_FRESH_QUOTE"
            else:
                status="CAUSAL_FEATURE"
            require(f["status"]==status,"FEATURE_STATUS")
            if status!="CAUSAL_FEATURE":
                require(f["feature_available_ms"] is None and f["source_sequence"] is None
                        and not any(k in f for k in ("mid","ofi_base","imbalance","microprice")),"MISSING_FEATURE")
                continue
            valid+=1
            (b,q),(a,v)=quotes[j-1]
            mid=(b+a)/2
            expected={"best_bid":b,"best_ask":a,"mid":mid,"ofi_base":prefix[j]-prefix[i],
                      "imbalance":(q-v)/(q+v),"microprice":(a*q+b*v)/(q+v)}
            require(f["source_sequence"]==seqs[j-1] and f["feature_available_ms"]==timestamps[j-1]
                    and f["source_age_ms"]==t-timestamps[j-1]
                    and f["update_packets"]==update_counts[j]-update_counts[i],"FEATURE_ID")
            for k,val in expected.items():
                require(abs(Decimal(f[k])-val)<=Decimal("1e-65")*max(Decimal(1),abs(val)),"FEATURE_VALUE")
            for h in policy["horizons_ms"]:
                target=t+h
                k=bisect_left(timestamps,target)
                expected_labels[t,h]=(target,k,mid)
        seen=set()
        observed=0
        for label in report["future_labels"]:
            key=(label["decision_ms"],label["horizon_ms"])
            require(key in expected_labels and key not in seen,"LABEL_KEY")
            seen.add(key)
            target,k,mid=expected_labels[key]
            require(label["target_ms"]==target,"LABEL_TARGET")
            if k==len(timestamps):
                status="NO_FUTURE_OBSERVATION"
            elif timestamps[k]-target>policy["tolerance_ms"]:
                status="FIRST_L2_OUTSIDE_TOLERANCE"
            else:
                status="OBSERVED_FUTURE_LABEL"
            require(label["status"]==status,"LABEL_STATUS")
            if status!="OBSERVED_FUTURE_LABEL":
                require(all(label[x] is None for x in ("label_available_ms","label_source_sequence","midquote_return_bps")),"MISSING_LABEL")
                continue
            observed+=1
            (b,_),(a,_)=quotes[k]
            value=((b+a)/2-mid)*10000/mid
            require(label["label_source_sequence"]==seqs[k] and label["label_available_ms"]==timestamps[k],"LABEL_ID")
            require(abs(Decimal(label["midquote_return_bps"])-value)<=Decimal("1e-65")*max(Decimal(1),abs(value)),"LABEL_VALUE")
        require(seen==set(expected_labels),"LABEL_INVENTORY")
    return {"source_messages":count,"decisions":len(report["features"]),"valid_features":valid,
            "labels":len(seen),"observed_labels":observed,"independent_software_parity":True,
            "oos_approved":False,"alpha_confirmed":False}
