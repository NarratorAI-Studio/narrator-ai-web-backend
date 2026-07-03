# Security Policy

## Reporting a Vulnerability

If you believe you've found a security vulnerability in NarratorAI Web Backend, please report it privately so we can investigate and fix it before public disclosure.

**How to report**: email security@gridltd.com.

Please include, where possible:

- A clear description of the vulnerability
- Steps to reproduce (a minimal proof-of-concept is ideal)
- The version / branch / commit you tested against
- Impact assessment (what an attacker could do)
- Any suggested remediation

Please **do not** open public GitHub issues for security reports.

## What to Expect

| Stage | Timeline |
|---|---|
| Acknowledgement of your report | **within 3 business days** |
| Triage and severity assessment | within 7 business days of acknowledgement |
| Public disclosure | coordinated — after a fix is released, or **90 days** from initial report, whichever comes first |

We will keep you informed of our investigation progress and the planned remediation timeline.

## Scope

This policy covers:

- This repository (`narrator-ai-web-backend`) — Flask application code, API routes, database migrations, orchestrator logic, configuration, dependency vulnerabilities affecting our deployment
- The deployment surface served from this codebase (the backend API, its pricing/wallet endpoints)

This policy does **not** cover:

- **Your configured upstream commentary API** — report directly to that provider
- **Third-party services** you connect to this deployment — report to the respective vendor
- **Social engineering**, physical attacks, or attacks requiring already-compromised credentials
- **Denial-of-service** attacks that require traffic generation (we already have rate-limiting; volumetric DoS is out of scope)
- **Outdated dependencies without a known exploitable path** — these are tracked separately via `dependabot`

## Bug Bounty

NarratorAI Web does **not** currently offer a paid bug bounty. We do appreciate responsible disclosure and will publicly credit reporters (with your permission) in the relevant release notes or a `SECURITY-ACKNOWLEDGEMENTS.md` file.

## Safe Harbor

We will not pursue legal action against good-faith security researchers who:

- Make a sincere effort to avoid privacy violations, data destruction, and service interruption
- Report the vulnerability privately at security@gridltd.com before public disclosure
- Give us reasonable time to investigate and remediate before going public
- Do not exploit the vulnerability beyond what's necessary to demonstrate it
