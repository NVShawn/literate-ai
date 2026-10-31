.PHONY: all test clean
LITAI_LANGUAGE_TOOL ?= node
EXPORT_PATH ?= _build/lab.js
all:
	"$(LITAI_LANGUAGE_TOOL)" tools/build.js "$(EXPORT_PATH)"
test: all
	"$(LITAI_LANGUAGE_TOOL)" "$(EXPORT_PATH)" --litai-test
clean:
	@echo "Disposable outputs belong to the lifecycle-owned build directory."
