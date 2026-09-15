# Request Profile v0.1.2 pipeline scaffold

The initial Request pipeline reuses the Existing/Common IR infrastructure but
does not call an LLM yet.

```text
Common IR v1
  -> Request CandidatePack
  -> untrusted RequestSourceSelectionV012 (future LLM boundary)
  -> server exact-span / Common IR evidence materialization
  -> pre_review_request_profile/v0.1
```

## Implemented boundary

- CandidatePack is built from non-table Common IR v1 blocks and explicit
  table-cell paragraphs. Flattened whole-table text is never a value
  candidate. CandidatePack IDs are exact-span locators; original Common IR
  block/cell/occurrence provenance is preserved separately.
- `RequestSourceSelectionV012` accepts only the 16 shared comparison fields,
  six Request context fields, request-type anchors, hierarchy nodes, support
  components, and Request-only delivery relations/methods.
- Final `value_raw` is recovered only by the server from one unique,
  contiguous CandidatePack source span.
- CandidatePack additionally exposes deterministic, pack-local
  `value_span_candidate_id` entries for literals repeated inside the same
  source block and for every eligible `program_period` date range. A model may select that ID instead of a legacy
  `source_block_id`/`anchor_text` pair; it never supplies offsets or an
  occurrence number. With a candidate ID it may include `source_block_id` only
  as an optional server-verified hint, never `anchor_text`. Legacy anchors remain allowed only when unique. Candidate
  IDs are derived from CandidatePack generator/version and are not Common IR
  nodes; server materialization still writes normal exact `value_source` and
  Common IR provenance. This applies uniformly to every anchor-bearing
  selection: Raw Facts, component name/applies-to, hierarchy names, and
  delivery actor/role/action/method members.
- `program_period` is stricter than other Raw Facts: it must select a
  `program_period_date_range` candidate ID, never legacy `anchor_text`. A
  closed date range and an explicit official `공고일~YYYY.MM.DD` range are
  allowed; an inferred or otherwise missing endpoint is not. The candidate is
  only the literal interval, excluding field labels, parenthetical duration,
  and before/after narration.
- Request type is not LLM selection output. The server resolves it from the
  CandidatePack checkbox option container only when exactly one checked glyph
  has one adjacent canonical label; zero, multiple, or mismatched labels fail
  closed. The resolved value is read-only context to the model. Its
  glyph-adjacent resolver does not ask an LLM for an occurrence index when
  `내역사업 신설` also appears inside `내내역사업 신설`.
- Delivery relations preserve provenance per actor/role/action and support
  a paragraph, one explicit table row, or a verified nested-table column pair.
- Output field states use typed fact/relation/component references. No
  selection run is represented as an all-`not_found` profile.

## Request change-narrative and delivery decisions

- Raw Facts describe the requested programme's applied final state. A
  before/after narration (`→`, `변경 전/후`, `기존 대비`, `시범사업 대비`) is not
  stored as `purpose_goal` or `support_content`.
- A final amount, period, or count remains extractable when independently
  written with an adjacent `변경 없음` note. `동일(50개사)` and
  `변경없음(2027.1.~12.)` are therefore values, not bare references. A bare
  `동일(변경 없음)` reference
  does not create a copied Raw Fact; source selection can instead emit
  `field_states[].status = mentioned_unresolved` with
  `unchanged_by_reference` as a diagnostic reason code.
- `purpose_goal` is only the requested policy-purpose expression. A sentence
  narrating a retained prior purpose, before/after state, or maintain-and-
  expand change is not a purpose Fact. Likewise, Raw Facts must exclude source
  labels such as `2027년 요청안(변경 후):`; only the content span after that
  label is eligible.
- When a purpose sentence also describes the intervention (for example,
  staged payment or a mentoring-method change), that mechanism is not a
  `purpose_goal`. If the policy outcome is a separate contiguous source span,
  select that span only; never trim, summarize, or combine spans. If no exact
  outcome span can be selected, omit `purpose_goal` rather than storing the
  intervention prose.
- When an explicit support-component heading has a local `실제 수혜자:` row,
  the selector must materialize the recipient value and link it to that
  selected component through `primary_component_id`. The server validates this
  explicit local geometry but never auto-creates a component or beneficiary.
- The same guard applies to delivery-method text and component names/applies-to
  text: a change-diff sentence is not persisted as a semantic value.
- `support_content` is a limited escape hatch, not a duplicate bucket. Use it
  only when a material support-content span cannot safely be classified as a
  support activity, method, or item. The same exact span must never be stored
  in both `support_content` and one of those three fields.
- A named support component is a package/menu identifier, not a substitute for
  a concrete `support_items` Fact. When a separately occurring body span names
  what the recipient receives, retain the heading as the component and select
  that body span as the item. Provider-side payment/disbursement is not itself
  a `support_activities` Fact.
- A separable one-to-one mentoring format is a `support_methods` Fact. Its
  frequency/total-session detail and payment-stage composition are limited
  `support_content` when they cannot safely be classified as an activity,
  method, or item.
- A named recipient service such as `멘토링` is a `support_methods` Fact, not
  a `support_items` Fact. A `1:1 방식` span may be stored separately as its
  delivery format.
- When a named service is explicitly delivered alongside another package and
  has its own format or cadence, it may be a separate `support_package`.
  Link its service/method/frequency Facts to that component rather than to an
  unrelated monetary package. A component name may use the same exact span as
  one Raw Fact only when that Fact has the same `primary_component_id`; this
  structural-label exception does not permit Raw Fact↔Raw Fact duplication.
- A payment tranche (`1단계`, `2단계`, `1차` 등) is not a `stage_support`
  component merely because it has a separate amount. It becomes an independent
  component only when the source explicitly gives that stage its own recipient,
  eligibility, exclusion, or participation boundary. Otherwise its stage text
  remains `support_content` and its amount remains scoped to the parent package.
- A selection or review priority is not applicant eligibility, target scope,
  a positive eligibility condition, or an exclusion. If an applicant remains
  eligible but is ranked later at screening, the Request shared comparison
  fields omit that statement rather than presenting it as an application ban.
- `support_methods` stores only an independently selectable support-provision
  or payment method. A mixed selection→placement→attendance→payment workflow
  is not a support-method Raw Fact; only a separable exact span such as
  `사후 정산 지급` may be selected. The other workflow steps remain only in
  Common IR at this stage.
- `program_period` is one exact date-range span only. Labels, parenthetical
  duration (for example `(12개월)`), and change narration are context rather
  than part of its `value_raw`.
- `delivery_relations.relation_container` supports `paragraph`, `table_row`,
  and `table_column_pair`. The last is restricted to an explicit Common IR
  table where an institution/organisation actor and an explicit role **or
  action** cell have the same column span and the latter is in the next
  non-empty semantic row. It does not infer heading adjacency, treat a layout
  label as an actor, or combine values from different regions. Server geometry
  validation is internal; the final container persists Common IR document/table
  lineage and actor/role-or-action cell IDs, not duplicated row/column values.
- `field_states` is optional source-selection output only for
  `mentioned_unresolved`, `extraction_failed`, `not_applicable`, or `partial`.
  Selection-time `partial.fact_ids` is optional and advisory. The server
  derives final fact/relation/component IDs only from values it actually
  materializes. If selection also supplies a contradictory non-value state,
  server values win and finalizes the field as `partial`, retaining the source
  state/reasons as diagnostics; other server-derived output values remain
  forbidden to the model.

## Runner and source-selection boundary

```bash
env UV_CACHE_DIR=/tmp/hwp-parsing-uv-cache uv run python -m \
  semantic_structuring.run_request_profile_v012 \
  --common-ir exploratory_study/results/request_profile_test_corpus_common_ir_v1_20260831/common_ir/PREREVIEW-TEST-2027-01.common_ir_v1.json \
  --profile-id request:PREREVIEW-TEST-2027-01 \
  --candidate-pack-output /tmp/request-candidate-pack.json \
  --output /tmp/request-selection-template.json
```

Without a mode flag, the runner writes only a selection template; it does not
call a remote model and does not create a profile. `--dry-run` writes the exact
prompt plus no-Gold CandidatePack payload for inspection:

```bash
env UV_CACHE_DIR=/tmp/hwp-parsing-uv-cache uv run python -m \
  semantic_structuring.run_request_profile_v012 \
  --common-ir <request-common-ir.json> \
  --profile-id request:PREREVIEW-TEST-2027-01 \
  --dry-run --output /tmp/request-selection-dry-run.json
```

After reviewing that artifact, the explicitly opt-in remote path is:

```bash
env UV_CACHE_DIR=/tmp/hwp-parsing-uv-cache uv run python -m \
  semantic_structuring.run_request_profile_v012 \
  --common-ir <request-common-ir.json> \
  --profile-id request:PREREVIEW-TEST-2027-01 \
  --remote \
  --max-repairs 1 \
  --selection-artifact-output /tmp/request.selection.json \
  --output /tmp/request.profile.json
```

`--remote` reads `OPENAI_API_KEY` from `.env` but never writes it. It writes a
parsed source-selection artifact (not raw model text), then materializes and
validates the final profile. Failed remote calls create a secret-free
`*.failure.json`; accepted values still require server exact-span/evidence
validation. A precomputed `RequestSourceSelectionV012` JSON or selection
artifact may be passed with `--selection` to materialize without an API call.

Every `--remote` invocation also creates `<output>.run-status.json`, including
the start/completion time, selected model ID, profile/CandidatePack/Common IR
identifiers, safe API response status/ID when available, parse/materialization
and repair stages, ordinary exception class/message, and the path plus write
status of profile/selection/failure artifacts. It contains neither the API key
nor raw model output. The runner writes an initial `running` status before its
API request and a final `completed` or `failed` status, so a missing final
profile can be distinguished from an unfinished call, a server validation
failure, or an artifact write failure. `KeyboardInterrupt` and `SystemExit`
are intentionally not intercepted.

If a parsed remote selection exhausts server-guided materialization repair, the
runner writes the ordinary failure profile artifact plus a separate secret-free
`*.selection.failure.json` containing the parsed selection, CandidatePack
lineage, and validation diagnostics—never raw model response text.

If a parsed remote selection fails server materialization (for example it
omits literal Markdown `**` syntax from an anchor), `--remote` performs at
most one server-guided repair by default. The repair receives the same no-Gold
CandidatePack, the parsed selection, and concise validation diagnostics; it
must return the full selection contract. Use `--max-repairs 0` to disable it.
The final selection artifact records aggregate token usage, retry count and
repair diagnostics, but never raw model response text.

## Intentional next work

- Perform an explicitly approved remote selection run and review its run-status
  artifact alongside the profile/selection artifacts.
- Add semantic regression Gold for the five Request fixtures.
- Add derived `target_constraints`, `support_facets`, and
  `support_scale_measures` producers only after valid Raw Fact selection.
