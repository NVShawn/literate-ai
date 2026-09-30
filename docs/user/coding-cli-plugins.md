# Coding CLI release plugins

Literate AI 1.1 packages the canonical onboarding and agent skills for Codex and
Claude. The plugin is a distribution of framework instructions; the matching
Literate AI wheel supplies the commands. Cursor and OpenCode generation adapters
remain available, but their plugin distribution formats are not yet qualified.

Release checking runs `make release-artifacts`. It builds the wheel once, runs
the installed-wheel smoke against those bytes, then generates deterministic
`literate_ai-VERSION-codex-plugin.zip` and
`literate_ai-VERSION-claude-plugin.zip` archives. Each contains a provider manifest,
a discoverable `skills/literate-ai/SKILL.md`, canonical supporting instructions,
and `bundle.json` with source revision and catalog content identities. The release
file manifest binds the exact wheel and archive digests into prepared evidence.
Publication uploads those retained files and verifies downloaded provider bytes.

Extract the selected archive into a dedicated directory; its root is `literate-ai`.
Install the matching wheel before using its commands. For a temporary Claude
session, `claude --plugin-dir /absolute/path/literate-ai` loads that directory.
Codex uses `.codex-plugin/plugin.json`; register the extracted directory through
your explicit local/team marketplace workflow. Consult the installed client's
plugin help for installation scope. See the
[Claude plugin reference](https://code.claude.com/docs/en/plugins-reference).

The archives contain no automatic hooks, credentials, MCP registration, or user
configuration writes. They do not install a marketplace or change permissions.
Upgrade by selecting the same release version of wheel and plugin and starting a
fresh coding session. Remove the plugin through the client's plugin manager and
remove only its extracted directory; keep the project and its authored files.

Packaging validation proves manifest shape, inventory and byte identity. Fresh
provider-session discovery and a generated Component remain distinct release
acceptance checks; packaging alone does not prove model behavior.
