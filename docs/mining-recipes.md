# Mining recipes

Wrapper scripts and `mine.yaml` snippets for toolchains that are awkward to run directly on a
host. They come from field tests of `crucible mine` on public repositories: Go
([hashicorp/go-version](https://github.com/hashicorp/go-version)), Ruby
([DannyBen/victor](https://github.com/DannyBen/victor)), Maven with a Spring backend and React
frontend ([gcode-de/SitemapDiff](https://github.com/gcode-de/SitemapDiff)) and a Gradle
multi-project build ([square/moshi](https://github.com/square/moshi)). Adapt names, images and
versions to your repo, then run `crucible mine calibrate` to prove the result.

Things every recipe relies on:

- Wrapper scripts live in `.crucible/scripts/` and are run as `bash "$CRUCIBLE_SCRIPTS/<name>"`.
  `crucible mine scaffold` copies them into the task, so `check.sh` and `crucible run` behave like
  `--verify`.
- `setup` runs **before** the PR's test files are copied in, on the plain tree. It may compile
  main code (`mvn compile`, `gradle classes`) but must not need the new tests. The files arrive
  afterwards, in `$CRUCIBLE_TEST_FILES` (repo-relative) and `$CRUCIBLE_TEST_FILES_CWD` (relative to
  the suite `cwd`), newline-separated.
- Guard against an empty list: never pass an empty string to the runner (see the loops below).
- A wrapper that starts containers makes the task need Docker on every machine that runs it;
  scaffold warns and records `requires: ["docker"]` in the task's `.mine.json`.
- Use only dummy values in `env` (`JAVA_HOME` and `GO_MODULE`-style settings are fine).

## Go, through a Docker wrapper

Use when Go is not installed on the host, or the repo needs another Go version. Two scripts:
one runs any command inside a Go container, one maps the PR's test files to packages (and
handles commits from before the repo had a `go.mod`).

`.crucible/scripts/go-docker.sh`:

```bash
#!/usr/bin/env bash
# Run a command in a Go container with the current directory mounted: go-docker.sh go test ./...
set -euo pipefail
image="${GO_IMAGE:-golang:1.22}"
cache="${CRUCIBLE_GO_CACHE:-$HOME/.cache/crucible-go}"
mkdir -p "$cache/mod" "$cache/build"
exec docker run --rm -u "$(id -u):$(id -g)" \
  -e HOME=/tmp -e GOMODCACHE=/cache/mod -e GOCACHE=/cache/build -e GOFLAGS=-buildvcs=false \
  -v "$cache":/cache -v "$PWD":/src -w /src "$image" "$@"
```

`.crucible/scripts/go-test.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
pkgs=()
while IFS= read -r f; do
  [ -n "$f" ] || continue
  d="$(dirname "$f")"
  pkg="./$d"
  [ "$d" = . ] && pkg="./"            # a file next to go.mod: the root package
  case " ${pkgs[*]-} " in *" $pkg "*) ;; *) pkgs+=("$pkg") ;; esac
done <<< "${CRUCIBLE_TEST_FILES_CWD:-}"
[ "${#pkgs[@]}" -gt 0 ] || { echo "no test files" >&2; exit 2; }

# Older commits predate go.mod: make a module for them (GO_MODULE = the repo's import path).
if [ ! -f go.mod ]; then
  bash "$CRUCIBLE_SCRIPTS/go-docker.sh" sh -c \
    "go mod init '${GO_MODULE:-example.com/mined}' && go mod tidy"
fi
exec bash "$CRUCIBLE_SCRIPTS/go-docker.sh" go test -count=1 "${pkgs[@]}"
```

`.crucible/mine.yaml`:

```yaml
suites:
  - name: go
    test_paths: ["**/*_test.go", "**/testdata/**"]
    runnable: ["**/*_test.go"]
    cwd: .
    setup: 'if [ -f go.mod ]; then bash "$CRUCIBLE_SCRIPTS/go-docker.sh" go mod download; fi'
    test_command: bash "$CRUCIBLE_SCRIPTS/go-test.sh"
    env: {GO_MODULE: github.com/hashicorp/go-version, GO_IMAGE: "golang:1.22"}
    timeout: 1200
```

Without Docker, the shipped preset does the same on the host: `go test -count=1
{tests|dir|unique|prefix:./}` (a root-level test file becomes `./`).

## Gradle with JDK toolchains, through Docker

Use when the build wants a JDK (or several, via Gradle toolchains) that the host does not have.
Gradle runs inside a JDK image; the repo's own `./gradlew` is used, and its toolchain downloads
are cached on the host.

`.crucible/scripts/gw.sh`:

```bash
#!/usr/bin/env bash
# Run the repo's Gradle wrapper in a JDK container: gw.sh test --console=plain
set -euo pipefail
image="${JDK_IMAGE:-eclipse-temurin:21-jdk}"
cache="${CRUCIBLE_GRADLE_CACHE:-$HOME/.cache/crucible-gradle}"
mkdir -p "$cache"
exec docker run --rm -u "$(id -u):$(id -g)" \
  -e HOME=/gradle-home -e GRADLE_USER_HOME=/gradle-home \
  -v "$cache":/gradle-home -v "$PWD":/work -w /work "$image" ./gradlew "$@"
```

`.crucible/scripts/run-tests.sh`: selects the PR's test classes. In a multi-project build
`--tests` fails every sub-project with no matching test, so an init script turns that off:

```bash
#!/usr/bin/env bash
set -euo pipefail
args=()
while IFS= read -r f; do
  [ -n "$f" ] || continue
  name="$(basename "$f")"; name="${name%.*}"
  args+=(--tests "*.$name")
done <<< "${CRUCIBLE_TEST_FILES_CWD:-}"
[ "${#args[@]}" -gt 0 ] || { echo "no test files" >&2; exit 2; }

init=.crucible-init.gradle            # inside the workspace so the container sees it
printf '%s\n' 'allprojects { tasks.withType(Test).configureEach { filter.failOnNoMatchingTests = false } }' > "$init"
trap 'rm -f "$init"' EXIT
bash "$CRUCIBLE_SCRIPTS/gw.sh" -I "$init" test --console=plain "${args[@]}"
```

`.crucible/mine.yaml`:

```yaml
suites:
  - name: gradle
    test_paths: ["**/src/test/**"]
    runnable: ["*Test.java", "*Test.kt", "*Tests.java", "*Tests.kt"]
    cwd: .
    setup: bash "$CRUCIBLE_SCRIPTS/gw.sh" classes --console=plain   # main code only
    test_command: bash "$CRUCIBLE_SCRIPTS/run-tests.sh"
    env: {JDK_IMAGE: "eclipse-temurin:21-jdk"}
    timeout: 1800
    setup_timeout: 3600
```

Do not use `testClasses` / `test-compile` in `setup`: the PR's tests are not there yet and a
test that references a class the base lacks would fail the build ("setup failed at base").

## Maven with `JAVA_HOME`

When a local JDK of the right version exists, point the suite at it instead of using a
container. `crucible mine` does this for you on first run: it reads the required Java version
(pom `maven.compiler.release` / `source` / `java.version`, the compiler plugin, CI
`setup-java`), writes it as a comment, and sets `env.JAVA_HOME` when it finds a matching JDK
(`/usr/libexec/java_home -v N` on macOS, `/usr/lib/jvm/*N*` on Linux, `JAVA_HOME_N_X64`-style
variables). By hand:

```yaml
suites:
  - name: maven
    test_paths: ["**/src/test/**"]
    cwd: .
    setup: mvn -B -q -DskipTests compile        # ./mvnw when the repo has the wrapper
    test_command: mvn -B test -Dtest={tests|stem|join:,} -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false
    env: {JAVA_HOME: /usr/lib/jvm/java-17-openjdk-amd64}    # macOS: /Library/Java/JavaVirtualMachines/<jdk>/Contents/Home
    setup_timeout: 3600
```

The diagnostics flag the usual JVM mismatches (`release version N not supported`, `Unsupported
class file major version`, Gradle `Cannot find a Java installation`, Lombok failing on a newer
JDK); each hint says to set `JAVA_HOME` or use a container wrapper like the Gradle one above
(`docker run ... maven:3-eclipse-temurin-17 mvn "$@"`). A frontend next to the Maven backend
(React, Vue) is a separate suite (`jest` / `vitest` preset in its directory).

## Ruby with `vendor/bundle`

Install gems inside the workspace so nothing touches the host's gem directory and each export
is self-contained:

```yaml
suites:
  - name: rspec
    test_paths: ["spec/**"]
    runnable: ["spec/**/*_spec.rb"]
    cwd: .
    setup: bundle config set --local path vendor/bundle && bundle install --quiet
    test_command: bundle exec rspec {tests}
```

A `LoadError: cannot load such file` before any test ran usually means the Ruby version does not
match `.ruby-version` / the Gemfile (put a matching Ruby first on `PATH` via `env`, or run the
commands in a `ruby:<version>` container wrapper) or the gems were not installed. Rails apps
also need a database prepared in `setup` (`bin/rails db:prepare`, dummy credentials in `env`).
