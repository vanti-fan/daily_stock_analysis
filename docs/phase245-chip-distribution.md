# Phase 2.4.5 — Chip Distribution
Baseline ecbad12; development branch only.
CD-01 audit: AkShare Eastmoney library estimate primary, optional Tushare cyq_chips + daily (entitlement not verified), Miaoxiang excluded because incomplete intervals/identity evidence, no reviewed Pytdx/efinance/Xueqiu chip endpoint.
CD-02 manager uses existing breaker and diagnostics; no additional LLM calls. Strict mode default on dev branch, Tushare fallback default off. Existing enable_chip_distribution switch remains authoritative.
CD-03/04 eight metrics, fraction ratios, positive CNY prices, nested quantiles, concentration formula, expected session and presence checks. Request unadjusted basis is not independently verified. No mixing providers or dates. A provider model is not actual investor holdings.
CD-05/06 computed field records and selected row/histogram archive with SHA-256; current batch model consistency gate. CD-07 independent chip cost page including unavailable state, including AI failure. Chip peak deferred per user; no invented histogram, no local holdings model.
CD-08 fixed fixtures, existing chip regressions and earlier phase regression, Chromium rendering. Live permission, calendar coverage, provider algorithms and unit accuracy remain Gate 2/3 validation. Tushare opt-in can add two data requests on fallback. No paid subscription change.
