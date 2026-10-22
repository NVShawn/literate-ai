# Connect public retained execution to live SDK producers

The public original-source runtime does not acquire selected native SDK producers,
so it correctly refuses SDK-backed execution. Its legacy SDK placement policy names
one producer run; fresh authorizations change that run identity even when the exact
locked SDK snapshot is unchanged. Reusing a serialized producer record would not
establish live admission.

Add an explicit selection policy binding each exact Component lock, recipe selection,
SDK snapshot and input-tree prefix. Keep the legacy policy wire identity and exact
run-bound behavior unchanged. After the complete retained plan and host execution
are authorized, acquire real SDKs through existing source-build owners and retain
those owners, fresh command grants and evidence through receipt admission.

Read-only planning never acquires or compiles SDKs. Missing or changed selections,
bytes, authority or current grants refuse. This does not add remote execution,
regenerative source authority, package qualification or release authorization.
