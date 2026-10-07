# Security Notes

This repository is a sanitized portfolio edition.

- No real mailbox database, message body, attachment, token, password, or API key is included.
- Runtime SQLite databases, logs, attachments, and `.env` files are ignored by Git.
- Example configuration uses placeholder domains and credentials only.
- Outbound actions are designed around explicit approval boundaries.
- Email and attachment content is treated as untrusted input.
- The LLM layer is expected to return structured output that is validated before use.

If you reuse this project, keep credentials in local environment variables or a
dedicated secret manager and review mail-provider and Slack permissions before
connecting real accounts.
