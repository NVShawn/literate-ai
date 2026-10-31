# Welcome aboard, Unprompted AI

Welcome to Jordan Hubbard’s Literate AI session at Unprompted AI! You can start
with a browser, explore a lab, and then use your coding assistant to change a
specification and check the resulting software.

Literate AI is the harness around that work. **Components** describe responsibilities
and acceptance criteria. **Flavors** select language, operating system and build
choices. **Locks** record the exact inputs. The lifecycle builds, tests and verifies
those inputs, and **receipts** identify what actually passed. Your assistant writes
and revises code; Literate AI keeps the requirements and the evidence connected.

## What you need, starting from zero

- **A computer:** a Mac (Apple Silicon or Intel), a Linux laptop/desktop, or a
  Windows 11 machine. On Windows, the conference command examples are easiest in
  [WSL 2](https://learn.microsoft.com/windows/wsl/install) with a Linux shell.
  A normal computer is enough; the cloud models do not require a local NVIDIA GPU.
- **A browser and terminal:** a current Chrome, Edge, Firefox or Safari for the
  visual labs, plus a terminal for the project exercises. Allow audio when an
  audio lab asks you to start it.
- **Python 3.11 or newer, Git and Git LFS:** these support Literate AI and its
  repository inputs. Install **Node.js 20 or newer** for the JavaScript lab work.
  Install **GNU Make** for the labs that select the Make Flavor. Use the
  [installation guide](../user/installation.md) for host-specific tools.
- **Your OpenRouter API key:** the session key is in your email. Have it ready
  before configuring the assistant. If you cannot find it, check your spam folder
  or ask the session team. Keep the key in your assistant’s credential store or
  your terminal environment; do not commit it with your lab.
- **One supported coding assistant:** choose OpenCode or Claude Code for the emailed
  OpenRouter key. Literate AI also supports Codex CLI and Cursor Agent, with the
  separate credential and clean-configuration limitations described below.
- **Literate AI 1.3.1 and this lab kit:** install the release wheel in a Python
  virtual environment, then obtain the [labs](https://github.com/jordanhubbard/literate-ai/blob/main/labs/README.md).
- **Internet access for setup and model calls:** finished browser previews work
  offline. Generation uses your OpenRouter key’s available models and credit limits.

**Windows release status:** the Windows test matrix is temporarily suspended for
**1.3.1 only because of resource availability**, with explicit release-owner approval.
It must be restored for the **next release**. Windows qualification is not claimed
for 1.3.1. Linux/macOS qualification and the other release gates remain required.
See the [recorded exception](../../verification/1.3.1-windows-qualification-exception.json).

## Install the release and get the labs

Use the exact wheel linked by the [1.3.1 release](https://github.com/jordanhubbard/literate-ai/releases/tag/v1.3.1).
The following commands use a shell on macOS, Linux, or WSL 2:

```sh
python3 -m venv .venv-litai
. .venv-litai/bin/activate
python -m pip install "literate-ai[browser] @ https://github.com/jordanhubbard/literate-ai/releases/download/v1.3.1/literate_ai-1.3.1-py3-none-any.whl"
python -m playwright install chromium
litai --version

git clone --depth 1 https://github.com/jordanhubbard/literate-ai.git
cd literate-ai
```

Native Windows PowerShell uses `py -3 -m venv .venv-litai` and
`.venv-litai\Scripts\Activate.ps1`; the wheel installation command is the same
inside the activated environment. Install Git/Node through the Windows instructions
in the installation guide. A terminal-local credential does not automatically reach
an assistant launched separately from the desktop.

## Choose your assistant

| Assistant used by Literate AI | Use the emailed OpenRouter key | Select it |
| --- | --- | --- |
| OpenCode CLI | Connect its built-in OpenRouter provider; select a tool-capable model | `CODING_CLI=opencode` |
| Claude Code CLI | Configure OpenRouter’s Anthropic-compatible endpoint and token | `CODING_CLI=claude` |
| Codex CLI | Use its separately configured OpenAI credentials for the 1.3.1 automated path; see the clean-configuration note below | `CODING_CLI=codex` |
| Cursor Agent CLI | Requires a Cursor login or `CURSOR_API_KEY`; not the emailed key | `CODING_CLI=cursor-agent` |

Install **one**, rather than all four. Each assistant has its own installation and
provider configuration. Successfully opening an assistant is separate from passing a
Literate AI build or acceptance check.

### OpenCode

Follow [OpenCode installation](https://opencode.ai/docs/) and
[OpenRouter’s OpenCode setup](https://openrouter.ai/docs/cookbook/coding-agents/opencode-integration).
Start `opencode`, run `/connect`, choose **OpenRouter**, and paste your emailed key
at the credential prompt. Use `/models` to choose a model enabled for that key.
Then, in the terminal where you will run Literate AI:

```sh
export CODING_CLI=opencode
opencode --pure run --help
```

Use `opencode models openrouter` to list fully qualified model IDs. Pass your
chosen ID to Literate AI with `--model`, for example:

```sh
litai rebuild COMPONENT_PATH --allow-host-execution --model "openrouter/EXACT_MODEL_ID_FROM_LIST"
```

Replace both placeholders using the lab README and your model list. The explicit
model matters because Literate AI uses a clean OpenCode configuration; an interactive
model choice alone is not a lifecycle binding.

Literate AI 1.3.1 requires the `--pure run` capability. If that probe is rejected,
update to a compatible OpenCode CLI or use another supported assistant; do not
assume every OpenCode desktop or older CLI version exposes the same command.

The development adapter also supports OpenCode v2 with `opencode run --help`
and private-server automation. That support is separate from the frozen 1.3.1
conference wheel above. For v2, install `@opencode/cli` following the
[v2 installation guide](https://opencode.ai/v2/docs/). List models with
`opencode models`, then supply the emailed key as `OPENROUTER_API_KEY` in the
Literate AI terminal. Isolated v2 uses fresh provider state and does not import
interactive OpenCode logins; keep the key out of project files.
Literate AI selects the interface automatically; existing v1 users can stay on v1.
See [coding-CLI configuration](../user/configuration-and-cli.md) for the exact
capability and isolation checks.

### Claude Code

Install [Claude Code](https://code.claude.com/docs/en/overview). In the terminal you
will use for both Claude and Literate AI, read the key without displaying it:

```sh
printf 'Paste your emailed OpenRouter key: '
read -r -s OPENROUTER_API_KEY
printf '\n'
export OPENROUTER_API_KEY
export ANTHROPIC_BASE_URL=https://openrouter.ai/api
export ANTHROPIC_AUTH_TOKEN="$OPENROUTER_API_KEY"
export ANTHROPIC_API_KEY=''
export CODING_CLI=claude
claude
```

Inside Claude, use `/status` to confirm the OpenRouter base URL and token method.
If you previously used an Anthropic account, follow the login-conflict steps in
[OpenRouter’s Claude Code guide](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration).
Choose a model supported by your key; Claude Code works best with Anthropic models.
Pass its OpenRouter model ID explicitly through `litai rebuild ... --model MODEL_ID`
for the lab lifecycle. This keeps the selected model visible in the plan.

### Codex CLI and Cursor Agent

Literate AI supports these adapters, but choose OpenCode or Claude Code if you
want the documented path using only the session’s emailed OpenRouter key.

[Codex CLI](https://developers.openai.com/codex/cli/) supports custom providers,
including [OpenRouter](https://openrouter.ai/docs/cookbook/coding-agents/codex-cli),
when you run Codex directly. Literate AI 1.3.1 invokes Codex with
`--ignore-user-config`; a provider block in your personal `config.toml` therefore
must not be assumed to reach the automated lifecycle. The Codex adapter also does
not forward `OPENROUTER_API_KEY`. Use the documented OpenAI credential flow for
this adapter, or use one of the OpenRouter paths above for the conference.
See [OpenAI’s provider configuration](https://developers.openai.com/codex/config-advanced/)
for direct Codex use.

[Cursor Agent authentication](https://docs.cursor.com/en/cli/reference/authentication)
uses a Cursor account or `CURSOR_API_KEY`. Cursor IDE custom-provider settings do
not establish that its CLI can use the emailed OpenRouter credential. Set
`CODING_CLI=cursor-agent` only after authenticating that CLI.

## Your first lab session

Start with [Grumpy Counter](https://github.com/jordanhubbard/literate-ai/blob/main/labs/00-grumpy-counter/README.md), then follow the
numbered lab sequence. Read the specification before changing behavior. Ask your
assistant to explain the Component graph and selected Flavors, then change one
requirement, regenerate or rebuild through the documented lifecycle, and inspect
both the running result and its evidence. A pretty page alone is not a passing
lifecycle receipt.

For a general first project outside the labs, use
[Getting started](../user/getting-started.md). The
[HTML repository explorer](../user/html-repository-explorer.md) shows the indexed
Components, relationships and available code-navigation evidence.
