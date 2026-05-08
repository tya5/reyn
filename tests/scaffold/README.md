# Scaffolding tests (bounded-life)

This directory holds tests with explicit retirement triggers per the
testing policy (docs/deep-dives/contributing/testing.md, Annex). Currently empty:
no scaffolding tests are needed.

# To add one:
# - Place a test file here
# - Include `# scaffold: triggered_by="..."` and `# scaffold: removed_by="..."`
#   metadata at the top
# - The PR that fires the trigger event must remove the file in the same PR
