# Security Policy

## Reporting a vulnerability

Do not open a public GitHub issue for security vulnerabilities.

**Preferred channel:** Email `security@reyn-project.example` with a clear
description of the issue, reproduction steps, and the potential impact.

**Alternative:** Use
[GitHub's private security advisory feature](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
for this repository.

You will receive an acknowledgement within 72 hours. We aim to provide an
initial assessment within 7 days.

## Disclosure timeline

Reyn follows a 90-day coordinated disclosure window from the date of initial
report acknowledgement. If a fix requires more than 90 days, we will communicate
the revised timeline before the window closes. Public disclosure is coordinated
with the reporter.

## Supported versions

Reyn is pre-1.0. Only the current `main` branch receives security fixes.
Older commits or release tags are not supported. Upgrade to the latest commit
on `main` before reporting a vulnerability.

## LLM-specific security design

Reyn is designed around the assumption that LLM output is untrusted until it
has passed through the OS validation and permission gate. Every LLM response
is subject to schema validation (Transition and Finish contracts), graph
membership checks (the Skill graph constrains which phase transitions are
legal), and Control IR permission enforcement before any workspace side-effect
is applied. This gate is the mechanism by which prompt injection, goal
hijacking, and capability escalation attacks are contained. Bypassing or
weakening the Control IR permission layer is considered a critical
vulnerability. Contributions that relax this gate require explicit security
review before merge.
