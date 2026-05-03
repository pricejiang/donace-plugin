# General review checklist (stack-agnostic)

Use this when the diff doesn't match a more specific stack (Python / TypeScript / iOS) or contains a mix.

## P0 (block stage)
- Hardcoded credential / secret in source.
- Logic that silently swallows errors that should propagate.
- Off-by-one in a loop bound that handles user-controlled length.
- Dropping a database column / breaking a public API contract not in the spec.

## P1 (advisory)
- Function longer than ~100 lines without internal structure.
- Duplication of a logic block that already exists nearby.
- Missing error handling for a plausible failure mode (e.g., the network call you just added has no timeout).
- Comment that states what the code does (the code already does that) but doesn't explain why.

## P2 (nit)
- Magic numbers in business logic.
- Commented-out code in the diff.
- Variable names that abbreviate beyond clarity (`u` for user, `cfg` for config — judge by neighbors).
- Inconsistency with project conventions in non-load-bearing places.
