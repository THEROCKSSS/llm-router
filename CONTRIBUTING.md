# Contributing

Thanks for considering a contribution. This repo is documentation-heavy by
design — the config examples are minimal and the docs carry the reasoning.

## Ground rules

1. **No secrets, ever.** No API keys, tokens, personal paths, private hostnames,
   or tailnet URLs in any file — including examples, docs, and commit messages.
   Run the secret scan (below) before every push.
2. **Examples must be runnable.** If you add or change a script or config
   example, state in the PR what you actually ran and what output you saw.
3. **Pitfalls are welcome as documentation.** If something cost you hours, add
   it to `docs/06-PITFALLS.md` in the right section with the symptom, cause,
   and fix.

## Secret scan (required before push)

```bash
grep -rInE '(sk-[A-Za-z0-9]{8,}|oc_sk_|ghp_|glpat-|Bearer [A-Za-z0-9._-]{12,})' .
grep -rInE --exclude-dir=.git --exclude=CONTRIBUTING.md '(ts\.net|/Users/|C:\\Users\\)' .
find . -name '.env' -o -name '*_key' -o -name 'master_key'
```

All three must come back empty except for `.env.example` placeholders.

## Workflow

- Branch, commit small, open a PR with: what changed, what you ran, what you saw.
- Keep docs in sync with behavior — a stale doc is a bug here.
