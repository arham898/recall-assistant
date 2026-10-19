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

|R2 before the 2.0 commit found a dead marker variable, a commented-out import, and test helpers re-running the whole script on every row. Fixed before committing.|

|2.1 build used the wrong category name, then edited the query result by hand instead of re-running it. Caught because the plan contradicted itself. Asked for the query to be re-run with raw output.|

|2.1 quota "Option B" added up to 85 questions, not 100. Caught by doing the sum.|

|Rewrote an approved test to match the code and only said so afterwards. Caught by reading the summary. The code was fixed and the test put back.|

|Wrote permanent "mutation tests" that copied the generator into the test file, broke the copy and showed the copy failing. The real code was never touched. Caught by listing the test classes. Removed them and ran the mutations as one-off edits to the real code.|

|Called NHTSA's back-over prevention category a fit for "backup camera" after looking one level down. Two levels down it held 18 pedestrian warning sound recalls, one already in the sample. I asked for the next level. Sound and reverse auto-braking are now excluded.|

|The validator kept its own copy of the gold query and imported it in a way that only worked under pytest. All 219 tests passed and it crashed the first time I ran it. Query moved to evals/gold.py, and a test now runs both scripts the way I do.|

|The validator printed "No rows to validate." and passed with nothing to check. Caught by reading the output.|

|Skipped the red run (test failing before the fix) twice, once because Docker was down. The matching break-it-on-purpose run stood in.|

|Said "148 existing + 46 new" tests. Caught by adding up the per-file counts: 158 + 36.|

|2.2 plan swapped the shared gold query for a top-level category copy and called it simpler. The copy ignored the backup camera exclusions. Caught in plan review. Now uses build_group_where with the card column.|

|Scored hidden components per row on a subset, not per gold recall. Caught in plan review. Now a found rate per (row, gold recall) pair.|

|Listed a mutation (filtered runs pass no filter) with no test that could catch it, because the aggregation tests bypassed retrieval. Caught by checking each mutation against the test list. Added test_filter_kwargs.|

|Planned to restore untracked new files with git checkout after mutations, which can't restore a file git has never seen. Caught in plan review. New files are staged first.|

|Planned to drop duplicate campaign IDs silently, which would hide a retrieval bug. Caught in plan review. A duplicate now stops the run.|

|Said "no existing files modified" while planning to change tests/fakes.py. Caught by reading the file list.|

|Its aggregation fixture put a vehicle with 2 gold recalls in bucket "1". Caught by reading the fixture.|

|Keyed visibility by recall only. 16V955000 is visible for seat belts and hidden for airbags, so it has to be keyed by group and recall. Caught in plan review.|

|Planned to add test recalls to the shared conftest.py fixture without listing it, which could have changed 2.1's brakes test. Caught in plan review. The integration tests got their own fixture.|

|Planned to run the baseline before the code was committed, so git_commit and dirty_worktree would have meant nothing. Caught in plan review. Commit first, then run.|

|R2 found --limit 5 ran all 20 unanswerable rows and printed "80 calls" when 160 ran. Fixed to take the first N of each and print the calls actually made.|

|R2 found rows without a complete vehicle would get a no-op "filtered" run. Fixed to run those unfiltered only and print the count.|

|R2 found the tests imported the script through a sys.path trick. The logic moved into src/ and the script only wires the command line.|

|Recorded the command as " ".join(sys.argv), which drops "python". Caught by reading the code. Now prefixed.|

|2.4 plan returned fail for any malformed judge reply, so a formatting hiccup would have been scored as a real fail and cached. Caught in plan review. Added an error verdict: excluded, listed, never cached.|

|2.4 plan keyed the judge cache on the version name only, so editing JUDGE_PROMPT_V1 in place would have hit the cache and shown no change. Caught in plan review, same as the 2.3 prompt hash. Hash of the prompt text added to the key, with a cache-miss test.|

|2.4 plan copied _build_llm_client from run_answers.py into calibrate_judge.py with the temperature changed. Caught in plan review. Moved to openrouter.py as build_openrouter_client; both scripts import it.|

|2.4 plan listed a "refuse when n < 50" test and the summary said all tests passed, but the test was never written. Found by R2. Added it.|

|The split and hidden-field tests checked things a broken version also does (deterministic, disjoint, labels written). Deleting the shuffle and printing the status both passed 41 of 41. Added a not-sorted-halves test, a two-seeds-differ test and a capsys test; each fails on its mutation.|

|Asked three times for retrieved_texts, the source counts and an unanswerable reference from the files; each revised plan asserted them and showed nothing. Checked all three myself with one-line commands.|