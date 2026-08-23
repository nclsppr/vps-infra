# Domain docs

Use these rules when an engineering skill explores this repository.

## Read before exploration

- Read `CONTEXT.md` at the repository root when it exists.
- Read the records under `docs/decisions/` that apply to the work.

Proceed when `CONTEXT.md` does not exist. Do not propose it as setup work. The `/domain-modeling` skill creates it when the project resolves domain terms.

## Layout

This repository uses one domain context:

```text
/
├── CONTEXT.md
├── docs/decisions/
├── applications/
├── ansible/
└── platform/
```

## Use canonical terms

Use each term as `CONTEXT.md` defines it. Do not replace a defined term with a synonym in an issue title, proposal, hypothesis, or test name.

If a required term is missing, first check whether the repository already uses another term. Record a real gap for `/domain-modeling`.

## Report decision conflicts

State when proposed work conflicts with a record under `docs/decisions/`. Name the record and explain why the decision should be reconsidered.
