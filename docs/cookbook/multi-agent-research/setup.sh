#!/usr/bin/env bash
# Stand up the lead/researcher/writer team for the multi-agent-research recipe.
set -euo pipefail

reyn agent new lead       --role "team lead. Triage requests; delegate research to researcher and drafting to writer; synthesize a final answer."
reyn agent new researcher --role "deep technical research. Cite primary sources. No prose polish."
reyn agent new writer     --role "concise prose. Strict word budgets. No headings unless asked."

reyn topology new launch --kind team \
    --leader lead \
    --members lead,researcher,writer

reyn topology show launch
