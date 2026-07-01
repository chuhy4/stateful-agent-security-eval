# arXiv Submission Checklist

> **Last updated**: 2026-07-01. Source: `paper/paper.tex`

## Current Status

**Paper v4 needs three edits before submission.** The Mac Studio sprint was STOPPED (reasoning models degraded under long-running daemon — see learningjourney Iteration 55). A fresh-load suite replaces it (launched 2026-07-01 16:38 SGT, ~12–20h).

**What's done (clean, publishable):**
- Phase 0 (date sweep): ✅ complete
- Phase 1 (payload variants): ✅ complete (50/50)
- 3 mechanical models RATG: ✅ clean (100%→0%, qwen2.5:14b/72b, qwen3:32b)

**What's running (fresh-load suite, replaces stopped sprint):**
- 6 reasoning models × {no_defense, ratg} × DTA × N=40 = 480 runs (fresh daemon per model)
- 3 models × {no_defense, rag_llm_judge_7b} × DTA × N=40 = 240 runs (fills Phase 3)
- Total: 720 runs. ETA: ~04:00–08:00 SGT Jul 2

**v4 writing actions (after fresh-load suite completes):**
1. Add daemon-degradation evaluation artifact paragraph (source: `paper/v4_daemon_artifact.md`) to §3.4
2. Add RATG section: mechanical models 100%→0%; reasoning models from fresh-load suite results
3. Add 7B-judge result to §3.2.4 (bounds capability threshold)
4. Do NOT add BSI §4.7/§4.8 reasoning-class instability claim (retracted — Iteration 55)
5. Do NOT widen "qwq-specific" scoping (sprint 0% was degradation, not safety)
6. Delete `paper/v4_daemon_artifact.md` and `paper/v4_draft_additions.md` after integration
- If partial: most interesting - characterize which models/conditions it works for and why.

**When sprint Phase 3 (7B judge) completes:**
- If 7B detects what 1.5B couldn't: revise §3.2.4 to bound the capability threshold. "A 7B judge achieves X% detection vs 0% for 1.5B."
- If 7B also fails: strengthen the "semantic masking defeats small judges" narrative. Add 1 sentence.

**Temporal persistence (already added to paper.tex §3.1):**
- ✅ Marathon test cited: "50-session marathon, byte-identical at checkpoints 10/20/30/40/50, persistence bounded by database durability."

**Completed experiments (this machine):**
- ✅ frontier model probe: 21 models × N=10 = 210 runs, 0% ASR
- ✅ frontier sandbox probe: 4 Latent Carriers × N=10-16 = 46 runs, 0% bypass
- ✅ Bedrock N=40 date sweep: 5 models × 3 dates, all p>0.017 (null confirmed)
- ✅ Supply chain: parked (logical argument in paper)
- ✅ Defense factorial: 5,040 runs (Mac Studio, completed April)
- ✅ N=10 rescreen: 180 runs (Mac Studio, completed April)
- ✅ Bedrock Sonnet/Haiku N=100: 400 runs (completed April)

## Metadata
- **Title**: Defense Effectiveness Across Architectural Layers: A Mechanistic Evaluation of Persistent Memory Attacks on Stateful LLM Agents
- **Primary**: cs.CR (Cryptography and Security)
- **Secondary**: cs.LG, cs.AI
- **Comments**: 5,040 factorial runs + 210 frontier screening runs + 46 sandbox probe runs; 9 open-source models, 21 frontier models, 3 providers; pre-registered comparisons; BCa bootstrap CIs.

## Build

```bash
cd paper/
pdflatex paper.tex
bibtex paper
pdflatex paper.tex
pdflatex paper.tex
```

Or package for arXiv:
```bash
tar -czf paper-v4-arxiv.tar.gz paper.tex math_commands.tex references.bib iclr2025_conference.sty paper.bbl
```

## Pre-submission checks

- [ ] `paper/paper.tex` compiles without errors
- [ ] All `\ref{sec:*}` resolve (no `??`)
- [ ] Abstract ≤ 1500 chars (arXiv limit for metadata field)
- [ ] All numbers match `canonical_numbers.md`
- [ ] Supplementary Table S1 (Holm-Bonferroni) referenced
- [ ] Frontier screening table renders correctly (21 models)
- [ ] No frontier API/infrastructure details in paper text
- [ ] Author name and email correct

## Source of truth hierarchy

1. `paper/paper.tex` - submission text
2. `canonical_numbers.md` - every number traces here
3. `FINDINGS.md` + `docs/index.md` - public reader-facing
4. `README.md` - repo landing page

## GitHub repo (public-facing)

- [x] README: 5,040 runs, 9 models, 21 frontier
- [x] FINDINGS.md: complete results summary
- [x] docs/index.md: GitHub Pages site (v4 section)
- [x] canonical_numbers.md: experiments 1-6
- [x] scripts/verify_canonical.py: programmatic verification
