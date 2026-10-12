# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.5.x | yes |
| 0.4.x and earlier (`cu-detector-research-artifact`) | no; upgrade to 0.5.x |

ComputerUseAgent is a research toolkit in alpha, not a production security
control. Fixes are released as 0.5.x patch versions of the Python package and
the extension together.

## Reporting a vulnerability

**Do not open a public issue** for a vulnerability or a privacy leak, and never
attach raw traces, typed text, credentials or participant information to any
report.

Use GitHub private vulnerability reporting:
<https://github.com/appleweiping/ComputerUseAgent/security/advisories/new>
(repository **Security** tab, then **Report a vulnerability**).
If the **Report a vulnerability** button is unavailable, open a public issue
that says only that you need a private contact, with no details.

Please include:

- the affected component and version (`computer-use-agent --version`, the
  extension version on `chrome://extensions`, or the commit SHA);
- a minimal reproduction using synthetic data;
- the impact, and whether recorded or sensitive data may already have been
  exposed.

The maintainer aims to acknowledge a report within 7 days and to agree on a
disclosure timeline with the reporter. This is a best-effort commitment from a
small project, not a contractual response time. Reporters are credited in the
advisory unless they ask not to be.

## Scope

In scope:

- **Recorder privacy leaks**: the extension storing or exporting anything its
  documentation says it drops (typed characters, password or
  `[data-recorder-private]` key detail, URL queries or fragments, clipboard or
  drag contents, page text, form values).
- **Extension message API**: a web page or another extension being able to
  start, stop, read, list or delete sessions, or to corrupt stored traces.
- **Viewer exposure**: the trace viewer serving files outside the traces it was
  given, binding to a non-loopback address, or executing content from a trace.
- **Trace and dataset handling**: path traversal, unsafe deserialization or
  schema bypasses in the Python package and CLI, and private data being written
  outside `CUA_PRIVATE_ROOT`.
- **Supply chain**: unpinned or tampered dependencies or GitHub Actions in this
  repository.

Out of scope: vulnerabilities in Chrome itself or in third-party dependencies
(report them upstream; tell us if this project needs to react), the archived
research harnesses under `research/` (frozen provenance, not executed by
users), and findings that require a malicious local administrator.

## Handling recorded data

- Traces are stored only in the browser's local extension storage until the
  user exports them. Keep exports of human sessions under `CUA_PRIVATE_ROOT`,
  outside the repository; `.gitignore` blocks the common locations but is not a
  control.
- Recording another person requires that person's informed consent and any
  approval your institution requires; record the determination with
  `CUA_HUMAN_RESEARCH_DETERMINATION_RECORD`.
- A privacy failure invalidates the affected session. Delete the session; do
  not try to repair raw evidence in place.
