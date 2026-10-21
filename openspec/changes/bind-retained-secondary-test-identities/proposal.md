# Bind retained secondary test identities

Original suites may need both root and a named unprivileged account. Private
retained workspaces correctly deny that account traversal today. Selecting a
username or relaxing directory modes is insufficient. Issue #85 requires an
exact numeric identity, a live disposable-worker allocation, reviewed resource
scope, current permission custody, cleanup and lifecycle evidence.

Implement this through the retained workspace and provisioning owners. Preserve
legacy profile identities and private defaults when the feature is absent.
Neither an account observation nor configuration claiming a disposable worker
grants authority. Complete original cases and independent acceptance remain
required before receipt or package admission.
