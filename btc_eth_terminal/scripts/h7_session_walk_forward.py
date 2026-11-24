"""Whole-epoch forward validation with actual event replay per fold.

Fixed candidate rules only; selection uses prior development sessions. Never
opens DECLARED_HOLDOUT. No refitting of statistical models or stitched equity.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from h4_cross_session_catalog import catalog
from h5_experiment_registry import sha,need
from h6_registered_strategy_runner import run,run_session,policy_for,code_pin,MAX_TRIALS
from independent_soak_replay import strict_json

def choose(report):
    if not report["all_complete"]:return None,"INCOMPLETE_TRIAL_INVENTORY"
    candidates=[r for r in report["results"] if r["status"]=="COMPLETE"
                and all(s["closed_roundtrips"]>0 for s in r["sessions"])
                and r["mean_session_net_return"]>0]
    if not candidates:return None,"NO_POSITIVE_ACTIVE_TRAINING_CANDIDATE"
    winner=sorted(candidates,key=lambda x:(-x["mean_session_net_return"],x["trial_id"]))[0]
    return winner["trial_id"],"SELECTED_ON_PRIOR_SESSIONS_ONLY"

def run_folds(entries,trials,train_sessions,embargo_ms,seed,workdir,*,fixture=False):
    need(type(entries) is list and 3<=len(entries)<=16 and all(e["role"]=="DEVELOPMENT" for e in entries),"H7_DEVELOPMENT_ONLY")
    need(type(train_sessions) is int and 2<=train_sessions<len(entries) and type(seed) is int and 0<=seed<2**32,"H7_BOUNDS")
    need(type(trials) is list and 1<=len(trials)<=MAX_TRIALS and all(policy_for(t) for t in trials)
         and len({sha(t) for t in trials})==len(trials) and len({t["product"] for t in trials})==1,"H7_TRIALS")
    cat=catalog(entries,embargo_ms,fixture=fixture);rows=cat["sessions"];by_pin={e["dataset_receipt_sha256"]:e for e in entries}
    windows=[]
    for i in range(train_sessions,len(rows)):
        test=rows[i]
        earlier=[j for j in range(i) if rows[j]["last_receiver_ms"]+embargo_ms<test["first_receiver_ms"]]
        need(len(earlier)>=train_sessions,"H7_EMBARGO_TRAINING_SHORT")
        windows.append({"train_indices":earlier[-train_sessions:],"test_index":i})
    plan={"schema":"h7_whole_epoch_walk_forward_plan_v1","catalog_sha256":cat["catalog_sha256"],
          "code_sha256":code_pin(),"orchestrator_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          "trials":trials,"train_sessions":train_sessions,"embargo_ms":embargo_ms,"seed":seed,"windows":windows,
          "selection":"HIGHEST_POSITIVE_MEAN_NET_SESSION_RETURN_ALL_TRAIN_SESSIONS_ACTIVE",
          "capital_reset":True,"source_kind":cat["source_kind"]}
    root=Path(workdir);root.mkdir(mode=0o700)
    with (root/"plan.json").open("x") as f:json.dump(plan,f,sort_keys=True,allow_nan=False);f.write("\n")
    folds=[]
    for i,w in enumerate(windows):
        train=[by_pin[rows[j]["dataset_receipt_sha256"]] for j in w["train_indices"]]
        result=run(train,embargo_ms,trials,seed,root/f"fold_{i:03d}.db",fixture=fixture)
        with (root/f"fold_{i:03d}_train.json").open("x") as f:json.dump(result,f,sort_keys=True,allow_nan=False);f.write("\n")
        winner,reason=choose(result)
        fold={"fold":i,"train_indices":w["train_indices"],"test_index":w["test_index"],
              "training_plan_sha256":result["plan_sha256"],"training_registry_head_sha256":result["registry_head_sha256"],
              "training_result_sha256":sha(result),"selected_trial":winner,"selection_status":reason,
              "test_status":"NOT_RUN_NO_CANDIDATE","test_result":None}
        if winner is not None:
            params=next(t for t in trials if sha(t)==winner);row=rows[w["test_index"]]
            # Persist the choice before touching the test strategy execution.
            with (root/f"fold_{i:03d}_selection.json").open("x") as f:json.dump(fold,f,sort_keys=True,allow_nan=False);f.write("\n")
            try:
                outcome=run_session(by_pin[row["dataset_receipt_sha256"]],row,params,fixture=fixture)
                fold.update(test_status="DIAGNOSTIC_COMPLETE",test_result=outcome)
            except (ValueError,KeyError,TypeError,OSError,ArithmeticError):
                fold["test_status"]="FAILED_REPLAY_OR_ACCOUNTING"
        folds.append(fold)
    return {"schema":"h7_whole_epoch_walk_forward_v1","plan":plan,"plan_sha256":sha(plan),"folds":folds,
            "diagnostic_test_count":sum(f["test_status"]=="DIAGNOSTIC_COMPLETE" for f in folds),
            "holdout_read":False,"cross_epoch_continuity":False,"portfolio_equity_stitched":False,
            "model_refit_performed":False,"actual_fills_verified":False,"oos_approved":False,
            "alpha_confirmed":False,"execution_authorized":False}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("plan",type=Path);p.add_argument("--workdir",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    a=p.parse_args();need(a.plan.stat().st_size<=1000000 and not a.output.exists(),"H7_PLAN_OUTPUT")
    v=strict_json(a.plan.read_bytes());need(set(v)=={"sessions","trials","train_sessions","embargo_ms","seed"},"H7_INPUT")
    r=run_folds(v["sessions"],v["trials"],v["train_sessions"],v["embargo_ms"],v["seed"],a.workdir)
    with a.output.open("x") as f:json.dump(r,f,sort_keys=True,allow_nan=False);f.write("\n")
if __name__=="__main__":main()
