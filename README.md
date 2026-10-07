# MCP Mail Assistant

A security-conscious AI email assistant built with **Python**, **Model Context Protocol (MCP)**,
and an LLM operator.

This repository is a **sanitized portfolio edition** of a personal project. It demonstrates how I
designed an event-driven assistant that can collect email, expose structured tools through MCP,
summarize and classify messages with an LLM, and place explicit approval boundaries around
high-risk actions.

> No real mailbox data, company credentials, internal URLs, production databases, or secrets are
> included in this repository.

## Why I built it

Email work is repetitive but risky to automate blindly. Reading and summarizing can be automated
aggressively, while sending or forwarding messages should require stronger controls.

The project explores that boundary:

- automate **collection, parsing, summarization, prioritization, and task extraction**
- expose deterministic operations as **MCP tools**
- treat email bodies and attachments as **untrusted input**
- separate LLM reasoning from side-effecting tools
- require explicit approval for sensitive actions
- persist state so crashes and retries do not silently duplicate work

## Architecture

```text
POP3 / IMAP
     |
     v
+-------------+
|   Daemon    |  collect / normalize / persist
+-------------+
     |
     v
+-------------+
| Event Queue |  SQLite-backed state
+-------------+
     |
     v
+---------------+        +----------------+
| LLM Operator  | <----> |   MCP Server   |
+---------------+        +----------------+
     |                          |
     |                          +--> email tools
     |                          +--> attachment tools
     |                          +--> event / task tools
     |
     +--> summary / priority / action items
     +--> approval boundary for side effects
```

The implementation follows a three-process design:

1. **Daemon** — receives and normalizes mail, stores data, and creates events.
2. **MCP Server** — exposes deterministic email / attachment / task operations as tools.
3. **Mail Operator** — consumes events and uses an LLM for analysis and decisions.

## Portfolio highlights

### MCP tool boundary

The LLM does not receive arbitrary access to the host system. Operations are exposed as explicit
MCP tools with structured inputs and outputs.

Representative implementation:

- `mcp_server/server.py`
- `mcp_server/mcp_tools/email_tools.py`
- `mcp_server/mcp_tools/email_db_tools.py`
- `mcp_server/mcp_tools/attachment_tools.py`
- `mcp_server/mcp_tools/event_tools.py`

### Event-driven processing

Mail collection and LLM analysis are decoupled instead of being executed in one long request.

This makes it easier to reason about:

- retries
- deduplication
- crash recovery
- event ownership
- asynchronous processing

Relevant modules:

- `daemon/`
- `watcher/`
- `db/event_store.py`
- `mail_operator/main.py`

### Untrusted-content handling

Email bodies and extracted attachment text are treated as data rather than trusted instructions.
The operator layer uses structured output contracts and separates user-authorized instructions from
untrusted message content.

Relevant modules:

- `mail_operator/prompts.py`
- `mail_operator/safety.py`
- `mail_operator/schemas.py`

### Human-in-the-loop safety

Read-only operations and summaries are intentionally easier to automate than outbound actions.
The design includes approval concepts for operations such as replying to email, and state is
persisted so the approval boundary can be revalidated before execution.

### Attachment parsing

The assistant can extract text from common business document formats:

- PDF
- DOCX
- XLSX
- PPTX

Parsers live under `parsers/`.

## Tech stack

- **Python 3.11+**
- **MCP (Model Context Protocol)**
- **Pydantic**
- **SQLite**
- **POP3 / IMAP / SMTP**
- **Slack SDK / Bolt**
- **pytest**
- **GitHub Actions**

## Repository layout

```text
core/             mail adapters, models, configuration
daemon/           background collection process
db/               persistence and migrations
mail_operator/    LLM-facing operator layer
mcp_server/       MCP server and tools
parsers/          document parsers
watcher/          email watching / deduplication pipeline
tests/            selected sanitized unit tests
.github/          portfolio CI
```

## Local setup

Create a virtual environment and install development dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Copy the example configuration:

```bash
cp .env.example .env
```

Then replace all placeholder values with credentials from your **own test accounts**.

### Run the portfolio test suite

```bash
python -m pytest tests/test_config.py tests/test_smtp_adapter.py tests/test_pop3_adapter.py
```

GitHub Actions runs the same selected tests and a syntax check on every push.

## Security / privacy

The public portfolio version is deliberately separated from the original development repository.

- it has a **fresh Git history**
- runtime databases are excluded
- attachments and logs are excluded
- secrets and `.env` files are excluded
- configuration examples use placeholder domains and IDs
- third-party reference source trees are not vendored

See [SECURITY.md](SECURITY.md) for additional notes.

## AI-assisted development

I used generative-AI coding tools during implementation and review.

AI was used as a development accelerator for tasks such as drafting code, reviewing changes,
exploring alternatives, and expanding test coverage. I retained responsibility for requirements,
architecture decisions, security boundaries, integration, validation, and deciding which generated
changes were accepted.

I intentionally include this because effective use of AI coding tools is part of what this project
was built to explore.

## Architecture inspiration

The three-process architecture was inspired in part by
[KIRA by KRAFTON AI](https://github.com/krafton-ai/kira).

The KIRA source tree is **not included** in this repository. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT
