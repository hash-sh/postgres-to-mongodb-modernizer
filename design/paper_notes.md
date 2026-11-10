# Notes: AptMQL-Bench (MongoDB Research, Oct 2026)

**Paper:** "AptMQL-Bench: From Text-to-SQL to Text-to-MQL via Access-Pattern Schema Design and Data-Preserving
Migration", by Hy Nguyen (Univ. of Sydney; work done during an internship at MongoDB), Nabi Rezvani and Robin Vujanic (MongoDB Research).
arXiv:2610.02770, v1 2 Oct 2026. Blog: "Introducing AptMQL-Bench: A Text-to-MQL Benchmark" (21 Oct 2026).

**What was read:** the arXiv abstract page and the blog post in full. Of the HTML full text, Sections 1–6 (abstract,
intro, pipeline, benchmark, metrics, LLM results) came back verbatim. The rest of Section 7, Section 8, the
limitations and the appendices came back only as a summary from the fetch tool, so nothing below relies on them.

## What the paper does (relevant parts)
- **Pipeline:** a coding agent runs the pipeline, with lightweight human review at designated quality gates.
  1. Schema design, guided by a guidebook distilled from MongoDB University courses around the **Golden Rule**:
     *"data that is accessed together is stored together"*.
     - **Conceptual design:** a source analysis and data profile, the *assumed* access patterns and operation rates,
       an embed-vs-reference decision for each relationship (patterns applied, anti-patterns avoided), and the
       collections, indexes and field transformations.
     - **Logical design:** a per-field mapping (source column → target field, MongoDB type, transformation notes).
     - **Review gate:** each modelling choice must be reasonable, and *"every source table must remain retrievable
       from the proposed design document"*.
  2. **Migration and three validation checks:**
     1. Collection validation: paired SQL and MQL queries that *"must return identical results"*.
     2. Cross-collection validation: source- and target-side pipelines whose outputs *"must agree under containment"*
        (references resolve).
     3. Source table validation: *"flags any source value with no match as probable data loss"*.
  3. **Translate, then revise** until results are exactly equivalent to the SQL. Then an **optimization pass**, because
     correct translations often keep a relational shape (*"redundant $lookup stages that look up already-embedded
     documents"*). 45.32% of their MQL queries were rewritten this way.
- **Naive-embedding failure modes:** foreign-key embedding failed outright on 6 databases, *"three because a
  self-referential foreign key triggered infinite recursion"* and three because documents exceeded the 16 MB limit.
  It also silently lost rows (up to 25.9%). One-to-one conversion "joins tables with unindexed cross-collection
  $lookups". The access-pattern design is reported as up to roughly 100× faster than one-to-one.
- The guidebook's anti-patterns include **unbounded arrays** and **bloated documents**.

## The 5 ideas this project borrows
1. **Access-pattern-driven design using the Golden Rule.** The schema agent gets the endpoint access patterns from
   Stage 2 as its main input.
2. **Conceptual, then logical, then a review gate.** Two separate LLM calls produce the conceptual decisions and the
   logical field mapping. The paper's human review is replaced by a deterministic lint plus an LLM critic plus a
   written self-review.
3. **The three post-migration checks**, implemented as 100% deterministic code in `pipeline/checks.py`.
4. **Translate, revise until it matches, then an optimization pass.** Stage 4 runs a repair loop against frozen tests,
   then a separate pass that removes relational-shaped queries (per-row lookups) where the schema already embeds the data.
5. **Handling the known failure modes explicitly.** The lint rejects self-referential embedding (`follows`),
   unbounded arrays (favorites, followers, comments) and documents that could exceed 16 MB.

**Difference from the paper:** the paper validates query translations against SQL results. Here the unit being
translated is an application's data layer, so equivalence is checked at the HTTP API level: the official suite,
frozen characterization tests, and a differential replay against both backends.
