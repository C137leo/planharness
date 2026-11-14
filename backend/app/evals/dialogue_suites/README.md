# Stateful Coach dialogue suites

These files are evaluator data, not production prompts. The actor receives each
turn's `text` plus its actual accumulated conversation and database state.
`expect`, tags, themes and future turns must never be sent to the actor/model.
There is one isolated database per conversation and no reset between its turns.

- `expanded_regression.json`: 15 independently authored conversations, seven turns
  each; 105 unique utterances. Includes changing topics, additions versus
  replacements, negation, advice without execution, cancellation, re-planning,
  ambiguous references, undoing only the latest change, and partial success when
  another requested object does not exist. It is a reusable regression corpus,
  not proof that these utterances have passed a real model.
- `holdout-final.json`: retired v2 set. Its **first** real-model result was
  14/15 turns and 2/3 conversations. That run exposed an arbitrary minimum food
  portion-size boundary, so the set informed development and is now regression
  data. Its original result remains recorded; later reruns must not be called
  unseen holdout results.
- `holdout-v3.json`: retired after an ingredient-identity oracle issue. Its
  unaltered first raw result was 11/15 turns and 2/3 conversations. Four failed
  turns shared one display-name versus canonical ingredient identity mismatch;
  this is not four independent semantic understanding failures. Any corrected
  identity recalculation is calibration data, not a new unseen first result.
- `holdout-v4.json`: completed its first real-model run at **15/15 turns,
  3/3 conversations, zero observed scope violations**, without being used to
  debug production. Subsequent independent adaptive exploration found reply
  generation/query issues and led to a new production code fingerprint. Keep
  v4's original result; do not re-label a v4 rerun as a new unseen score.
- `holdout-v5.json`: retired after its first real-model run returned **13/15
  turns, 2/3 conversations, two scope-violation turns**. One ambiguous reference
  to multiple modified objects was wrongly executed via model-supplied undo IDs;
  the next correction had a downstream trajectory/scope failure. The original
  score is retained. Advice/current-state oracle extensions themselves passed.
- `holdout-v6.json`: an old-fingerprint trial only. Before its results or
  utterances were reviewed, an existing local unit test independently found a
  guard regression: multiple changed resource references within one atomic
  regeneration/replacement task were confused with multiple candidate tasks.
  That independent production fix replaced the final acceptance fingerprint.
  Preserve raw v6 output separately; do not attribute the unit-test discovery to
  v6 or claim v6 as final acceptance of the newer code.
- `holdout-v7.json`: the newly sealed independent final set: three conversations,
  five turns each, with no exact utterance copied from an earlier corpus. It
  retains multiple-candidate references, whole-task restoration, topic switches,
  corrections, read-only advice/current-state queries and task-scoped cancellation.
  Advice is required only in the turn that actually asks for it. Its text and
  expected parameters were not disclosed to the production developer, and it was
  neither executed nor used for tuning before sealing. See its own manifest.
- `dev.json` and `holdout.json`: earlier, smaller suites; their provenance and
  execution status are separate from the new expanded and final sets.

The deterministic postconditions check persisted business state and the
allowlist of permitted paths; they do not impose an intent or tool name. Added
oracle kinds are `unchanged`/`changed` (compare the path against the same turn's
before state) and `food_grams` (sum the grams for the named food in that meal).
This prevents an added-food goal from passing when other food was replaced,
and prevents a regeneration goal from passing when no training data changed.

A passing conversation requires every turn to pass. Context-reference turns
also assert a concrete affected object, and scope checks protect all other
objects, dates and tables. Merely finding a previous task in context does not
prove correct reference resolution. A read-only turn returning any text is not
by itself proof of a correct explanation; audit explanation correctness still
requires the execution records and reply to be checked. Provider/network errors
must be reported as unavailable, never credited as successful no-op turns.

The corpus author did not call the real model or execute the newly sealed v7
set. Live-run results supplied by the parent agent are explicitly recorded for
retired v2/v3, without claiming they came from the corpus author's own execution.
The authored role-play in the last regression conversation is a test design
exercise, not by itself a live autonomous product exploration result.

The final frozen release also passed the 105-turn regression in 15 conversations. Raw model trajectories and unsuccessful earlier checkpoints are retained under `docs/evidence/coach-dialogues/`.

Final v7 first live result: 15/15 turns, 3/3 conversations, zero unauthorized mutation turns. Its content was not used for production development/debugging. The same-version adaptive exploration regression passed 8/8; it is explicitly regression, not blind accuracy.
