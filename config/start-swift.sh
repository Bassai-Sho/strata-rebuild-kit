#!/usr/bin/env bash
# Start Strata (Swift 1.5 IQ3_XXS). Frees GPU-held memory first and refuses to start if memory looks wrong.
cd ~/github/strata-work/Strata || exit 1
if docker ps --filter ancestor=strata-sycl-dev -q | grep -q .; then echo "an engine container is still running (docker ps); stop it first"; exit 1; fi
sync; echo 2 | sudo tee /proc/sys/vm/drop_caches >/dev/null
sleep 3
avail=$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo)
echo "MemAvailable: ${avail} GiB"
if [ "$avail" -lt 50 ]; then echo "only ${avail} GiB available (need about 50+): not starting"; exit 1; fi
export UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS=1 STRATA_WINDOW_SYNC=1 STRATA_ENGINE_STALL_S=900
./run-swift-iq3_xxs.sh 2>&1 | tee ~/strata-start-swift.log
