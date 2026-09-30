# Original-source native SDK fixture

This directory is the vendor source authority for the native-SDK admission tests.
It is authored fixture code, not a generated Component or a simulated native result.
Its public Python package calls a real compiled C ABI. It has no Python arithmetic
fallback and loads its library relative to the SDK package.

CMake writes the SDK beneath `BUILD/sdk/python`, with both the public package and
native libraries. The public C ABI links a second native library, so relocation
tests must preserve real loader dependencies. Removing that helper from a newly
captured package must fail native observation before another consumer load.
The admission tests must treat those output bytes as a separate
dependency product. They must not copy the vendor implementation into the generated
consumer's source or admit an ambient package with the same import name.

The fixture uses the repository's license. Its source revision, recipe, license,
public import contract, selected target/compiler and product bytes must all be bound
by the eventual SDK admission. Building this fixture alone does not prove that
Standard accepts or packages SDK dependencies.
