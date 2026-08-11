#!/bin/sh
# Serialized merge train. Runs in the BACKGROUND (nohup ... &) -- holding CI
# polling in the foreground blocks the turn, and the operator asked twice what
# it was doing there.
#
#   merge_train.sh "<pr numbers, space separated>" [predecessor-log]
#
# The optional second argument is another train's log file; this run waits for
# that train to print "merge train complete" first. Two trains merging to main
# at once is not a race in the merge itself -- it is each train invalidating the
# other's readiness check, so every remaining PR goes BEHIND on every merge.
#
# REBUILT 2026-08-09 after a restart wiped the scratchpad. Three corrections
# made during the day are carried forward deliberately; see each one below.
set -u
cd /Users/yasudatetsuya/Workspace/reyn_dev/lead-coder || exit 1
ORDER="${1:?usage: merge_train.sh \"<pr numbers>\" [predecessor-log]}"

[ -n "${2:-}" ] && while ! grep -q "merge train complete" "$2" 2>/dev/null; do sleep 20; done

for p in $ORDER; do
  # Visual items are covered by the operator's standing waiver (CLAUDE.md rule 1):
  # the only place they can SEE a visual result is main, so holding the merge for
  # one inverts the real dependency. Every OTHER unchecked item still stops the
  # train. Report what was skipped -- a guard that quietly narrows its own scope
  # reads as "nothing was outstanding" when something was.
  un=$(gh pr view "$p" --json body --jq '[.body|split("\n")[]|select(startswith("- [ ]"))|select(test("Visual")|not)]|length')
  vis=$(gh pr view "$p" --json body --jq '[.body|split("\n")[]|select(startswith("- [ ]"))|select(test("Visual"))]|length')
  [ "${vis:-0}" != "0" ] && echo "note: #$p has $vis unchecked Visual item(s) -- proceeding under the owner waiver"
  # Says what it measured, not what it assumed. An earlier version reported
  # "grew N unchecked items since launch" while only ever comparing against 0 --
  # it never recorded a launch value, so "grew" was a claim the code could not
  # support. Reporting an unmeasured quantity is the same defect this train
  # exists to catch in PRs.
  if [ "${un:-1}" != "0" ]; then
    echo "STOP: #$p has $un unchecked non-Visual Test-plan item(s)"; exit 1
  fi

  # A PR that touches tests/ does not merge until I have said, on the PR, that I
  # read them. On 2026-08-09 two tests pinned a third-party library's behaviour,
  # passed test_tier_audit (its Tier check matches a string, not a claim), and I
  # merged both having read their bodies -- one of them then cost the operator
  # three reboots. "I will open the tests next time" is the shape of promise this
  # train exists to replace with a condition.
  if gh pr diff "$p" --name-only 2>/dev/null | grep -q '^tests/'; then
    if ! gh pr view "$p" --json comments \
         --jq '[.comments[]|select(.body|test("TESTS-READ"))]|length' 2>/dev/null \
         | grep -qv '^0$'; then
      echo "STOP: #$p touches tests/ and carries no TESTS-READ note."
      echo "      Open the test code, then comment: **[lead-coder]** TESTS-READ: <what each test claims, and whose contract it is>"
      exit 1
    fi
  fi

  # Wait for the branch to actually stop being BEHIND, rather than issuing one
  # update and sleeping a guessed 20s. Every merge in this train puts the PRs
  # behind it BEHIND again, so "update once" is right only for the first one --
  # #3875 sat stopped at "is BEHIND, not CLEAN" after its own CI had gone green,
  # because the single update raced the merge ahead of it in the same train.
  b=0
  while [ "$b" -lt 20 ]; do
    state=$(gh pr view "$p" --json mergeStateStatus --jq .mergeStateStatus)
    [ "$state" != "BEHIND" ] && break
    gh pr update-branch "$p" >/dev/null 2>&1 \
      && echo "#$p BEHIND, update-branch issued (attempt $((b+1)))"
    b=$((b+1)); sleep 15
  done
  [ "$b" -ge 20 ] && { echo "STOP: #$p would not stop being BEHIND"; exit 1; }

  # Retry loop: BEHIND after green is NOT a failure -- main moved while this PR's
  # CI was running, which happens constantly with several PRs in one train. The
  # old code STOPped here and the train died silently 4 times in one night (the
  # STOP goes to a log nobody reads). Only a state waiting cannot fix is a stop.
  attempt=0
  while [ "$attempt" -lt 6 ]; do
  attempt=$((attempt+1))
  # Anchor every check to ONE sha. `gh pr checks` answers "is the PR green"
  # without saying WHICH tree was green -- and a push landing mid-poll makes
  # those two different questions with the same answer shape.
  sha=$(gh pr view "$p" --json headRefOid --jq .headRefOid)
  echo "#$p head=$(echo "$sha" | cut -c1-9), waiting for its checks"
  n=0
  while [ "$n" -lt 90 ]; do
    runs=$(gh api "repos/tya5/reyn/commits/$sha/check-runs" --jq '.check_runs' 2>/dev/null)
    total=$(echo "$runs" | jq 'length' 2>/dev/null)
    pending=$(echo "$runs" | jq '[.[]|select(.status!="completed")]|length' 2>/dev/null)
    failed=$(echo "$runs" | jq '[.[]|select(.conclusion=="failure" or .conclusion=="cancelled" or .conclusion=="timed_out")]|length' 2>/dev/null)
    # total > 0 matters: an empty check-run list is "CI has not started", which
    # has the same shape as "nothing failed". A cancelled run is red here too --
    # #3670 found `conclusion == "failure"` queries silently treating a
    # timed-out job as not-red.
    if [ "${total:-0}" -gt 0 ] && [ "${pending:-1}" -eq 0 ]; then
      if [ "${failed:-1}" -ne 0 ]; then echo "STOP: #$p has $failed failing/cancelled check(s) at $sha"; exit 1; fi
      break
    fi
    n=$((n+1)); sleep 20
  done
  [ "$n" -ge 90 ] && { echo "STOP: #$p checks did not settle for $sha"; exit 1; }

  now=$(gh pr view "$p" --json headRefOid --jq .headRefOid)
  [ "$now" != "$sha" ] && { echo "STOP: #$p head moved ($sha -> $now); the green belongs to a tree that is no longer the PR"; exit 1; }

  ms=$(gh pr view "$p" --json mergeStateStatus --jq .mergeStateStatus)
  if [ "$ms" = "BEHIND" ]; then
    echo "#$p went BEHIND after green (attempt $attempt) -- update-branch, re-wait"
    gh pr update-branch "$p" >/dev/null 2>&1
    sleep 20
    continue
  fi
  [ "$ms" != "CLEAN" ] && { echo "STOP: #$p is $ms, not CLEAN"; exit 1; }

  gh pr merge "$p" --squash --delete-branch >/dev/null 2>&1 \
    && echo "MERGED #$p" || { echo "STOP: #$p merge command failed"; exit 1; }
  break
  done
  [ "$attempt" -ge 6 ] && { echo "STOP: #$p could not stay CLEAN after 6 attempts"; exit 1; }
  sleep 5
done
echo "merge train complete"
