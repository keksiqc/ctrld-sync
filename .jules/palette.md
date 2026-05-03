## 2025-01-26 - [Silent Waits in CI]

**Learning:** Long silent waits in CLI tools (especially in CI/non-interactive mode) cause user anxiety about hung processes.
**Action:** Always provide periodic heartbeat logs (e.g. every 10s) for long operations in non-interactive environments.

## 2025-02-14 - [ASCII Fallback for Tables]

**Learning:** Using Unicode box drawing characters enhances the CLI experience, but a robust ASCII fallback is crucial for CI environments and piped outputs.
**Action:** Always implement a fallback mechanism (like checking `sys.stderr.isatty()`) when using rich text or Unicode symbols.

## 2025-02-28 - [Interactive Restart]

**Learning:** Reconstructing command arguments manually for process restarts is brittle and breaks forward compatibility.
**Action:** When restarting a CLI tool with modified flags (e.g., removing `--dry-run`), filter `sys.argv` instead of rebuilding the argument list from parsed args.

## 2025-03-05 - [CLI Progress Line Residue]

**Learning:** When using carriage return (`\r`) to animate CLI progress bars or countdowns, shrinking strings (e.g., transitioning from "10s" to "9s") leave visible ghost characters (residue) at the end of the line if not explicitly cleared.
**Action:** Always prefix carriage-return updates with the ANSI clear-line sequence (`\033[K`) to ensure the entire line is cleanly re-rendered.

## 2025-03-05 - [CLI Empty States]

**Learning:** Presenting a simple "Nothing to do" message when an operation is empty leaves the user without guidance.
**Action:** When presenting empty states in the CLI (e.g., no items to process), always provide actionable hints or call-to-actions, such as suggesting relevant CLI flags or configuration edits.

## 2025-03-12 - [Visual Hierarchy in CLI]

**Learning:** Using bright colors (like CYAN) for both primary actions and secondary hints creates visual noise and makes it harder for users to focus on what matters.
**Action:** Use DIM ANSI escape codes (\033[2m) for secondary or optional CLI text (like hints and follow-up instructions) to establish a clear visual hierarchy and reduce noise.

## 2025-03-12 - [Interactive Prompt Forgiveness]

**Learning:** When prompting users to press Enter to continue or Ctrl+C to cancel, users will often instinctively type "n", "no", or "quit" and press Enter. Ignoring this input and proceeding anyway leads to accidental and potentially destructive actions. Furthermore, prompts without a trailing space cause user input to visually collide with the prompt text.
**Action:** Always add a trailing space to input prompts, and gracefully intercept common cancellation strings (e.g., "n", "no", "quit") even if the explicit instruction only mentions Ctrl+C.

## 2025-03-24 - [Input Prompt Collision]

**Learning:** When prompting users for input via generic wrappers (e.g., `input()` or `getpass()`), if the prompt string lacks a trailing space, the user's typed input will visually collide with the prompt text, creating a poor aesthetic and confusing UX.
**Action:** Always append a trailing space automatically to prompt strings in generic input handler functions if one is not provided by the caller.

## 2025-03-24 - [Generic Input Cancellation Safety]

**Learning:** While intercepting strings like "n" or "no" for cancellation in interactive boolean prompts (e.g., "Ready to launch?") is good UX, applying this same interception logic universally to *generic* input functions (like `get_validated_input` or `get_password`) introduces severe functional and security regressions. A user whose valid answer is "no" or whose password happens to match a cancellation string will be unexpectedly booted from the application.
**Action:** Confine string-based cancellation interception to specific, appropriate contexts (like interactive confirmations). For generic input and password fields, rely solely on standard interrupt signals (Ctrl+C / Ctrl+D).

## 2024-04-15 - Uncolored Constant Embeddings
**Learning:** Hardcoding static ANSI color constants into string properties (e.g. `EMPTY_INPUT_HINT = f" {Colors.DIM}💡 Hint...{Colors.ENDC}"`) breaks the fallback display formatting when NO_COLOR is set, because evaluating `Colors.DIM` occurs *before* `USE_COLORS` resolves appropriately during import, or simply creates issues in non-interactive environments where emojis and hints get completely stripped out if a lazy developer adds them conditionally. Instead, the actual hints should be clean strings (with emojis intact), and they should be passed to a helper function like `_print_hint` that explicitly wraps the output in colors *only* if `USE_COLORS` is true.
**Action:** When adding static string constants to the module level or passing them around, never embed `Colors.XXX` directly. Instead, maintain pure strings and apply styling logic at the exact point of printing via conditional checks (`if USE_COLORS`). This ensures emojis and semantic information are preserved as uncolored text for fallback modes while keeping the CLI pretty when allowed.

## 2024-04-15 - Semantic Emojis in No-Color Fallbacks
**Learning:** When stripping ANSI colors for fallback modes (e.g., `NO_COLOR=1` or non-TTY environments), it's a common mistake to accidentally strip semantic emojis along with the color formatting. Emojis provide vital scannability and context that users rely on when color cues are absent.
**Action:** Always ensure that `if USE_COLORS` else blocks preserve emojis in the uncolored strings. Never treat emojis as part of the "color decoration" to be discarded.
