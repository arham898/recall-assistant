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

|First fixture fix copied the seed code and left the pool built from Settings, which points at the real database. The next version added conftest imports after I said not to. Reviewed and changed both.|

|Retry tests swapped in their own SDK client, so max_retries=0 was never tested. Asked for a mutation check: after the fix, removing max_retries=0 failed 10 of 17 tests.|

|A research step ran uv add while still in plan mode, changing pyproject.toml and uv.lock before I approved anything. It reverted both itself, but openai stayed installed in the venv.|

|Changed the approach to the chunker fix without asking first, after being told to ask.|

|During E1, a results block from before the 1.2b cleaning (22,902 field cards) ended up dated 13 Oct. Caught by checking it against the current count of 22,545.|

|Renamed --model to --model-name without saying so, and the code comment gives the wrong reason (an argparse clash). The real reason is to avoid confusion with the LLM model setting.|

|2.1 plan stripped every fact from the reference answers, reading the leak rule (which is about questions) as covering answers too. Caught in plan review. Reference answers now carry the defect, consequence or remedy text.|

|2.1 plan dropped component text from the leak check without saying so. Caught in plan review and put back.|

|2.1 plan reported "0 campaigns have more than one component" as a finding, but that's true by construction because the column holds one value. The raw file showed 1,497. This became module 2.0.|

|2.1 plan labelled the all-category gold-size table as "20 groups combined", gave second-level "recall" counts that couldn't be recall counts, and said 30 categories when its table had 26. Caught by adding up its own numbers: the 28,241 total is the sum over all 41 categories.|

|2.1 plan had a shuffle test that sorted inside the test, and a gold-completeness test that recomputed gold with the same function. It claimed mutations would fail both; neither could fail. Sent back to use expected values known from the fixture.|

|2.0 plan twice stated a design decision as settled after I said it was mine: "known limitation" for the lost components, and "no vehicle link table needed" with no code or output. I made it a script with positive-control tests. The result held: 0 and 0.|

|2.0 plan guessed "~40k campaigns" to explain a reconciliation gap. results.md says 15,303, and there was no gap.|

|2.0 summary said the most components on one campaign is 4. psql showed 6 (20V771000).|