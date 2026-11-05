# Bundle builds outside compose. Run from the repository root: make -C deploy <target>

ROOT := $(abspath ..)

.PHONY: bundle-claude-code
# The Claude Code runtime bundle directory, in deploy/build/runtime-claude-code/
bundle-claude-code:
	rm -rf build/runtime-claude-code
	docker build -f runtime-claude-code.Dockerfile --target bundle \
	  --output type=local,dest=build/runtime-claude-code $(ROOT)
