# TypeScript / JS review checklist

## P0 (block stage)
- Non-null assertion `!` on user-provided or network-derived value (e.g., `req.body.user!.id`).
- `as` cast of a network response or user input without runtime validation.
- `eval()` / `Function(...)` on data crossing a trust boundary.
- Direct DOM injection of user-supplied HTML without sanitization (XSS risk).
- Missing await on a promise whose rejection would surface as an unhandled rejection in production.

## P1 (advisory)
- `any` type used where a concrete type is feasible (especially function parameters and public API surfaces).
- `console.log` left in non-debug code paths.
- `// @ts-ignore` / `// @ts-expect-error` without a comment.
- Catch block that swallows the error silently.
- Use of `==` where `===` is the project default.

## P2 (nit)
- `let` used where `const` would suffice.
- Long functional chains where intermediate `const` would help readability.
- Comments stating what the code does instead of why.
