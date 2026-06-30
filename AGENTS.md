# Agentic Attack Evaluation — Project Map

**Status**: Defense factorial complete (5,040 runs, 0 errors). Paper body complete (§1–§4, all prose). arXiv-ready pending PDF render test.

---

## Commands

```bash
# Run all post-factorial statistics (always use this — never run bootstrap by hand)
.venv/bin/python scripts/analyze_results.py
.venv/bin/python scripts/analyze_results.py --out results/defense_factorial/analysis.json

# Run tests
.venv/bin/python -m pytest tests/ -q --tb=short -n auto
```

There is no dev server, build step, or lint script for this research repo. All analysis goes through `scripts/analyze_results.py`.

---

## Reference Materials

**Large reference files (not auto-loaded — load manually as needed):**

| File | Use it when… |
|------|-------------|
| `.Codex/reference/update-reminder.md` | **Start here first**. Current status, what changed recently, post-change checklist (did you update learningjourney + paper + knowledge?). Also has the `analyze_results.py` usage note. |
| `.Codex/reference/learningjourney.md` | Debugging weird model behaviour, understanding why a design choice was made, reviewing the full 45-iteration timeline. Last updated: Iteration 45 (qwq:32b Draft-Only Executor, memory_sandbox RAG bypass). |
| `.Codex/reference/knowledge.md` | Writing the paper (§1–§42 reference), mechanistic findings (why gemma4's BTCR flip matters), tool contract design, statistical methodology. Last updated: Section 25 (artifact 7, thread-safety race). |
| `.Codex/reference/paper.md` | Writing or updating §1–§4, reviewing findings and framing, checking related-work positioning. Last updated: paper body complete, arXiv ready. |
| `.Codex/reference/n10_verification_summary.md` | Canonical archetype classifications, rescreen methodology, reclassification rationale (qwen3:32b, qwen3.5:9b). Use when reviewing per-model behaviour in the paper. |
| `.Codex/reference/mechanistic-analysis.md` | Mechanistic framing — always report *why* a defense works or fails, not just the ASR number. Includes checklist, ablation templates, paper framing guidance. |

To load any file: ask "Read `.Codex/reference/filename.md`" and I'll pull it into context.

Global rules in `~/.Codex/rules/` also apply (audit-protocols, anti-hallucination, global-workflow, etc.).

---

## Project State

### Factorial: COMPLETE
9 models × 7 defenses × 2 attacks × N=40 = **5,040 runs, 0 errors**

All 9 models complete: qwen2.5:14b, qwen3.5:9b, qwen3:32b, qwen2.5:72b, qwen3.5:122b, qwq:32b, glm-4.7-flash:q8_0, gpt-oss:20b, gpt-oss-safeguard:120b.

### Core Findings
- **5 defenses fail** at ≥77.8% ASR: minimizer, sanitizer, rag_sanitizer, rag_llm_judge (~88.9% each), prompt_hardening (77.8%).
- **1 defense succeeds**: memory_sandbox — 0% ASR for 8/9 models; qwq:32b sole outlier (100% ASR via RAG re-injection bypass, Iteration 45).
- **BTCR (no-attack arm) = 100%** for all 9 models under memory_sandbox — zero utility cost in the absence of attack.
- Holm-Bonferroni correction over **108 active comparisons** (115 total; 7 N/A for qwq:32b). 10 significant.

### Behavioural Archetypes (v2 codebase, canonical)
- **Vulnerable Executors (11)**: qwen2.5:14b, qwen3.5:9b, qwen3:32b, qwen2.5:72b, qwen3.5:122b, qwq:32b, glm-4.7-flash:q8_0, gpt-oss:20b, gpt-oss-safeguard:120b, cogito:14b, glm-4.7-flash:bf16
- **Partial Executor (1)**: qwen3.5:35b — excluded from factorial
- **Latent Carriers (4)**: see n10_verification_summary.md
- **Inactive Receivers (2)**: see n10_verification_summary.md
- **qwq:32b note**: Draft-Only Executor archetype at 16k context (deliberate choice, not truncation); resolves authority conflict differently at 32k.

### Bedrock Smoke (400 runs)
- Sonnet 4.6 = Explicit Detector (0% injection)
- Haiku 4.5 = Active Detector with Defensive Storage (100% injection, 0% attack)

### Next Steps
1. arXiv PDF render test (fix Unicode arrows for pdflatex)
2. arXiv submission
3. USENIX Security 2026 workshop submission

### Key Data Files
- `results/defense_factorial/results.jsonl` — canonical factorial data
- `results/n10_all_models/results.jsonl` — canonical v2 rescreen data
- `canonical_numbers.md` — authoritative per-condition ASR/BTCR numbers
- `FINDINGS.md` — high-level findings summary
- `SUBMISSION_CHECKLIST.md` — arXiv submission checklist
