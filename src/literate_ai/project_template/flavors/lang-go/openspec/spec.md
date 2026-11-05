# Go implementation Flavor

### Requirement: Portable Go implementation

When `implementation.language-ecosystem=go` is selected, generate a Go application
with `source/main.go` as its entrypoint and `source/tests/litai_runtime.go` as a
compiled native behavior-test package imported through the declared local `./tests`
path. The application SHALL compile with one direct `go build` invocation and no
`go.mod`, external module, `go:generate` directive, cgo, unsafe package, or
platform-specific build tag. It SHALL import only standard-library packages and the
declared local test package. Its native executable SHALL accept one UTF-8 JSON array
containing the declared entrypoint arguments as its sole command-line argument
(`argv[1]`), SHALL NOT read the request from standard input, and SHALL write only the
JSON result to standard output. The `--litai-test` mode SHALL execute compiled
assertions against actual application behavior without reading the test manifest.

#### Scenario: Go application executes

- **WHEN** the generated file is compiled by the selected host Go toolchain
- **THEN** the native executable implements the acceptance contract for every role assigned to the Go Flavor
