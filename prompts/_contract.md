## Output contract

Your work is consumed by a program, not a person. The only thing that
counts is the JSON document you write to `$out_path`.

- Write exactly one JSON document to `$out_path` with your file-writing
  tool. Do not wrap it in markdown fences.
- It must validate against this JSON Schema:

```json
$schema
```

- Anything you say in chat is discarded. Do not put conclusions only in
  your reply — put them in the document.
- Never edit anything under `.devloop/in/`, and never run `git commit`,
  `git push` or `git checkout`. The orchestrator owns git.
