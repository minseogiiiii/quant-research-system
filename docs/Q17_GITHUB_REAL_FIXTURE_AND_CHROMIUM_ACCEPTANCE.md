# Q17: Genuine GitHub archive and shipped v36 browser acceptance

**Candidate verdict: READ-ONLY QUALIFICATION ADVANCED. Not release-approved.**

Verified against the actual GitHub Actions executions on 2026-11-22. No fabricated fixture data, mocked API, replacement frontend build, or trading execution is accepted.

## Q14 private historical archive (confirmed PASS)

- Run: https://github.com/minseogiiiii/quant-research-system/actions/runs/44997372359
- Git commit: `cf3f9cddbdb74a00b40a353d25965258e4b393a2`
- Exact Q7 archive: 61,656,622 bytes; full SHA-256
  `0e49a7e2d7692452304ad5f555688182b4b74c1803c0b63f5094a4d526ca6b15`
- Private HTTPS GitHub Release downloaded with scoped read-only token. URL and token redacted from Actions logs.
- Original real frozen fixture SHA-256 checks for all three fixtures passed.
- Source parity `frontend_source_compared_to_checkout: true`.
- Actual Node 22: `tests: 31`, `passed: 31`, `failed: 0`, `skipped: 0`; exit 0.
- `Destroy raw archive on all outcomes`: **success**. No raw dataset uploaded to artifacts.

## Q15 original shipped v36 Chromium (confirmed PASS)

- Run: https://github.com/minseogiiiii/quant-research-system/actions/runs/44997382775
- Git commit: `5eb7f93736be55a29ab7a2d96d0268d959bd0fe9`
- Q14 commit compared with Q15 commit: exactly **one added comment in the Q15 workflow YAML**, zero product source changes.
- Same original private Q7 ZIP SHA-256 was checked; original shipped browser files restored in ephemeral runner.
- `index.html`: `4f36ff36e83716e30473475b7d4fca1afa15926960b37fe6c48155abcceedb26`
- `assets/index-BpiBDnMe.js`: `38775f84f9502e3d3f63f9c02d73ca2d57285ac83d728fcdabfe279cccbe1457`
- `assets/index-Ckn1nBC7.css`: `e46be294943262601df34f27c14f090e0bf8ca4acd183940c2f21c2e81a6b8b6`
- Same-origin local HTTP served by the genuine v36 gateway, with Playwright using real Google Chrome.
- Both BTC-USD and ETH-USD switch correctly, unavailable original source fails closed with **no trusted quote**.
- Browser JS page errors: 0. Trading enabled: false. No fake EventSource/fetch. No private archive exported.
- Safe artifact `q15-shipped-v36-no-source-dom-evidence`, GitHub artifact ID `14744482005`.
- Independently downloaded and inspected ZIP integrity: exactly `q15_shipped_dom_public.json` and `q15_no_source_dom.png`; ZIP CRC good. JSON contains `verdict: PASS_V36_SHIPPED_DOM_FAIL_CLOSED`, `release_approved: false` and excludes raw market data.
- Archived screenshot visually confirms "ARCHIVE SIMULATION · NOT LIVE", "No currently trusted market quote" and "orders disabled".
- This test **does not certify genuine Coinbase LIVE source, LIVE browser liquidity, streaming accuracy, source gap and reconnect in an actual network run, or 24-hour operation**.

## Independent full validation

- On the exact Q15 HEAD `5eb7f93736be55a29ab7a2d96d0268d959bd0fe9`, [Validation run 44997387178](https://github.com/minseogiiiii/quant-research-system/actions/runs/44997387178) is **SUCCESS**.
- Q14/Q15 workflow checks do not replace the historical full regression. Previously recorded private local Python **992/992** and the GitHub root 18-stage validation remain separate, with no product source changes introduced by Q17.

## Mandatory remaining gates, not passed

1. Persistent Linux host with real Coinbase BTC/ETH source and **24 consecutive hours** of source collection.
2. Persistent-host power/collector SIGKILL/restart/disk exhaustion and archive replay plus sustained SSE client backpressure and memory observation.
3. Separate LIVE-source Chromium scenario with actual data admission, quarantine and recovery, if required by the release acceptance plan.
4. Explicit operator review of deployment instructions and rollback, human approval and retention/security of original data.

**Strict result:** `release_approved: false`; `merge_approved: false`; `live_orders_enabled: false`. Preserve the original Q7 frozen source manifest. Remain Draft PR #7. Do not merge to staging, integration or main on the strength of only these two passing gates.

**Token hygiene:** If a short-lived personal access token was created solely for this qualification, revoke it or allow expiration after evidence is secured. To rerun after expiry, rotate only the token secret; do not loosen HTTPS policy.
