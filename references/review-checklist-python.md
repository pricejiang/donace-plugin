# Python review checklist

Stack-specific items the reviewer should look for in Python diffs. Severity buckets here are advisory — the universal rubric in `reviewer.md` always takes precedence.

## P0 (block stage)
- `subprocess(..., shell=True)` with user-controlled input — command injection risk.
- `eval(...)` / `exec(...)` on data crossing a trust boundary.
- SQL string concatenation with user input (e.g., `f"SELECT * FROM x WHERE id = {user_id}"`); use parameterized queries instead.
- Missing or wrong `__init__.py` for a package that expects to be importable.
- Bare `assert` used as a runtime guard in production code (Python strips asserts under `-O`).

## P1 (advisory)
- Bare `except:` clauses (catches `KeyboardInterrupt`, `SystemExit`); use `except Exception:` or narrower.
- Mutable default arguments (`def f(x=[])`).
- Modifying a list while iterating over it.
- Returning `None` implicitly from a function whose other branches return values; either be explicit or restructure.
- Reaching into `_private` or `__dunder` attributes of another module.

## P2 (nit)
- `# type: ignore` without a comment explaining why.
- f-strings used for non-trivial logic that would be clearer as a helper.
- Trailing whitespace, missing newline at EOF (most repos auto-fix; flag only if the project doesn't).
- Commented-out code in the diff.
