# Agent log

Record of AI agent actions during development — what the agent did, how I caught issues, and what I changed.

## Format

| What the agent did | How I caught it | What I changed |
|------|--------------------|-----------------|----------------|
| Generated initial project scaffold | Reviewed all files before committing | N/A — first commit, reviewed and approved |

|Removed test_no_seq_scan during a fix without saying so. I caught it from the test count (48 → 47). The reason was valid (redundant once seq scans are disabled in the test), but it should have been flagged.|

|Bumped setup-uv to a tag that doesn’t exist (@v10), assuming every action publishes major-version tags. I checked the repo’s tags before pushing.|

|Agent said the FORD/RANGER/2021 filter matched "~1 campaign" to explain why Postgres skipped the HNSW index, however, it's own results shpowed 6 Ranger 2021 recalls. I asked it to explain/analyse.|

|Asked for dense and hybrid plans for each chunker. It returned only dense and keyword. Found it by checking the output against the request. Challenged.|

|Claimed HNSW gives "~99% recall at our scale". Completely false figure as this is what layer 2 does which is not impemented yet. Removed the figure|

|Plan review caught three bugs before any code development. 1.3 plan used SET LOCAL with a parameter, which psycopg 3 can't send. Also proposed an RRF test built from random fake vectors that couldn't produce the tie it claimed to test and finally had a punctuation test that returned before reaching Postgres. Reviewed plan before approving it with bug fixes in place|

|Planned from memory assuming httpx when the SDK now used HTTPX2 and hard coded a model ID. Plan was reviewed and changed against the installed SDK with no default model|

|Used [24V-001] as the citaiton example and labelled sources [1], while real data shows ID's as 24V13000 so correct citations would fail it's own parser. Reviewed and changed.|

|Integration tests were wrapped in a fake retriever so the real Retriever was never tested. Reviewed and updated to use the real Retriever.|