# iOS (Swift / Objective-C) review checklist

## P0 (block stage)
- Force-unwrap `!` of an Optional sourced from user input or network.
- Force-cast `as!` of a value that could be user-controlled or untrusted.
- Hardcoded API key / secret in source.
- Storing PII in `UserDefaults` (use Keychain for sensitive data).
- Networking off the main thread without back-pressure or cancellation.

## P1 (advisory)
- Implicitly Unwrapped Optional (`Type!`) on a stored property that isn't lazy.
- Long completion-handler chains that should be `async/await`.
- `print(...)` / `NSLog(...)` left in shipped code (use a logger).
- Strong reference cycle risk in closure captures (`self` not weakly captured).
- Force-unwrap of `Bundle.main.path(forResource:)` when the resource is optional in practice.

## P2 (nit)
- View controller code mixed with model logic that should be in a ViewModel.
- Magic numbers in layout constraints.
- Method names that don't match Swift API design guidelines.
