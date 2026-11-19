#!/usr/bin/env python3
"""Read-only, fail-closed SHA check of Q7 files staged in GitHub.

This check is a partial source provenance check only. It intentionally does
NOT certify Q7 deployment, real LIVE Q7 gateway, or source completeness.
"""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import subprocess

EXPECTED = {
 'btc_eth_terminal/backend/v37_factor_lab.py':'a6d49d3c972d33d978f7a4373e11cd5eef1c1094',
 'btc_eth_terminal/backend/v37_frontend_integrity.py':'b7cb343a4f921b9a2db110feff2090aa67294f3b',
 'btc_eth_terminal/backend/v37_independent_factor_audit.py':'b8c32b0406492b63a0c87efbaa025682dfe5a26e',
 'btc_eth_terminal/backend/q7_experiment_prereg.json':'e498cfaa0aec66b24e3720599566a4babcdade67',
 'btc_eth_terminal/backend/q7_experiment_prereg.sha256.json':'e3445a4eb19e74c4abd1d69e5a02cfabc6bde92a',
 'btc_eth_terminal/frontend/src/FactorLabPanel.tsx':'4e2c9347dd90f2e9748eec52602ee250d228a261',
 'btc_eth_terminal/frontend/v37_dist_integrity.json':'e4b28a72ef15f772271236f7e047af4757fcc303',
 'btc_eth_terminal/scripts/v37_build_frontend.py':'bbd825e35f02f3d653a1b5de7e5ef7f4062edaaa',
}
R5_BASE = '64aa85081cab73c917333249eb8e200c8d4044fa'

def git(*args: str) -> str:
    return subprocess.run(['git',*args],check=True,text=True,stdout=subprocess.PIPE).stdout.strip()

def check(root: Path) -> dict:
    if git('rev-parse','--is-inside-work-tree') != 'true':
        raise ValueError('NOT_GIT_REPOSITORY')
    branch = git('branch','--show-current')
    if branch != 'staging/q7-readonly-qualification-20261119':
        raise ValueError('WRONG_STAGING_BRANCH')
    if subprocess.run(['git','merge-base','--is-ancestor',R5_BASE,'HEAD'],check=False).returncode:
        raise ValueError('R5_ANCESTRY_REQUIRED')
    if git('status','--porcelain'):
        raise ValueError('DIRTY_WORKTREE')
    for path,expected in EXPECTED.items():
        raw=(root/path).read_bytes()
        observed=hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()
        assert observed==expected,(path,observed)
        assert git('rev-parse','HEAD:'+path)==expected,path
        if path.endswith('.py'):
            ast.parse(raw,filename=path)
    p=root/'btc_eth_terminal/backend'
    reg=json.loads((p/'q7_experiment_prereg.json').read_bytes())
    pin=json.loads((p/'q7_experiment_prereg.sha256.json').read_bytes())
    assert hashlib.sha256((p/'q7_experiment_prereg.json').read_bytes()).hexdigest()==pin['preregistration_sha256']
    assert reg['orders_enabled'] is False and reg['split']['blind_final'] is None
    assert reg['current_verdict']=='INSUFFICIENT_EVIDENCE'
    f=root/'btc_eth_terminal/frontend'
    asset=(f/'v37_dist_integrity.json').read_bytes()
    body=(p/'v37_frontend_integrity.py').read_text()
    assert hashlib.sha256(asset).hexdigest() in body
    asset_json=json.loads(asset)
    assert asset_json['trading_enabled'] is False
    assert len(asset_json['files'])==3
    changes=git('diff','--name-only',R5_BASE+'..HEAD').splitlines()
    for path in changes:
        parts=Path(path).parts
        if any(part in {'data','dist','dist_v36','dist_v37','node_modules','__pycache__','.env'} for part in parts):
            raise ValueError('UNSAFE_PUBLIC_CHANGE:'+path)
        if Path(path).suffix in {'.jsonl','.gz','.zip','.key','.pem','.sqlite','.db'}:
            raise ValueError('UNSAFE_PUBLIC_CHANGE:'+path)
    return {'status':'PASS_PARTIAL_SOURCE_ONLY_NOT_MERGE_READY',
       'branch':branch,'q7_staged_pinned_files':len(EXPECTED),
       'q7_required_source_total':278,'q7_unstaged_or_unverified_at_least':278-len(EXPECTED),
       'data_in_git':False,'orders_enabled':False,'full_q7_build_verified':False,
       'real_q7_live_verified':False,'main_or_integration_merged':False}

if __name__=='__main__':
    print(json.dumps(check(Path.cwd()),sort_keys=True,indent=2))
