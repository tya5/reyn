#!/bin/sh
# Capture the identity of any process that grows large, BEFORE the machine dies.
# The operator lost a 30 GB python3.12 to a reboot on 2026-08-09 (twice); with
# the process gone, "reyn" vs "a peer's script" vs "a runaway test" cannot be
# told apart. Suspicion is not attribution.
#
# RSS ONLY. An earlier version also watched VSZ at a 20 GB threshold "to cover
# whichever column the operator had read" -- on macOS every process reports
# ~425 GB of virtual size (shared address space), so that threshold selected
# EVERY PROCESS and the watchdog spammed ~200 alerts before being killed.
# I added a second measure without ever looking at what it reads on this OS:
# the same defect this watchdog exists to catch, committed by the watchdog.
#
# So: if the operator's Activity Monitor figure turns out to be the virtual
# column, the answer is "that number is not a problem", NOT "widen the watch".
RSS_MB=${RSS_MB:-3000}
LOG=~/Workspace/reyn_dev/lead-coder/.local/rss_offenders.log
seen=""
while true; do
  # RSS is in KB on macOS ps. Full argv, not comm -- "python3.12" names the
  # interpreter, which is exactly the part that is never the answer.
  hits=$(ps -eo pid,rss,etime,args | awk -v t="$((RSS_MB*1024))" 'NR>1 && $2>t {print}')
  [ -z "$hits" ] && { sleep 30; continue; }
  echo "$hits" | while IFS= read -r line; do
    pid=$(echo "$line" | awk '{print $1}')
    mb=$(echo "$line" | awk '{printf "%.0f", $2/1024}')
    case " $seen " in *" $pid "*) continue;; esac
    seen="$seen $pid"
    printf '%s pid=%s rss=%sMB %s\n' "$(date -u +%FT%TZ)" "$pid" "$mb" "$(echo "$line" | cut -d' ' -f4-)" >> "$LOG"
    echo "RSS ALERT pid=$pid ${mb}MB -- $(echo "$line" | awk '{print $4, $5, $6}')"
  done
  sleep 30
done
