# Q15 read-only source admission, browser and fault qualification

**Candidate only. No release or order authorization.**

## Current trusted evidence

- [Q14 complete 18-stage root validation](https://github.com/minseogiiiii/quant-research-system/actions/runs/44986988899): Ruff 0, mypy green, root pytest 534/534.
- [Q14 latest PR full validation](https://github.com/minseogiiiii/quant-research-system/actions/runs/44987560943): success, 992 Python historical and 31/31 Node replay separately verified in isolated local environment.
- [Q15 no-source genuine HTTP safety](https://github.com/minseogiiiii/quant-research-system/actions/runs/44990093943): 5/5 PASS, actual localhost gateway, absent BTC/ETH quotes return HTTP 503, POST orders rejected.
- [Q15 shipped v36 original browser CI](https://github.com/minseogiiiii/quant-research-system/actions/runs/44989990495): **BLOCKED**, not successful browser E2E, because `Q13_PRIVATE_RELEASE_URL` is not configured.
- The shipped public v36 HTML/CSS/JS was independently checked inside the locally available original full Q7 ZIP: exactly 3/3 byte lengths and SHA-256 match the frozen `v36_dist_integrity.json`. This is local verification, not GitHub browser execution.
- The earlier GitHub Actions v36.1 code-only artifact has a **different JavaScript hash and filename**. It is explicitly forbidden as a substitute for this Q15 gate.

## One source secret unlocks two real historical qualification gates

Original (private) full immutable Q7 archive:

`BTC_ETH_V36_3_R5_Q7_VERIFIED_ENGINEERING_CANDIDATE.zip`

SHA-256:

`0e49a7e2d7692452304ad5f555688182b4b74c1803c0b63f5094a4d526ca6b15`

Required frozen shipped v36 public HTML/CSS/JS:

| Asset | Exact SHA-256 |
| --- | --- |
| `frontend/dist_v36/index.html` | `4f36ff36e83716e30473475b7d4fca1afa15926960b37fe6c48155abcceedb26` |
| `frontend/dist_v36/assets/index-BpiBDnMe.js` | `38775f84f9502e3d3f63f9c02d73ca2d57285ac83d728fcdabfe279cccbe1457` |
| `frontend/dist_v36/assets/index-Ckn1nBC7.css` | `e46be294943262601df34f27c14f090e0bf8ca4acd183940c2f21c2e81a6b8b6` |

1. Make the original frozen ZIP accessible on **private HTTPS storage** to the authorized CI runner, preferably using a short-lived access-controlled download URL.
2. In GitHub repository **Settings > Secrets and variables > Actions**, set repository secret `Q13_PRIVATE_RELEASE_URL` to the access-controlled ZIP download URL. Never paste URL tokens into Git commits, PRs, issues or logs.
3. Trigger the manual **Q14 historical real-fixture 31 of 31 gate** workflow. It checks the full ZIP and three historical Coinbase fixtures; requires **31/31 PASS, SKIP 0**.
4. Trigger the manual **Q15 byte-identical shipped v36 HTTP Chromium fail-closed gate** workflow. It verifies the full ZIP SHA before extracting **only the three public v36 browser assets** into an ephemeral runner, then executes the unchanged JavaScript/CSS/HTML over real same-origin HTTP. Nothing from the private market data folders is extracted.
5. Inspect successful exact run/commit checks and attach safe evidence to Draft PR #7, keeping the PR unmerged.

**Security guarantee:** Q15 deletes the full private ZIP within the same extraction step whether verification succeeds or fails. Only the three public browser assets remain in the ephemeral job directory. An optional screenshot depicts only an unadmitted/no-source state. No market archive or signed URL is uploaded as an artifact.

## Independent remaining production certification

- Q15 no-source HTTP tests are **not** a replacement for the private historical 0-SKIP Node gate.
- An unadmitted source browser test is **not** a real Coinbase LIVE browser, SSE reconnect, or market-price fidelity claim.
- Shipped v36 browser gate is **BLOCKED** until the original binary can be fetched; no alternate bundle may be substituted.
- A persistent Linux host with **24 hours uninterrupted real-source collection**, independently verified failure/restart/disk-full/collector SIGKILL/SSE backpressure recovery, and separate archival replay remains mandatory before read-only deployment signoff.
- Any paper/live trading authorization is a separate decision after demonstrated economic alpha and independent risk controls. The default remains **0 live orders**.

**Release verdict: BLOCKED** until every mandatory gate is independently green, with original Q7 frozen manifest preserved and an explicit human signoff.
