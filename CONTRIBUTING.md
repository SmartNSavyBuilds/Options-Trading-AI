# Contributing

Small, reviewable changes are preferred.

## Before opening a pull request

1. Keep paper-first behavior intact; do not add live-trading defaults.
2. Run the project tests from the repository root:
   ```bash
   python -m pytest tests -q
   ```
3. Update `README.md` or relevant runbooks when behavior or setup changes.
4. Keep secrets, `.env`, generated `outputs/`, and runtime data out of commits.
5. Explain any risk, approval, or broker-execution change explicitly.
