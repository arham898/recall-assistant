# Golden Evaluation Set

## Schema

Each row in the JSONL files is a JSON object with these fields:

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique identifier |
| `question` | string | The question to ask |
| `source` | `"generated"` / `"handwritten"` / `"unanswerable"` | How the row was created |
| `template` | string or null | Template name for generated rows |
| `component_group` | string or null | Component group key (null for all_recalls and handwritten) |
| `gold_campaign_ids` | list of strings | Campaign IDs that answer the question |
| `reference_answer` | string | Expected answer content |
| `reference_facts` | list of `{campaign_id, field, text}` | Backing facts from the database |
| `make` | string or null | Vehicle make (upper-cased, matches VehicleFilter) |
| `model` | string or null | Vehicle model (upper-cased, matches VehicleFilter) |
| `year` | int or null | Vehicle model year |
| `pass_rule` | `"cite_any_gold"` / `"cite_all_gold"` / `"decline"` | How to judge the answer |
| `tags` | list of strings | Optional tags |
| `notes` | string or null | Optional notes |

`reference_facts[].field` is one of `"defect_summary"`, `"consequence_summary"`, `"corrective_action"`.

## Example rows

```json
{
  "id": "hand_brake_ranger",
  "question": "What's the fix for the brake recall on the 2021 Ford Ranger?",
  "source": "handwritten",
  "template": null,
  "component_group": null,
  "gold_campaign_ids": ["24V123000"],
  "reference_answer": "[24V123000] Dealer will replace the brake hose",
  "reference_facts": [],
  "make": "FORD",
  "model": "RANGER",
  "year": 2021,
  "pass_rule": "cite_all_gold",
  "tags": [],
  "notes": null
}
```

```json
{
  "id": "unanswerable_flying_car",
  "question": "Are there any recalls for the 2025 Tesla Cybertruck flying module?",
  "source": "unanswerable",
  "template": null,
  "component_group": null,
  "gold_campaign_ids": [],
  "reference_answer": "",
  "reference_facts": [],
  "make": "TESLA",
  "model": "CYBERTRUCK",
  "year": 2025,
  "pass_rule": "decline",
  "tags": [],
  "notes": null
}
```

## Pass rules

- **cite_any_gold**: the answer passes when status is `"answered"`, the
  answer states the reference fact, and cites at least one gold campaign ID.
- **cite_all_gold**: as above, but the answer must cite every gold campaign ID.
- **decline**: the answer passes only when status is `"declined"`.
  `"invalid"` is recorded separately so the malformed-decline rate is measured.

Generated and handwritten results are always reported separately.

### Hand labels (evals/labels.jsonl)

A hand label is a judgement on the content of the answer against the reference answer, not on its format.

- Pass: the answer gives what the question asks for and it matches the reference. A count question needs the right count; a defect or remedy question needs the right defect or remedy. Paraphrasing is fine.
- Bracketed citations are not required for a pass. The code checks already measure those.
- A missing or wrong campaign ID is a fail only when the question asks which recall(s).
- Anything stated that the reference contradicts, or that is not in the recall data, is a fail, even if the rest is right.
- Unanswerable questions: a decline is a pass; any answer is a fail.
- The reference answer is the key. If the reference is wrong, fix the reference (and say so in the note), never pass the answer to match a wrong key.
- A cited campaign ID that is not among the retrieved cards fails the answer regardless of question type. A real retrieved card cited for the wrong year or vehicle does not fail by itself.

The note is one line and names the reason, so failures can be tallied later.

Citation rules, set during labelling on 19 to 20 Oct 2026:

A cited campaign ID that does not correspond to any retrieved card fails the answer, whatever the question asked. This covers fabricated IDs, IDs copied from the prompt’s own example, and one-digit typos. The test is “not among the cards the model was given”, not “not in NHTSA’s data”.
A manufacturer’s internal recall code (for example Audi’s 90S9) used in place of the campaign ID for a card that was retrieved is noted, not failed.
A real retrieved card cited for the wrong model year or wrong vehicle is noted, not failed, unless the question asked for the full list, in which case extras count against it.
Count questions pass on the number alone. A right number from a wrong set is a pass with a note.
A decline when the gold recall was not retrieved is still a fail; the note records that the cause is retrieval, not the answer.
Notes are kept terse and carry the campaign IDs involved, so they can be grouped in failure analysis.

Labels: 100 rows, 74 pass, 26 fail. By stratum: unanswerable 20 (1 fail), handwritten 30 (7 fail), generated 50 (18 fail).