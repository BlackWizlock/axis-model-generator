#!/bin/bash
set -euo pipefail
main() {
if [[ "$*" == "--task all" ]]; then
  cd "$(dirname "$0")/.."
  python3 scripts/ci-web-all.py
  return
fi
if [[ "$*" == "--task ui" ]]; then
  cd "$(dirname "$0")/.."
  run_id="$$-$RANDOM-$RANDOM"
  ui_tag="axis-model-generator/$(basename "$PWD"):ui-test-${run_id}"
  trap 'docker image rm "$ui_tag" >/dev/null 2>&1 || true' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  docker buildx build --check --platform "${MG_TEST_PLATFORM:-linux/arm64}" -f deploy/web/Dockerfile.frontend-test .
  docker build --platform "${MG_TEST_PLATFORM:-linux/arm64}" --build-arg "MG_BUILD_REVISION=$(git rev-parse HEAD)" --build-arg "MG_BUILD_WORKTREE=$(basename "$PWD")" -t "$ui_tag" -f deploy/web/Dockerfile.frontend-test .
  docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges --memory 1g --cpus 1 "$ui_tag" sh -c 'npm --prefix web test && npm --prefix web run lint && npm --prefix web run test:coverage'
  docker image inspect "$ui_tag" --format 'Frontend image proof: {{.Id}} {{.Os}}/{{.Architecture}}'
  return
fi
cd "$(dirname "$0")/.."
[[ "$*" == '--task diagnostics' || "$*" == '--task upload_chunks' || "$*" == '--task artifacts' || "$*" == '--task auth' || "$*" == '--task storage' || "$*" == '--task jobs' || "$*" == '--task preview' ]] || { echo 'Usage: bash scripts/ci-web.sh --task auth|storage|jobs|preview|artifacts|upload_chunks|diagnostics'; exit 2; }
task="$2"
export MG_TEST_PLATFORM="${MG_TEST_PLATFORM:-linux/amd64}"
[[ "$MG_TEST_PLATFORM" == linux/amd64 || "$MG_TEST_PLATFORM" == linux/arm64 ]] || { echo 'Unsupported test platform.'; exit 2; }
export MG_TEST_PURPOSE="${task}-test"
docker info >/dev/null 2>&1 || { echo 'Docker unavailable: infrastructure gate failed.'; exit 1; }
worktree="${MG_TEST_WORKTREE_NAME:-$(basename "$PWD")}"
[[ "$worktree" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || { echo 'Invalid worktree name.'; exit 2; }
revision="${MG_TEST_REVISION:-$(git rev-parse HEAD 2>/dev/null || echo standalone-local)}"
branch="${MG_TEST_BRANCH:-$(git branch --show-current 2>/dev/null || echo standalone-local)}"
run_id="$$-$RANDOM-$RANDOM"
project="axis-model-generator-${worktree}-${task}-${run_id}"
export MG_TEST_WORKTREE_NAME="$worktree" MG_TEST_REVISION="$revision"
export MG_TEST_EMULATOR_IMAGE="axis-model-generator/${worktree}:storage-emulator-test-${run_id}"
export MG_TEST_IMAGE="axis-model-generator/${worktree}:${task}-test-${run_id}"
export MG_TEST_BUILD_NAME="axis-model-generator | ${worktree} | ${task}-test/tests | ${revision:0:12} | $$"
export MG_TEST_EMULATOR_BUILD_NAME="axis-model-generator | ${worktree} | ${task}-test/emulator | ${revision:0:12} | $$"
export MG_TEST_SECRET_ROOT="$PWD/.web-test-runtime/${project}"
echo "Working directory: $PWD"
echo "Selected worktree: ${MG_TEST_CANONICAL_WORKTREE:-$PWD}; branch: $branch"
echo "Purpose: ${task}-test; project: $project; image: $MG_TEST_IMAGE"
echo "Docker build name: $MG_TEST_BUILD_NAME; revision: $revision"
if [[ "$task" == jobs || "$task" == preview || "$task" == artifacts || "$task" == upload_chunks || "$task" == diagnostics ]]; then
  # Fail before generating credentials/building infrastructure if the required
  # actual kernel controls cannot be installed. Unsupported never becomes skip.
  docker run --rm --platform "$MG_TEST_PLATFORM" --cap-drop ALL --security-opt no-new-privileges \
    --user 10001:10001 --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --network none --pids-limit 16 --memory 256m --cpus 1 \
    --label com.axis.model-generator.project=axis-model-generator \
    --label "com.axis.model-generator.worktree=$worktree" \
    --label com.axis.model-generator.purpose=jobs-linux-guard-capability \
    --label "org.opencontainers.image.revision=$revision" \
    -v "$PWD/tests/web/fixtures/guard_probe.py:/guard_probe.py:ro" \
    python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 \
    python /guard_probe.py --capabilities
  echo 'Kernel capability preflight passed; continuing required durable worker acceptance.'
fi
probe="${project}-restart-probe"
compose=(docker compose -p "$project" -f deploy/web/compose.test.yaml)
if [[ -n "${MG_TEST_COMPOSE_OVERRIDE:-}" ]]; then
  override="$(python3 -c 'import pathlib,sys; root=pathlib.Path.cwd()/"_scratch"; path=pathlib.Path(sys.argv[1]); resolved=path.resolve(strict=True); assert not path.is_symlink() and resolved.is_relative_to(root.resolve()) and resolved.suffix in {".yaml",".yml"}; print(resolved)' "$MG_TEST_COMPOSE_OVERRIDE")" || { echo 'Invalid own scratch Compose override.'; exit 2; }
  compose+=(-f "$override")
fi
cleanup() {
  docker rm -f "$probe" >/dev/null 2>&1 || true
  if "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1; then
    for image in "$MG_TEST_IMAGE" "$MG_TEST_EMULATOR_IMAGE"; do
      if docker image inspect "$image" >/dev/null 2>&1; then
        docker image rm "$image" >/dev/null 2>&1 || echo "Cleanup retained image in use: $image" >&2
      fi
    done
  else
    echo "Cleanup incomplete for $project; retaining image tags." >&2
  fi
  rm -rf "$MG_TEST_SECRET_ROOT"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p .web-test-runtime
chmod 700 .web-test-runtime
mkdir -m 700 "$MG_TEST_SECRET_ROOT"
docker run --rm --platform "$MG_TEST_PLATFORM" -v "$PWD/scripts/web-test-env.py:/generator.py:ro" -v "$MG_TEST_SECRET_ROOT:/runtime" python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 python /generator.py /runtime
docker buildx build --check --platform "$MG_TEST_PLATFORM" --build-arg "BUILDKIT_BUILD_NAME=$MG_TEST_BUILD_NAME" -f deploy/web/Dockerfile.test .
docker buildx build --check --platform "$MG_TEST_PLATFORM" --build-arg "BUILDKIT_BUILD_NAME=$MG_TEST_EMULATOR_BUILD_NAME" -f deploy/web/Dockerfile.s3-test-emulator .
"${compose[@]}" build minio tests
expected="linux ${MG_TEST_PLATFORM#linux/} axis-model-generator $worktree ${task}-test $revision"
actual="$(docker image inspect "$MG_TEST_IMAGE" --format '{{.Os}} {{.Architecture}} {{index .Config.Labels "com.axis.model-generator.project"}} {{index .Config.Labels "com.axis.model-generator.worktree"}} {{index .Config.Labels "com.axis.model-generator.purpose"}} {{index .Config.Labels "org.opencontainers.image.revision"}}')"
[[ "$actual" == "$expected" ]] || { echo 'Image identity gate failed.'; exit 1; }
echo "Image identity verified: $actual"
emulator_actual="$(docker image inspect "$MG_TEST_EMULATOR_IMAGE" --format '{{.Os}} {{.Architecture}} {{index .Config.Labels "com.axis.model-generator.project"}} {{index .Config.Labels "com.axis.model-generator.worktree"}} {{index .Config.Labels "com.axis.model-generator.purpose"}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "com.axis.model-generator.source-sha256"}}')"
[[ "$emulator_actual" == "linux ${MG_TEST_PLATFORM#linux/} axis-model-generator $worktree storage-emulator-test $revision 45521908307306e925c98d629e1c17d78c8b72b6ee242b1bfb1409f7d8ee5841" ]] || { echo 'Emulator image identity gate failed.'; exit 1; }
echo "Emulator image identity verified: $emulator_actual"
docker image inspect "$MG_TEST_EMULATOR_IMAGE" --format 'Local emulator image ID: {{.Id}}'
history="$(docker buildx history ls --format '{{.Name}}')"
found=false
while IFS= read -r name; do [[ "$name" != "$MG_TEST_BUILD_NAME" ]] || found=true; done <<< "$history"
[[ "$found" == true ]] || { echo 'Docker build history name gate failed.'; exit 1; }
echo "Docker build history name verified: $MG_TEST_BUILD_NAME"
found=false
while IFS= read -r name; do [[ "$name" != "$MG_TEST_EMULATOR_BUILD_NAME" ]] || found=true; done <<< "$history"
[[ "$found" == true ]] || { echo 'Emulator build history name gate failed.'; exit 1; }
echo "Emulator Docker build history name verified: $MG_TEST_EMULATOR_BUILD_NAME"
"${compose[@]}" up -d --wait postgres minio s3-proxy
for purpose in mg_test_db mg_test_s3_proxy mg_test_s3_backend; do
  network="${project}_${purpose}"
  [[ "$(docker network inspect "$network" --format '{{.Internal}}')" == true ]] || { echo 'Internal test network gate failed.'; exit 1; }
done
"${compose[@]}" run --rm tests python -m web.test_pipeline --network-gate
"${compose[@]}" run --rm tests python -c 'import os,pathlib,psycopg; con=psycopg.connect(pathlib.Path(os.environ["MG_DATABASE_URL_FILE"]).read_text()); assert con.execute("SELECT current_database(),current_user").fetchone()==("model_generator","mg_api"); con.close(); print("Required own PostgreSQL connection gate passed.")'
"${compose[@]}" run --rm tests python deploy/web/s3-test-init.py
"${compose[@]}" run --rm tests python -m model_generator.web.migrate
if [[ -n "${MG_WEB_TEST_MODULES:-}" ]]; then
  read -r -a modules <<< "$MG_WEB_TEST_MODULES"
  "${compose[@]}" run --rm tests python -m unittest "${modules[@]}" -v
  return
fi
if [[ "$task" == auth ]]; then
  "${compose[@]}" run --rm tests python -m unittest web.test_auth web.test_security web.test_config_db web.test_postgres web.test_pipeline -v
else
  "${compose[@]}" run --rm tests
fi
"${compose[@]}" run --rm tests python -c 'import sys,unittest,pathlib; suite=unittest.TestSuite(unittest.defaultTestLoader.discover("tests",pattern=name.name) for name in pathlib.Path("tests").glob("test_*.py")); result=unittest.TextTestRunner(verbosity=1).run(suite); sys.exit(not result.wasSuccessful())'
"${compose[@]}" run --rm tests python -m pip check
if [[ "$task" == jobs || "$task" == preview || "$task" == artifacts || "$task" == upload_chunks || "$task" == diagnostics ]]; then
  # Required actual managed-container lifetime/recovery probes run on the host.
  # No Docker socket or daemon is available inside tests or worker containers.
  bash scripts/ci-web-worker-probes.sh "$project"
fi
if [[ "$task" == storage || "$task" == jobs || "$task" == preview || "$task" == artifacts || "$task" == upload_chunks || "$task" == diagnostics ]]; then
  "${compose[@]}" run --rm tests python -m web.test_uploads --memory-probe
  "${compose[@]}" run -d --name "$probe" tests python -m web.test_pipeline --restart-probe storage >/dev/null
else
  "${compose[@]}" run -d --name "$probe" tests python -m web.test_pipeline --restart-probe >/dev/null
fi
wait_probe() {
  local expected="$1" stage
  for ((attempt=0; attempt<600; attempt++)); do
    stage="$(docker exec "$probe" cat /proof/state 2>/dev/null || true)"
    if [[ "$stage" == "$expected" ]]; then return; fi
    if [[ "$(docker inspect "$probe" --format '{{.State.Running}}')" != true ]]; then
      docker logs "$probe"; echo 'Restart probe failed.'; exit 1
    fi
    sleep 0.1
  done
  echo 'Restart probe deadline failed.'; exit 1
}
wait_probe seeded
"${compose[@]}" stop postgres >/dev/null
if [[ "$task" == storage || "$task" == jobs || "$task" == preview || "$task" == artifacts || "$task" == upload_chunks || "$task" == diagnostics ]]; then "${compose[@]}" stop minio >/dev/null; fi
docker exec "$probe" sh -c 'echo -n offline > /proof/command'
wait_probe offline-passed
"${compose[@]}" up -d --wait postgres >/dev/null
if [[ "$task" == storage || "$task" == jobs || "$task" == preview || "$task" == artifacts || "$task" == upload_chunks || "$task" == diagnostics ]]; then "${compose[@]}" up -d --wait minio >/dev/null; fi
docker exec "$probe" sh -c 'echo -n recovered > /proof/command'
wait_probe restart-passed
docker exec "$probe" sh -c 'echo -n finish > /proof/command'
# Stop waiting only after the probe process itself exits successfully.
[[ "$(docker wait "$probe")" == 0 ]] || { docker logs "$probe"; exit 1; }
docker logs "$probe"
if [[ "$task" == upload_chunks || "$task" == diagnostics ]]; then
  docker rm "$probe" >/dev/null
  "${compose[@]}" run --rm tests python -m web.test_upload_chunks --memory-probe
  "${compose[@]}" run -d --name "$probe" tests python -m web.test_upload_chunks --restart-probe >/dev/null
  wait_probe chunks-seeded
  "${compose[@]}" stop postgres minio >/dev/null
  docker exec "$probe" sh -c 'echo -n chunks-offline > /proof/command'
  wait_probe chunks-offline-passed
  "${compose[@]}" up -d --wait postgres minio >/dev/null
  docker exec "$probe" sh -c 'echo -n chunks-recovered > /proof/command'
  wait_probe chunks-restart-passed
  docker exec "$probe" sh -c 'echo -n chunks-finish > /proof/command'
  [[ "$(docker wait "$probe")" == 0 ]] || { docker logs "$probe"; exit 1; }
  docker logs "$probe"
fi
if [[ "$task" == diagnostics ]]; then
  docker rm "$probe" >/dev/null
  "${compose[@]}" run --rm tests python -m web.test_diagnostics_live --journal-write
  "${compose[@]}" run --rm tests python -m web.test_diagnostics_live --journal-read
fi
echo "$task Docker acceptance passed."
if [[ "$task" == preview ]]; then
  echo 'Actual python-cpu guarded render, geometry, recovery and process acceptance passed. Native amd64 release validation remains a separate production gate.'
fi

}
main "$@"
