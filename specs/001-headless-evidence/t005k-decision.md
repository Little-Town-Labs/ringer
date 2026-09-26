# T005k: compatibility decision required

T005j accepted. Architect found routine extraction cannot preserve direct RingerRunner constructor interface while leaving credential-handling EvalLogger in CLI and prohibiting reverse imports/wrappers.

## Recommended bounded change (not yet approved)
Core RingerRunner gains required keyword-only logger parameter with private two-method structural protocol (log_attempt, close). Keep all positional parameters and class reexport identity. Constructor assigns supplied logger instead of constructing EvalLogger. CLI run_manifest creates existing EvalLogger and passes it, with explicit close on runner-construction failure before ownership transfer. Runner retains existing finalization close. EvalLogger and parse_env_file remain unchanged.

Consequences: direct Python RingerRunner callers must pass logger; CLI flags/commands do not change. Eleven test call sites need temporary JSONL EvalLogger. Backend/credential initialization moves earlier than runtime/state/dashboard initialization on constructor failure, which is a behavior change and sensitive composition seam. Lead owns that integration only after explicit user approval. No actual credential access or live configuration change is authorized.

## Policy boundary
This is a constructor-interface and initialization-order change, not a pure move. Original R5 preserves behavior/import contracts. User decision required before dependent implementation. Do not silently authorize through broad refactoring scope. Alternative: retain runner in CLI for now (defer extraction); preserving default constructor without reverse dependency requires a separately designed compatibility strategy or relocating existing logging code, neither selected.

## Future closure if approved
Steering: SteeringRule, SteeringProfile, _steering_yaml_values, parse_steering_profile, load_steering_profile, steering_profile_candidates, resolve_steering_profile, steering_worker_rules, inject_steering_spec; constants STEERING_STATUSES/STEERING_AUDIENCES/STEERING_RULE_HEADING_RE into steering.py.
Runner: RingerRunner, print_summary, DELIVERABLE_MAX_BYTES, FALLBACK_HARVEST_SUFFIXES, FALLBACK_HARVEST_MAX_FILES, SHEPHERD_MODEL, VERIFY_METHOD into runner.py plus approved logger seam. No separate workers framework. Keep diagnostic strings unchanged.
Tests requiring direct logger: steering five sites, taxonomy one, model_log one, deliverables two, identity_evidence one, model_field one. Steering patches move build_worker_command and resolve_steering_profile to runner consumer; instance observation patch stays. Full AST comparisons except approved seam, logger delivery/close/error ownership tests and normal regression suite required.

Status: blocked on owner compatibility/initialization-order approval. No T005k worker dispatched. All accepted work remains uncommitted in isolated worktree; T006 final docs/verification outstanding.
