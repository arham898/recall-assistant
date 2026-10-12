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