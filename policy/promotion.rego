# May a change go to production? The same rules as app/policy/engine.py (PythonPolicy), with the same reason texts:
# tests/test_policy.py runs one table of cases against both. Thresholds come from the input (`params`), so both
# engines decide from the app's settings.
package incidentflow.promotion

import rego.v1

default allow := false

allow if count(reasons) == 0

reasons := array.concat(
	array.concat(array.concat(confidence, warnings), jenkins),
	array.concat(issue_type, dev_evidence),
)

confidence := [sprintf("confidence %.2f below minimum %.2f", [input.confidence, input.params.min_confidence])] if {
	input.confidence < input.params.min_confidence
} else := []

warnings := [sprintf("warning_count %d must be 0", [input.warning_count])] if {
	input.params.require_zero_warnings
	input.warning_count > 0
} else := []

allowed_states := sort({upper(state) | some state in input.params.allowed_jenkins_states})

jenkins := [sprintf("jenkins_status %s not in allowed set %s", [input.jenkins_status, concat(", ", allowed_states)])] if {
	not upper(input.jenkins_status) in allowed_states
} else := []

issue_type := ["issue_type unknown requires manual deep-dive"] if {
	input.issue_type == "unknown"
} else := []

dev_evidence := ["dev execution evidence is not in passed state"] if {
	input.dev_execution_status != "passed"
} else := []
