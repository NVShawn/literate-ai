# C++ implementation Flavor

### Requirement: Portable C++ implementation

When `implementation.language-ecosystem=cpp` is selected, generate a C++17
standard-library application with `source/main.cpp` as its entrypoint. Its native
executable SHALL accept one UTF-8 JSON array containing the declared entrypoint
arguments as its sole command-line argument (`argv[1]`), SHALL NOT read the request from
standard input, SHALL write only the JSON result to standard output, and SHALL use no
third-party or platform-specific dependency unless the selected OS Flavor requires it.

#### Scenario: C++ application executes

- **WHEN** the generated source is compiled by the selected host C++ toolchain
- **THEN** the native executable implements the same acceptance contract as every other language Flavor selected for the application

#### Scenario: A selected MSVC compiler requires its developer environment

- **WHEN** the selected compiler uses a Visual Studio developer environment
- **THEN** setup selects that compiler's compilation target and binds its exact toolset
- **AND** the compiler executable's host architecture is not substituted for its compilation target
- **AND** a conflicting or unproven target refuses before compilation
