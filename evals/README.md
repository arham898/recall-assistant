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
A manufacturer’s internal recall code (for example Audi’s 90S9) cited in place of a campaign ID is marked `bad_citation` by the code check and fails the row, even when the card was retrieved. The hand label judges content and may still pass the row.
A real retrieved card cited for the wrong model year or wrong vehicle is noted, not failed, unless the question asked for the full list, in which case extras count against it.
Count questions pass on the number alone. A right number from a wrong set is a pass with a note.
A decline when the gold recall was not retrieved is still a fail; the note records that the cause is retrieval, not the answer.
Notes are kept terse and carry the campaign IDs involved, so they can be grouped in failure analysis.

Labels: 100 rows, 74 pass, 26 fail. By stratum: unanswerable 20 (1 fail), handwritten 30 (7 fail), generated 50 (18 fail).

## Known hard questions

**gen_remedy_INTERNATIONAL_HV_2018.** "What's the fix for the fuel system recalls on the 2018 International HV?" The International HV 2018 has five campaigns in recall_vehicles: 18V644000 (EXTERIOR LIGHTING:BRAKE LIGHTS), 19V216000 (FUEL SYSTEM, DIESEL: PRE-HEATER), 20V219000 (FUEL SYSTEM, DIESEL: PRE-HEATER), 22V691000 (ENGINE), and 23V119000 (ELECTRICAL SYSTEM:BODY CONTROL MODULE/BCM). The two gold campaigns are fleet-wide filings: 19V216000 covers 23 make/model/year rows and 20V219000 covers 25 rows, spanning IC Bus (1300 FBC, CE, CESB, HC, TC) and International (Durastar, HV, MV, Workstar) for 2018 to 2021. In the 19 Oct 2026 run neither gold was retrieved; 22V691000 was. The row is kept as a hard retrieval case: a single-vehicle question whose gold recalls are filed against two dozen vehicles.

## Known limits of the generator

NHTSA component tags are assigned at the filing level and sometimes cover the administrative category rather than the physical part. Three generated rows expose this:

- **gen_existence_FOREST_RIVER_XLR_TOYHAULER_2021.** Question asks for "wheel recalls". Gold campaign 20V767000, component EQUIPMENT:OTHER:LABELS: "The Federal Placard and Cargo Carrying Capacity (CCC) label may state a lower weight than the actual weight."
- **gen_remedy_LEXUS_RC_F_2021.** Question asks for "tire recall". Gold campaign 22V615000, component EQUIPMENT:OTHER:LABELS: "The tire information label does not list the vehicle's weight capacity."
- **gen_remedy_ROAD_RESCUE_TYPE_I_2018.** Question asks for "suspension recall". Gold campaign 20V284000, component SERVICE BRAKES: "REV Ambulance Group Orlando, Inc. (REV) is recalling certain 2018-2020 Type 1 ambulances built on Ram 4500 and 5500 chassis equipped with Compressible Liquid Adaptive Suspension Systems (C.L.A.S.S.)... The rear brake caliper flexible line may have been routed so that it contacts a suspension component, possibly causing excessive wear and a loss of rear brakes."

In all three cases the model declined (correctly, from its perspective: no tire/wheel/suspension defect was described). The code marks them as `wrongly_declined` because the gold ID was retrieved. These are tagged 2 and 10 in the failure worksheet and are not fixed here.

A related limit: NHTSA lists some manufacturers under more than one make spelling. Mercedes-Benz appears as both `MERCEDES BENZ` (no hyphen) and `MERCEDES-BENZ` (with hyphen). The generator treats each spelling as a separate make, so gold campaigns for `gen_all_recalls_MERCEDES_BENZ_C_300_4MATIC_2018` are drawn only from recall_vehicles rows where `make = 'MERCEDES BENZ'` and `model = 'C 300 4MATIC'`. Campaign 18V725000 (starter battery short-circuit) is filed under `make = 'MERCEDES-BENZ'`, `model = 'C 300'`, a different triplet, so it is not gold for the 4MATIC row. The answer that cites it is not wrong about NHTSA's data; it is wrong about which vehicle the question asked about.