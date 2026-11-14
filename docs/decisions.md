# Architecture decisions

Record of design decisions made during this project, with context and reasoning.

## Format

**Decision title**
Context, options considered, and why we chose this path.

## Use uv with src layout

We need a Python project manager. Options: uv, poetry, pip+venv. Chose uv because it is fast, handles both dependency management and virtual environments, and is the direction the Python packaging ecosystem is moving. The src layout prevents accidental imports from the project root.

## Module 1 - Data Loader
Module 1 loads NHTSA's public recall file into Postgres. The file has one row per recall per vehicle, so a recall coering 30 models will appear 30 times with the same description. These have been grouped using Python into one record per campaign, with a de-duplicated list of vehicled rather than loading everything into a staging table and merging with SQL. 

The SQL was wrote directly with psycopg instead of an ORM as this is a one-way batch with no relationship navigation. A similar reasoning for why a small migration runner is used rather than Alembic. 

The loader upserts on campaignID for idempotency and a parser then maps each column explicitly an checks the column count on every row, so later changes in the original datas layout fail rather than silently passing and getting fed into the pipeline. 

Current modules limitations: 
- Everything is held in memory before saving. Fine for this size but larger data would need batching.
- Recalls are saved one at a time but again not worth optimising igven the data size.
- Download currently has no retry logic. 

## Module 2 - Chunks, embeddings and indexes
Each recall is split into cards two ways: record (one card per recall) and field (separate defect, consequence and remedy cards). Both are kept so layer 2 can measure which finds the right recall more often.

The embedding model reads at most 512 tokens and silently drops the rest, so long text is cut into windows of up to 400 tokens with a 50-token overlap. Cuts use the tokenizer's character offsets, so the original text and capitals are kept. Each card starts with a vehicle header capped at 5 make/model entries; filtering by make, model and year uses the recall_vehicles table, not the card text.

Cards are embedded with bge-small-en-v1.5 as normalised vectors and compared by cosine distance, so only meaning (direction) matters. The upsert clears an embedding only when a card's text changes, so re-runs only re-embed what changed, and leftover windows are deleted.

There is one partial HNSW index per chunker, so a search on one set never goes through the other. m = 16 and ef_construction = 64 are pgvector's defaults and are fine at about 30,000 cards. The EXPLAIN tests prove the index can be used; explain_check.py proves it is used on the real data.

Tests run against a separate recall_assistant_test database so they can't wipe the real cards. Three tests use the real tokenizer, because the fake one still had encode_plus after transformers 5 removed it: the unit tests passed and the first real run crashed.

Limitations:
- A full embedding run takes about 30 minutes on CPU.
- HNSW is approximate and can miss a close card. Layer 2 measures this.
- The header names at most 5 models, so a question about a later model can't match it by meaning. Filters cover this.

## Module 2b - Data cleaning
Inspecting a split recall on the real data found three issues. Each was measured before fixing.

Unknown model years: 119 recalls had no known model year, all reported between 2010 and 2017, mostly Webasto sunroof add-ons. A recall is now kept if any vehicle has a known year of 2018 or later, or, if none do, if it was reported on or after 1 January 2018. Simply dropping every no-year recall would also lose a future recent one. This removed the 119, taking the set from 7,633 to 7,514.

HTML: 111 recalls contained anchor tags and nothing else, with link text that repeats the URL. Only <a> and </a> are stripped, with a regex. A general HTML parser was rejected because unescaping can change normal text (PUMP&REGULATOR becomes PUMP®ULATOR), and text without tags must stay unchanged so it isn't re-embedded. All 111 were in the dropped set, so the cleaner is there for future files.

Windows: "WEBASTO'S" was being cut into "WEBAST" and "ASTO'S". Windows now start and end on whole words and stay at 400 tokens or fewer. Checked on every real recall: no text lost, and window starts always move forward. Only 25 cards needed re-embedding.

Removed recalls: an upsert only adds and updates, so the loader now also deletes recalls that no longer pass the filter, and their vehicles and cards go with them through ON DELETE CASCADE. The upsert and delete run in one transaction, and the loader refuses to delete more than 10% of recalls without --allow-mass-delete, so an empty download can't wipe the database.

Limitations:
- The cleaner only handles anchor tags. Other HTML in a future file would pass through.
- A single word longer than a window, such as a long URL, is still cut mid-way.

## Module 3 - Retrieval modes
Four search modes return one hit per recall: dense, keyword, hybrid and rerank. Every mode takes the chunker as a setting. Make, model and year filters use the recall_vehicles table through EXISTS, so a recall covering several matching vehicles isn't returned twice.

Dense embeds the question with the BGE query prefix through one shared function, so no mode can forget it. The chunker is validated and inserted as a literal rather than a parameter, so Postgres always knows which partial HNSW index applies. pgvector 0.8.7's iterative scan keeps pulling cards until the limit is filled, and results are re-sorted by exact distance afterwards. For a narrow filter such as FORD RANGER 2021 (6 recalls), Postgres skips HNSW and does an exact scan, which is correct, so the index isn't forced.

Keyword uses Postgres's built-in full-text search with ts_rank_cd, because pg_textsearch (BM25) isn't in the pgvector image. Words are combined with OR rather than websearch_to_tsquery's AND, so a natural question like "my car takes longer to stop" still returns results. Each word goes through plainto_tsquery, so question text is never pasted into a query string.

Hybrid fuses dense and keyword with reciprocal rank fusion (k = 60) in one SQL query. Each list is collapsed to one card per recall before fusing, so a recall with three field cards can't push others down. Ranks are used rather than scores because cosine similarity and ts_rank_cd are on different scales. Ties are broken by dense rank, with recalls missing from the dense list last, then by campaign ID, so results don't change between runs. Each list puts forward a fixed pool of 50 recalls whatever k is; when the pool was tied to k, the top 3 changed depending on whether 3 or 30 results were requested. The fusion SQL is shared with fuse_ranks(), so the hand-calculated RRF test covers the real query.

Rerank re-scores hybrid's top 30 with the ms-marco MiniLM cross-encoder. For the field chunker it reads one card per recall: the closest card by embedding distance, or the top keyword card if the recall isn't in the dense list. Layer 2 will show whether that costs accuracy.

Limitations:
- No BM25. ts_rank_cd is a simpler keyword ranking.
- Retrieval always returns results, even when nothing matches. "brake problems" filtered to a 2021 Ranger returns seat-belt and child-seat recalls, because no Ranger 2021 brake recall exists. The answer step has to decline.
- Without a filter, a vehicle named in the question is only a weak signal: "2021 Ranger brake hose" returns brake hose recalls for any vehicle.
- Rerank can only re-order hybrid's top 30, so it can't recover a recall hybrid missed.

## Module 4 - LLM Answers with Citations
openai SDK (3.x) over OpenRouter’s SDK or raw HTTP: one standard interface across providers, typed errors that map to the retry rules, and a fake transport can be injected for tests.
SDK retries off (max_retries=0): the SDK already retries 408, 409, 429 and 5xx. Stacked with mine, that’s up to 12 requests, including codes I don’t want retried.
Retry rules:
429 and 5xx: back off and retry, honouring Retry-After.
402: stop; the credit cap is hit.
Other 4xx: stop; the same request fails the same way.
Timeouts: not retried; 4 × 60 s is too long, and the request may already be billed.
Connection errors: retried; the request never arrived.
Retry-After over 30 s: stop and show the wait; retrying early only gets another 429.
Three statuses (answered, declined, invalid): so an empty or cut-off reply can’t pass as a decline.
Decline only on the exact marker: a decline with extra text can hide an uncited claim. The cost is that some honest declines count as invalid, and run 3 did exactly that (NO_RECALL_FOUND). Measure how often in layer 2 before loosening.
Citations checked by membership in the retrieved set, not by ID format: a well-formed ID can still be invented. A source number like [2] counts as invalid.
Prompt uses the real ID format ([24V123000]) and unbracketed source labels: models copy formats they see, and the first draft would have made correct citations fail the check.
Prices in settings with the date checked; “unknown” if not priced; actual charge printed beside the estimate: prices change, and the charge checks the estimate.
perf_counter, not monotonic: on Windows with Python 3.12, monotonic has 15.6 ms resolution.
Retry tests use the real SDK with a mock transport: they test the SDK’s real error handling, not my guess of it. That caught a crash on connection errors before any real call.
argparse: no new dependency for two commands.
gemini-2.5-flash-lite: $0.10/$0.40 per million tokens, and thinking is off by default, so no reasoning tokens eat max_tokens.

## Layer 2, module 0: every component per recall

- The loader kept only the first raw row's component. 1,497 of 7,514 recalls had more than one, and 1,006 had one in a top-level category their card never shows. Example: 16V955000's card says seat belts, but three of its four components are airbag inflators.
- Added a recall_components table with every component. Left recalls.component and the cards unchanged, so layer 2 measures retrieval as it was built. Listing every component in the card header comes after 2.2, so that change gets measured too.
- Components are stored per campaign, not per vehicle. component_vehicle_link.py found 0 campaigns where a category covers only some of the vehicles, and 0 where it sits only on vehicles the filter removed. Its tests show it does find both cases when they exist.
- Still open: recalls.component depends on row order in the raw file.

## Layer 2, module 1: the golden question set

- 150 questions: about 100 generated from the database fields, plus 30 hard ones and 20 unanswerable ones I write myself. Generated questions have known answers but are easier and repetitive, so generated and handwritten results are always reported separately.
- Gold answers come from recall_components, not the card's single component, so 16V955000 counts as both a seat belt and an airbag recall.
- Component groups use NHTSA's top-level categories as exact strings, never a prefix match.
  - Brakes is every SERVICE BRAKES category plus PARKING BRAKE, because an owner says "brakes" for any of them. A Ranger with a hose recall and a parking brake recall has two brake recalls, not one.
  - Fuel is every FUEL category, including diesel and FUEL/PROPULSION SYSTEM. For a hydrogen car the tank is the fuel tank.
  - EQUIPMENT, VISIBILITY, LATCHES/LOCKS/LINKAGES and STRUCTURE are left out of component questions, because no name an owner would use fits everything in them. An RV emergency exit window isn't a "body structure" problem to its owner. They still count for "What recalls affect...".
  - Backup camera leaves out two kinds of component: the pedestrian warning sound (WARNINGS: EXTERNAL/PEDESTRIAN ALERT) and reverse automatic braking. One sampled question, the 2022 Pilotcar PC-4, had a sound recall in its answer key. A recall with both a camera and a sound component still counts.
- Sampling: 20 questions per template, 5 per component group across the remedy, consequence, existence and count templates, each vehicle used once, fixed seed. Equal numbers keep template scores comparable. 5 per group is too few to report groups on their own.
- No recall is gold in more than one question. One recall usually covers several model years, so the same seat belt recall came back for both the 2018 and 2019 Lexus LX570. Repeats aren't independent: one retrieval miss would cost two questions.
- Count and all_recalls questions only use vehicles with 2 to 10 gold recalls. With one gold recall, an assistant that finds one recall and stops would still pass, so the question wouldn't test finding every recall.
- At most 5 questions per make. The dry run showed it never binds (the highest make had 4), so it's a safety limit, not something shaping the sample.
- The sample follows NHTSA's catalogue, so many questions are about trucks, buses, RVs, trailers and motorcycles. I kept that instead of stratifying: it's honest and easy to explain, the count and all_recalls rule already puts 40 questions on vehicles with several recalls, 2.2 reports results by the vehicle's total recall count, and my handwritten questions cover mainstream cars.
- Jaguar and Land Rover are excluded, from my own questions too. It's public data, but I grade the answers by hand and don't want any question of inside knowledge.
- Vehicles with more than 10 gold recalls are skipped, because retrieval returns 10 results and "cite every gold recall" would be impossible.
- Remedy and consequence questions say "recall" or "recalls" to match the number of gold recalls, so a question never sounds like it has one answer when it has two.
- Display names use a simple capitalisation rule plus overrides for codes and brands, checked by eye against the final 100. Names don't change which vehicles are sampled.
- Leakage: the 5-word check blanks out the row's own vehicle name first, because every question has to name its vehicle and a 5-word name tripped the check on its own. My handwritten rows get the same check as a warning.
- Pass rules were fixed before any answers exist. An unanswerable question passes only on a proper decline. A malformed decline like NO_RECALL_FOUND counts as invalid, not as a pass.
- The gold query lives in one place in the package (evals/gold.py), used by the generator, the validator and the tests. Before, the validator had its own copy.
- pydantic is now a direct dependency because GoldenRow uses it directly. Before, it only came in through pydantic-settings. The alternative was dataclasses with hand-written checks.
- The generator excludes model year 9999 even though the parser turns it into NULL, so a raw value can never produce "the 9999 Ford Ranger".
- Known biases: existence questions always have the answer "yes", and groups follow NHTSA's labels, so a backup camera fault filed under electrical isn't gold for "backup camera".
- Still open: move the unknown-year constant into the parser (ingest/ was out of scope for this module). NHTSA spells one make two ways (MERCEDES-BENZ and MERCEDES BENZ), so a make filter misses recalls filed under the other. Reference texts include boilerplate like mailing dates and phone numbers, so 2.3's answer checks must look for the fix itself.

## Layer 2, module 2: retrieval metrics

- Headline numbers are unfiltered hit@5 and MRR, per mode and chunker, with generated and handwritten rows reported separately. Filtered numbers are secondary: with the filter on, a vehicle with 10 or fewer recalls gets every recall into the top 10 whatever the order. The 2021 Ranger has 6 recalls, so filtered hit@10 is 100% even when the right one comes 6th.
- Results are also broken down by the vehicle's total recall count: 1, 2-3, 4-10, more than 10. Filtered numbers only say something about ranking in the last bucket.
- recall@k divides by the number of gold recalls, not by k. Three rows have 6 gold recalls, so they can score at most 5/6 at k = 5. Dividing by k would cap every single-gold row at 0.2.
- MRR uses the rank of the first gold recall, 1-based, and 0 when none is found.
- Latency is p50 and p95 per configuration, nearest-rank, after one untimed warm-up call per mode and chunker. With 130 calls, p95 is the 124th fastest.
- Hidden components are scored per (question, gold recall) pair, keyed by component group and campaign. 16V955000 is visible for a seat belt question and hidden for an airbag question. This is the number the card-header change should move.
- Rerank top scores are recorded for answerable and unanswerable rows, per chunker and filter. Measured only; no threshold is set from them yet.
- Run labels: baseline first, then cards_all_components after the card header lists every component. Each label has its own section in results.md, and re-running a label replaces only that section.
- Decline rule for the 15 real-car unanswerable rows: "no airbag recall on this car, but here are the others [IDs]" counts as answered, not declined, and fails.
- The eval logic lives in src/recall_assistant/evals/retrieval_eval.py and scripts/eval_retrieval.py only wires the command line. The 2.1 validator crashed outside pytest because its code only worked on pytest's import path. The tests now import the package the same way the script does.
- The run refuses to start on a dirty tree, and the JSON records the commit, the card seal and every retrieval setting, so a result can always be tied to the code and cards that produced it. Code is committed before any run.
- A duplicate campaign ID in a hit list stops the run instead of being dropped. One hit per recall is something retrieval promises, so dropping repeats quietly would hide a bug.
- results/ is git-ignored and the baseline JSON is added with --force, so the per-question ranks are kept with the code.
- Still open: 22 defect summaries contain a mis-decoded non-breaking space (Â). MERCEDES-BENZ is spelt two ways in the NHTSA file.

## Layer 2, module 4: hand labels and a calibrated judge

- Labelled the unfiltered hybrid record answers: the default configuration and the harder number, with 46 code-check fails and 104 passes, so the judge gets both classes. The filtered set had 2 faithfulness flags and too few fails to test a judge on.
- 100 rows in three strata, not a random 100: all 20 unanswerable, all 30 handwritten, 50 generated at a fixed seed. A random draw would have given about 18 handwritten rows, and those are the hardest.
- A hand label judges the content against the reference, not the format. The judge sees the reference and the candidate, never the cards, so it can only judge content. Labelling format by hand would make the two disagree by construction, and the code checks already measure format. The label tool hides status, code checks and faithfulness, and a test checks they aren't printed.
- One exception, set at row 24 and written into evals/README.md: a cited campaign ID that does not correspond to any retrieved card fails the answer whatever the question asked. An owner who looks up a made-up ID finds nothing. A real card cited for the wrong year or vehicle is noted, not failed. A manufacturer code for a retrieved card (Audi's 90S9) is noted, not failed. Count questions pass on the number alone.
- Labelling found the answer prompt's example ID, [24V123000], copied into 8 of 150 answers as a real citation, plus 1 copy of the second example. Code checks passed them because the shape is valid; faithfulness passed them because the sentence content was in the cards. Three of the 8 are in the labelled 100 and all three have correct content. Not fixed here, because the labels are against the 19 Oct answers. Fix in 2.5: point the prompt at the card IDs with no literal example, and add a code check that every cited ID is in the retrieved set.
- Labels: 74 pass, 26 fail. By stratum: unanswerable 20 (1 fail), handwritten 30 (7 fail), generated 50 (18 fail). Most generated fails are list and count questions, and most of those are retrieval misses: the gold recall was not in the ten cards, so the model could not list or count it. Where the gold card was missing, one row declined correctly (Entegra Aspire) and one answered confidently from neighbouring recalls (International HV 2018). Same cause, opposite behaviour. Both are 2.5.
- A malformed judge reply is a third verdict, error: left out of every metric, listed in the report, never cached. Counting it as fail would move kappa for a formatting hiccup.
- The judge cache key includes a hash of the prompt text as well as the version name: everything that changes the output is in the key, so editing a prompt in place can't return a stale verdict. Not quite everything, it turned out: the key does not cover what judge() does to the candidate text, so after the fix below the cache had to be deleted by hand.
- Dev and test are a 50/50 split by seed after sorting by id. The prompt is tuned on dev only; test is reported, never tuned on. Each half has about 13 fails, so a kappa difference of a few hundredths between prompt versions is noise. I read the confusion matrix, not the third decimal. In the matrices, positive means pass: TP both pass, FP judge pass and human fail, FN judge fail and human pass, TN both fail.
- First run, prompt v1: dev kappa 0.33, test 0.38. On dev, 13 of 50 rows were judge fail, human pass, and 8 of those were unanswerable rows. The cause was in judge(), not the prompt. It replaced the decline marker with the sentence "The assistant declined to answer." before building the judge prompt, so no prompt rule about the marker could ever apply, and the judge read the substituted sentence as declining when the reference "had content". No test covered it; calibration found it. Fixed by sending the candidate text unchanged.
- Prompt v2 adds four rules written against the dev disagreements: the reference is the minimum correct content, not the maximum; judge what the question asks for, not every field in the reference; the exact marker string is the correct answer when the reference says no recall; an extra recall alongside the correct one is not a wrong-vehicle error. v1 stays in the registry so its runs stay reproducible.
- Results table in docs/results.md, one row per run with commit hash. With the marker fix and the same labels, v1 went to test kappa 0.64 and v2 to 0.66. So the code fix was most of the gain and the prompt rules were one or two rows on held-out test, however good they looked on dev (0.60 to 0.85).
- The judge found two of my labels wrong, both unanswerable rows: hw_037 was a correct decline I had marked fail, hw_044 asserted a recall for the wrong model year and I had marked pass. I re-checked all 20 unanswerable rows the same way, not just the two the judge flagged, found no others, and corrected both. Final numbers after correction: v1 test kappa 0.73 (31/0/6/13), v2 test kappa 0.76, agreement 0.90 (33/1/4/12). v2 dev 0.85 (35/1/2/12).
- Code checks against the hand labels over all 100: both pass 69, code pass and human fail 5, code fail and human pass 5, both fail 21. Agreement 0.90. The deterministic checks agree with me as often as the v2 judge does on test. The judge adds the ability to read content; on this data

## Module 2.5a: failure analysis worksheet and tally (22 Oct 2026)

- Three failure signals per answer: human label, code-check pass rule, judge verdict. Combined as a union, not an intersection, with the signals recorded per row. Reason: the rows only one signal catches are the ones worth reading (Tucson: judge only; hw_004: human and code, judge passed).
- Worksheet covers all 150 scored answers, not the 100 labelled. 25 of 57 rows are code-only with no human note or judge reason.
- Numbers: code fails 46 of 150, human fails 26 of 100, judge fails 30 of 100, union 57. Signal combinations: code only 25, all three 19, judge only 6, human and judge 5, code and human 2. No row failed code and judge without the human; cross-checks against the 2.4 matrix (both fail 21, code-fail/human-pass 5, human-fail/code-pass 5) all agree.
- Categories fixed in a legend in code; tally matches the leading number, refuses empty or unknown. Separator is ";" because category names contain commas. The note after "(" is ignored by the parser.
- Legend changed during the read: 3 widened to "wrong year or vehicle" (E-class card on a C 300, 22V154000 on a Grand Cherokee L, the 4700 on a 47X); 11 added for correct answers the judge failed (Aprilia, hw_022).
- Tally: 57 rows, 31 with more than one category, 97 tags. 1: 2, 1b: 11, 2: 16, 3: 22, 4: 4, 5: 11, 6: 2, 7: 1, 8: 21, 9: 0, 10: 5, 11: 2.
- Reading: 3 and 2 (38 tags) point at retrieval structure, a year filter on cards and lookup by vehicle for list and count questions, which is layer 3. 5 is the prompt example (8 of 11) and 2.5b. 8 is split: bare numbers and ID lists are the pass rule being stricter than the question; "Source N" and bold IDs are the model copying card layout. 10 is the golden batch before 2.6. 9 is zero because the Ram 1500 case was a note on a passing row.
- Correction to 2.4: the code checks failed all 8 prompt-example answers (7 bad_citation with the ID in invalid_ids, Oshkosh uncited because its IDs were bold with no brackets), and invalid rows have no faithfulness score. The 2.4 note said code and faithfulness passed them. What stands: the code flagged the symptom; reading found the cause. invalid_ids already is cited_ids_not_retrieved, so 2.5b item 2 reduces to confirming a test exists.
- 8 of 150 answers contain a prompt-example ID, not 8 plus 1: the 25V456000 copy is in the Lucid Air answer alongside 24V123000.
- invalid_ids kept as a set for the membership check and shown once per ID (Western Star cites its typo twice).
- Hidden-tail checks: 21V421000 does not cover a Silverado 1500 (hw_038); 26V332000 and 24V064000 cover the Civic to 2022, not 2023 (hw_037); 22V617000 covers Escalade/Suburban/Tahoe/Yukon only (hw_042). No label changed.
- Golden loader opened files without an encoding, so hw_002, hw_019 and hw_028 reached the model as "Iâ€™ve" in every retrieval eval and answer run since 2.1. Fixed; the 19 Oct answers and labels stand as they were produced; 2.5b's re-run is not byte-identical on those three questions.
- Export is never re-run after categories are written. Validation runs before the dirty-tree guard in the tally so a refusal doesn't need a clean tree; the guard still precedes the write.
- Deferred to layer 3: lookup by vehicle for count and all_recalls; year filter on cards; two forms of decline.

## Module 2.5b: prompt example ID removed, three wordings measured (23 Oct 2026)

- Problem: 8 of 150 answers in the 19 Oct run cited 24V123000, the made-up example in the system prompt (the Lucid Air answer also cited 25V456000). Category 5 in the worksheet.
- Method: one prompt change at a time, each one re-run on all 150 questions and compared with the 19 Oct baseline by compare_prompt_fix.py (pass rule by source and template, invalid reasons, example-ID count, bare-bracket count, HHEM summary). The pass rule was not changed at any point: changing the metric after seeing a result is the same as rewriting a test to pass.
- Runs, in order (prompt commit; copies / bare brackets / uncited / bad_citation / passes of 130 / unanswerable declined-invalid-answered / HHEM rows, median, flagged): 19 Oct baseline (44cce59; 8 / 8 / 12 / 11 / 85 / 19-0-1 / 101, 0.955, 14). v1 (4ebb361; 0 / 3 / 34 / 7 / 70 / 19-0-1 / 81, 0.961, 7). v2 (4a75741; 0 / 66 / 2 / 4 / 92 / 19-0-1 / 114, 0.534, 51). v3 (f77c452; 0 / 0 / 8 / 11 / 91 / 17-2-1 / 109, 0.961, 6). Sections in results.md: "Prompt-fix comparison (Module 2.5b)" for v1, then ": v2", ": v3", ": v1-vs-v2", ": v2-vs-v3".
- v1 ("each source header shows its campaign ID"): copying stopped, but the example had also been showing the shape of a citation. Count answers became a bare number, existence answers echoed the header ("Source 2 (campaign 21V351000) states..."), six answers cited source numbers like [2, 6]. uncited 12 to 34, passes 85 to 70. A regression, recorded as one.
- v2 (ID shape described as two digits, V, six digits; "never the source number"): uncited 2, passes 92, the best pass count of any run. But 66 answers were nothing but a bracket of IDs, 18 of them to handwritten questions. The pass rule only asks whether the gold IDs are cited, so it passed them; HHEM has no claim to check and scored them around 0.2. Scorer checked first: 36 answers identical between 19 Oct and v2 scored identically, so the drop was in the answers, not the tool.
- v3 (v2 plus "write the answer as one or more sentences, with the citations inside those sentences"): bare brackets 0, HHEM median 0.961 with 6 flagged, passes 91. The pass rule and HHEM agree for the first time since the baseline.
- Decision: v3 goes into layer 3. Better than the baseline on passes, faithfulness, bare brackets and uncited; equal on bad_citation. No v4: the remaining defects are structural, and both wording iterations moved something they were not aimed at.
- Open, layer 3: 8 of v3's 11 bad_citation rows are source numbers ([3], [5, 5]); map [N] to the Nth source's campaign ID in code, which no wording can guarantee. hw_034 and hw_048 declined with NO_RECALL_FOUND, one word short of the exact marker, and were scored as uncited answers; a structured decline, not a string match.
- Caveats that stay in every 19 Oct comparison: hand labels and judge rows are against the 19 Oct answers; hw_002, hw_019 and hw_028 reached the model with a broken apostrophe on 19 Oct and correctly on every 23 Oct run.
- --tag added to run_answers.py so a second run of the same configuration on the same day gets its own files and its own results.md section instead of overwriting the first.
- Cost: about 5p per 150-answer run, four runs. The v3 run was interrupted three times by an upstream rate limit and still cost 5p because the answer cache kept every completed call.

## Layer 2, golden-set fixes

- 18V725000 stays out of the gold for gen_all_recalls_MERCEDES_BENZ_C_300_4MATIC_2018. recall_vehicles files it under MERCEDES-BENZ / C 300 / 2018 (and E 300); the 4MATIC row's three gold campaigns sit under MERCEDES BENZ (no hyphen) / C 300 4MATIC. The generator matched the exact triplet, so it was right and my worksheet note "may be valid" was wrong. The answer cited a recall for a different variant: category 3, not 10. Worksheet row changed from 2;6;3;10 to 2;6;3 and the tally regenerated: category 10 from 5 to 4, 96 tags.
- 90S9 rule: the code's rule stands. A manufacturer code in brackets is marked bad_citation and fails the row even when the card was retrieved, because an owner who looks it up on NHTSA finds nothing. The README said "noted, not failed" and was wrong; one sentence rewritten. The hand label still judges content and may pass the row. 19 Oct numbers unchanged (handwritten 23 of 30, 6 invalid; bad_citation 11).
- INTERNATIONAL_HV_2018 kept as a hard retrieval case, not an ambiguous question. The two gold campaigns are fleet-wide filings (19V216000 against 23 make/model/year rows, 20V219000 against 25, across IC Bus and International for 2018 to 2021); International HV 2018 has five campaigns in total. Neither gold was retrieved on 19 Oct; 22V691000 was.
- Three generator limits documented, no fix: NHTSA component tags that name the filing category rather than the part (20V767000 placard under wheels, 22V615000 tire label, 20V284000 brake lines near a suspension part). All three are wrongly_declined, tagged 2;10.
- Layer 3 note: NHTSA spells one make two ways (MERCEDES-BENZ, MERCEDES BENZ); joins the converter-makes item.

## Module 2.6: CI eval gate

- Baseline is measured on the sample, not the full index. The gate runs against a 692-campaign sample in CI, so the baseline comes from a database holding only that sample (scratch database recall_ci, created with createdb). Measuring it on the full index would compare different exams.
- Hard negatives: for each of the 25 answerable smoke questions the sample holds its gold campaigns plus the 30 hybrid/record candidates the reranker sees (rerank_candidates = 30). That gave 692 unique campaigns, so no random filler was used. Golds plus filler would make hit@5 1.0 forever.
- Smoke set: 30 questions at seed 42 (3 per generated template, 10 handwritten answerable, 5 unanswerable); 25 scored. Threshold: more than 5 points on hit@5 for hybrid_record_unfiltered or rerank_record_unfiltered. One question is 4 points on 25, so the gate fires on two or more extra misses.
- Baseline (commit 715c636): hybrid_record_unfiltered hit@5 0.96 MRR 0.7647; rerank_record_unfiltered hit@5 0.96 MRR 0.9067. Eight configurations recorded; only the two unfiltered ones are gated.
- The gate is a job in ci.yml with no path filter. protect-main requires a named check; a separate path-filtered workflow would leave the check pending on PRs that do not touch retrieval.
- The answer-level check never fails the job (continue-on-error, exits 0, runs only when OPENROUTER_API_KEY is set). After the 23 Oct 429s an LLM call is not a gate.
- baseline.json records the commit and SHA-256 of ci_sample.tsv and smoke.jsonl, hashed after normalising CRLF to LF because the checkout is CRLF on Windows and LF in CI; --check refuses with "baseline stale" before connecting if either differs.
- update_baseline.py refuses on a dirty tree with no override, and refuses if the target database already holds more campaigns than the sample, so it cannot wipe the full index.
- The TSV is written with "\t".join, latin-1 strict, rows sorted by campaign_id, make, model, year, "\n" endings: a rerun on unchanged data is byte-identical (checked by hash). Components round-trip; full field comparison on all 692 campaigns.
- make_ci_sample.py is run rarely: the TSV is 6.99 MB and every regeneration adds that to history. Two .gitignore exceptions (tests/data/ci_sample.tsv under data/, results/baseline.json under results/).
- The CLI integration test runs on the five seed campaigns with --sample and --smoke, not the real sample. The first version embedded all 692 and took 295 s; suite 511 passed in 2:00 after the change.
- Regression demo, commit b228dde: Retriever.rerank default rerank_candidates 30 to 1. lint-and-test green; eval-gate red: rerank hit@5 0.96 to 0.64 (16 of 25, which is hybrid hit@1), hybrid unchanged, exit 1, identical output locally and in CI. PR base was layer-2-evals so the diff is one line; eval-gate is a required check on main.
- Found on the way: the eval loop calls retriever.rerank(question, k=10) and takes everything else from the method defaults, so Settings.rerank_candidates is not what the gate measures. Two sources of truth; layer 3 item.

## Module 2.7: parser quoting (10 Nov 2026)

- Found by the 2.6 round-trip test after R2 switched the TSV writer to tab-join: a seed field starting with a double quote lost its quotes through parse_recalls. The parser used csv.reader's default quoting; NHTSA's file has none. Measured before fixing: 6 raw rows, 2 campaigns, 1 vehicle row missing from the database (22V176000 TAOS 2022).
- Fix is quoting=csv.QUOTE_NONE and nothing else. Parser tests: an unclosed leading quote no longer swallows the next row; a closed one keeps its quotes.
- Reload, not a rebuild: the upsert added one vehicle row, the chunk upsert cleared and re-embedded only the four cards whose header changed. Both card seals moved; old and new recorded in results. All layer 2 numbers stand as produced, with the caveat that they ran on cards missing that row.
- 2.6 R2 outcome: _load_smoke_rows was a copy of _load_jsonl (import now); the three recall-loading queries were in two places (now load_recall_rows_by_ids in the loader, used by the exporter and the round-trip check); the dirty-tree test's "must not connect" patch targeted ConnectionPool while the script calls psycopg.connect, so it could never fire (patched on psycopg.connect now); the TSV writer was csv.writer, not the approved tab-join, and 1,641 of 6,053 rows had quoted fields; writer changed, sample regenerated (0 quoted fields), baseline rerun with every quality metric unchanged. Declined: moving the OpenRouter secret from job-level to step-level env until the key exists and the step's if can be verified.
- Latency fields in baseline.json are informational and change on every run; candidate for removal.

## Module 3.1: FastAPI service

- Streaming is real: tokens go to the client as the LLM produces them. The first plan ran the whole answer and then sent the finished text word by word; that would have made time to first token equal to total latency. Both paths call the same classify(), so the done event's status is the one the non-streaming route would have returned.
- Clients take citations from the done event, never from token events. Gemini split "22V176000" across two chunks in the first real run.
- stream() performs the upstream request before returning an iterator, so a spend-limit stop is a plain 503, same as the non-streaming route. The cost: the sources event waits for the upstream to accept the request, so sources and the first token arrive together. Time to first token at the client therefore includes the LLM connection wait. That is the definition 3.4 uses.
- API_KEY fails closed: the app refuses to start without it. No "auth off" mode.
- db_pool_max (5) is a new setting. settings.pool_size (50) is the retrieval candidate pool and would have opened 50 Postgres connections.
- latency_ms on the /ask route and in the done event covers retrieval plus the LLM call. The middleware's latency is time to headers, which for a streaming response is before the answer is complete; use the route's line.
- Longer backoff on OpenRouter 429 deferred again: stream() shares generate()'s retry loop, so it is one change for both and belongs with the load test in 3.4.

## Module 3.2: Docker

- CPU torch comes from the PyTorch CPU index as a uv source for torch, not from excluding CUDA packages at install time. The first plan listed 19 --no-install-package flags; that installs the CUDA wheel and leaves out its libraries, and breaks the next time torch renames a package. The index change also removed the same 19 packages from CI, which had been installing them on every run since layer 1.
- torch stays at 2.14.1. uv lock re-resolved torch to 2.15.0 when its source changed; I pinned it back so layers 1 and 2 keep their measurement basis. Upgrading torch is a separate decision with its own numbers.
- Models are baked into the image at build time (217 MB) and loaded with HF_HUB_OFFLINE=1, so a cold start never downloads. The image is large (see results); the alternative is a slow first request after scaling from zero.
- The container test reaches Postgres through host.docker.internal with --add-host=host.docker.internal:host-gateway, which works on Docker Desktop and on the Ubuntu runner. --network host does not work on Docker Desktop.
- In CI, missing Docker fails the test; locally it skips. A skip in CI would pass green with nothing tested.
- One docker-build job builds the image once with the GHA cache and runs the container test against that tag. The test builds its own image only when RECALL_IMAGE is unset.
- The compose api service sets only POSTGRES_HOST=db; everything else comes from .env. ${VAR:-} defaults were removed because they set empty strings that override Settings defaults.