# Phase 2.4.3 — Intelligent Fallback & Adjustment Consistency

Baseline f199f64; development branch only. Scope: ordinary Shanghai/Shenzhen equities, not indices, ETFs, BSE or offshore.

- Branch workflow enables DSA_INTELLIGENT_FALLBACK by default. Set repository variable false to revert to upstream routing; do not mix old and new cache databases during testing.
- Reviewed historical paths request QFQ; Tencent direct rejects raw day fallback. Request evidence is not independent verification of adjustment factors or point-in-time anchor. Tushare daily and Pytdx bars are not treated as QFQ; no guessed factors.
- Atomic database guard refuses incomplete replacement of previously stored dates. The standard daily pipeline bypasses legacy resume and extends the existing request window. An incomplete response fails closed; existing rows remain untouched.
- Existing capability checks, circuit breakers and cancellation remain. Health uses paired same-operation evidence, minimum 3 completed calls, success below 50% deferred; configured order is tie-breaker. Shared dependencies deferred, not claimed independent.
- Optional akshare_xq at end of REALTIME_SOURCE_PRIORITY enables Xueqiu quote fallback only after prior routes fail. No new mandatory network calls; optional fallback can add requests on failure. Xueqiu may require valid login/session; not verified live. No authentication bypass. Unverified timestamp not assigned, no TTM/static PE substitution for dynamic PE.
- Strict quote supplement: at most one later route; matching code, known currency, market time (<=600 s, same China day) and price (<=1%). Source metadata recorded per field. A missing provider timestamp disables merging, not primary display.
- Fundamentals: reuse quote valuation; sanitize non-finite/malformed fields and preserve lineage. Financial statements continue existing AkShare bundle; no new paid endpoints.
- Tests use fixed fixtures, no external data or LLM API. Live provider coverage, real response timing, adjustment factors and full integration are acceptance gates, not yet verified.

Additional integration: Baostock checks returned adjustflag=2; Pytdx raw historical bars remain excluded from QFQ but its existing quote endpoint is an optional identity-verified fallback (`pytdx`). Strict mode does not append raw live quotes to QFQ indicators. On closed-date history failure, the pipeline archives the already-planned quote and skips AI rather than analyzing unverified old bars. Request evidence is archived per run; no DB schema change. Provider regressions use pytest. These checks still require the actual branch workflow to execute.
