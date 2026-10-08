#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
project="$1"
[[ "$project" =~ ^axis-model-generator-[a-z0-9-]+-(jobs|preview|artifacts|upload_chunks|diagnostics)-[0-9]+(-[0-9]+-[0-9]+)?$ ]] || { echo 'Own jobs/preview/artifacts/upload_chunks/diagnostics project required.'; exit 2; }
compose=(docker compose -p "$project" -f deploy/web/compose.test.yaml)
if [[ -n "${MG_TEST_COMPOSE_OVERRIDE:-}" ]]; then
  override="$(python3 -c 'import pathlib,sys; root=pathlib.Path.cwd()/"_scratch"; path=pathlib.Path(sys.argv[1]); resolved=path.resolve(strict=True); assert not path.is_symlink() and resolved.is_relative_to(root.resolve()) and resolved.suffix in {".yaml",".yml"}; print(resolved)' "$MG_TEST_COMPOSE_OVERRIDE")" || { echo 'Invalid own scratch Compose override.'; exit 2; }
  compose+=(-f "$override")
fi
worker="${project}-worker-test-1"
observer="${project}-old-namespace-observer"
wait_state() {
  local state="$1"
  for ((attempt=0;attempt<300;attempt++)); do
    if [[ "$state" == descendant ]]; then
      if docker exec "$worker" python /app/tests/web/fixtures/worker_probe.py status >/dev/null 2>&1; then return; fi
    else
      if "${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py verify "$state" >/dev/null 2>&1; then return; fi
    fi
    [[ "$(docker inspect "$worker" --format '{{.State.Running}}')" == true ]] || { docker logs "$worker"; echo 'Managed worker exited before probe barrier.'; exit 1; }
    sleep .1
  done
  echo 'Actual worker probe deadline failed.'; exit 1
}
cleanup_worker() { "${compose[@]}" rm -s -f worker-test >/dev/null 2>&1 || true; }
wait_exit() {
  for ((attempt=0;attempt<300;attempt++)); do
    if [[ "$(docker inspect "$worker" --format '{{.State.Running}}')" == false ]]; then return; fi
    sleep .1
  done
  echo 'Managed namespace teardown exceeded30s.'; exit 1
}
cleanup() { cleanup_worker; docker rm -f "$observer" >/dev/null 2>&1 || true; }
trap cleanup EXIT

"${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py seed
export MG_TEST_PROCESS_MODE=descendant
"${compose[@]}" up -d worker-test
wait_state descendant
"${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py verify running
if "${compose[@]}" run --rm --no-deps worker-test; then
  echo 'Second managed worker entered held epoch.'; exit 1
fi
docker exec "$worker" python -c 'from pathlib import Path; assert Path("/scratch/startup-preflight-count").read_text()=="1"; print("Second managed worker denied before parsing or claiming.")'
[[ "$(docker inspect "$worker" --format '{{.HostConfig.Init}}')" == true ]] || exit 1
# Keep the bounded tmpfs volume mounted while observing teardown; its contents
# otherwise disappear when Docker unmounts the final container reference.
"${compose[@]}" run -d --no-deps --user 10001:10001 --name "$observer" -v "${project}_worker_scratch:/old-worker:ro" tests python -c 'import time; time.sleep(60)' >/dev/null
# Kill only the actual worker child of init, retaining its writing descendant.
kill_status=0
docker exec "$worker" python -c 'import os,signal,pathlib; children=pathlib.Path("/proc/1/task/1/children").read_text().split(); assert len(children)==1; os.kill(int(children[0]),signal.SIGKILL)' || kill_status=$?
[[ "$kill_status" == 0 || "$kill_status" == 137 ]] || { echo 'Worker SIGKILL injection failed.'; exit 1; }
wait_exit
[[ "$(docker wait "$worker")" != 0 ]] || { echo 'SIGKILL did not terminate managed container.'; exit 1; }
[[ "$(docker inspect "$worker" --format '{{.State.Running}}')" == false ]] || exit 1
[[ "$(docker inspect "$worker" --format '{{.State.OOMKilled}}')" == false ]] || { echo 'Injected SIGKILL was masked by OOM.'; exit 1; }
docker exec "$observer" python -c 'from pathlib import Path; import time; files=list(Path("/old-worker/jobs").glob("*/*/descendant.writes")); assert len(files)==1; size=files[0].stat().st_size; assert size>0; time.sleep(1); assert files[0].stat().st_size==size; print("Old namespace descendant writes stopped before reclaim.")'
docker rm -f "$observer" >/dev/null
# Docker wait observes container teardown. Its old PID namespace is dead before
# creating/reclaiming another epoch; no old child can continue writing.
cleanup_worker
export MG_TEST_PROCESS_MODE=normal
"${compose[@]}" up -d worker-test
wait_state completed
"${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py verify completed
cleanup_worker

"${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py seed
export MG_TEST_PROCESS_MODE=descendant
"${compose[@]}" up -d worker-test
wait_state descendant
"${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py cancel
wait_state cancelled
"${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py verify cancelled
cleanup_worker

"${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py seed
export MG_TEST_PROCESS_MODE=descendant
"${compose[@]}" up -d worker-test
wait_state descendant
"${compose[@]}" stop postgres >/dev/null
wait_exit
[[ "$(docker wait "$worker")" != 0 ]] || { echo 'PG loss did not terminate managed epoch.'; exit 1; }
cleanup_worker
"${compose[@]}" up -d --wait postgres >/dev/null
export MG_TEST_PROCESS_MODE=normal
"${compose[@]}" up -d worker-test
wait_state completed
"${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py verify completed
cleanup_worker

for boundary in lease-multipart lease-terminal; do
  "${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py seed
  export MG_TEST_PROCESS_MODE="$boundary"
  "${compose[@]}" up -d worker-test
  wait_exit
  [[ "$(docker wait "$worker")" != 0 ]] || { echo 'Lost dedicated lease did not terminate managed epoch.'; exit 1; }
  docker logs "$worker" 2>&1 | grep -F "Synthetic dedicated backend terminated at $boundary"
  stage=unavailable
  [[ "$boundary" != lease-terminal ]] || stage=terminal-fenced
  "${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py verify "$stage"
  cleanup_worker
  export MG_TEST_PROCESS_MODE=normal
  "${compose[@]}" up -d worker-test
  wait_state completed
  "${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py verify completed
  cleanup_worker
done
echo 'Actual post-child dedicated PG lease loss fenced multipart and terminal COMMIT; managed unit exit/recovery passed.'

for boundary in before-checkpoint after-checkpoint; do
  "${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py seed
  export MG_TEST_PROCESS_MODE="$boundary"
  "${compose[@]}" up -d worker-test
  wait_exit
  [[ "$(docker wait "$worker")" == 17 ]] || { docker logs "$worker"; echo 'Checkpoint crash boundary not reached.'; exit 1; }
  if [[ "$boundary" == before-checkpoint ]]; then
    "${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py verify unavailable
  fi
  cleanup_worker
  export MG_TEST_PROCESS_MODE=normal
  "${compose[@]}" up -d worker-test
  wait_state completed
  "${compose[@]}" run --rm tests python /app/tests/web/fixtures/worker_probe.py verify completed
  cleanup_worker
done
echo 'Actual init SIGKILL namespace teardown, single processing and immutable S3 checkpoint recovery passed.'
