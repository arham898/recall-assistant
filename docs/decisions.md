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