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
