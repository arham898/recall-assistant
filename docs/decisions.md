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