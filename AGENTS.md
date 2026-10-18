# Repository guidance

- Keep optional services disabled by default and preserve existing inference protocols.
- Use synthetic data only in source, tests, fixtures, documentation, commits and pull requests.
- Never commit personal/family details, real user prompts, private hostnames, network
  layouts, device identifiers, credentials, connection strings or deployment-specific paths.
- Read service addresses and secrets from environment variables or an external secret store.
  Use loopback addresses and reserved example domains/ranges for examples and tests.
- Before every commit/publication, inspect added lines and staged files for identifying
  content and secrets. Keep any private matching rules outside the repository.
- Do not test against personal infrastructure or import real memory/session content.
- Run the relevant self-tests, lint, config validation and applicable integration tests.
