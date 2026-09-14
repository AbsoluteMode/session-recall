# Open questions

- Gateway deployments must supply their own endpoint, credentials, dimensions,
  context limit, and embedding revision. These are not repository defaults.
- The gateway registry does not provide a required immutable embedding revision;
  operators must update the configured revision when the backend changes.
