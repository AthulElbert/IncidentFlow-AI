# May this person act on this service's change? (AUTHZ_MODE=scoped.) Roles are checked before this is asked; this
# adds ownership: the person must be in the team that owns the service, or in the admin team. A service with no owner
# is open to the admin team only. Same rules and reason texts as app/policy/engine.py (PythonPolicy).
package incidentflow.authz

import rego.v1

default allow := false

allow if count(reasons) == 0

reasons := [] if {
	input.admin_team != ""
	input.admin_team in input.principal.teams
} else := [] if {
	input.owner_team != null
	input.owner_team in input.principal.teams
} else := [sprintf("%s is not in %s, the team that owns %s", [input.principal.actor, input.owner_team, input.service])] if {
	input.owner_team != null
} else := [sprintf("%s has no owning team; only the admin team may act on it", [input.service])]
