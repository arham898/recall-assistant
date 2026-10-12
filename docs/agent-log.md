# Agent log

Record of AI agent actions during development — what the agent did, how I caught issues, and what I changed.

## Format

| What the agent did | How I caught it | What I changed |
|------|--------------------|-----------------|----------------|
| Generated initial project scaffold | Reviewed all files before committing | N/A — first commit, reviewed and approved |

|Removed test_no_seq_scan during a fix without saying so. I caught it from the test count (48 → 47). The reason was valid (redundant once seq scans are disabled in the test), but it should have been flagged.|

|Bumped setup-uv to a tag that doesn’t exist (@v10), assuming every action publishes major-version tags. I checked the repo’s tags before pushing.|

