# Security Policy

## Reporting a vulnerability

Do not open a public GitHub issue for security vulnerabilities.

**Use GitHub's private security advisory feature:**
[Report a vulnerability](https://github.com/tya5/reyn/security/advisories/new).
This routes the report directly and privately to the maintainer.

Reyn is currently maintained by a single author on a best-effort basis. There
is no formal SLA — acknowledgement and initial assessment are typically
delivered within a few business days.

## Disclosure timeline

Reyn aims for a 90-day coordinated disclosure window from the date of initial
report acknowledgement. If a fix requires longer, the revised timeline is
communicated to the reporter before the window closes. Public disclosure is
coordinated with the reporter.

## Supported versions

Reyn is pre-1.0. Only the current `main` branch receives security fixes.
Older commits or release tags are not supported. Upgrade to the latest commit
on `main` before reporting a vulnerability.

## Scope

In scope:
- Vulnerabilities in Reyn's OS layer — the validation gate, permission
  enforcement, Control IR execution, event log integrity, and persistence
  (snapshot / WAL) mechanisms.
- Sandbox / privilege escalation issues in stdlib skill execution paths.

Out of scope:
- Issues caused by user-supplied skills or `reyn.yaml` configurations that
  intentionally relax Reyn's permission model. The OS gate enforces the
  declared permission policy; choosing a permissive policy is a user decision.
- Prompt-injection content delivered through user-provided LLM inputs that
  the user's own skill graph chooses to act on. Reyn enforces structural
  validation; semantic content judgement is the skill author's responsibility.
- Vulnerabilities in third-party LLM providers, LiteLLM, or MCP servers.
  Report those upstream.

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
