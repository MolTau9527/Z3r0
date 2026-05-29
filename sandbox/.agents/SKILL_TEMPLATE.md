---
name: example-skill
description: Use this skill for one clear sandbox task family. Keep this sentence short, specific, and action-oriented.
---

# Example Skill

Describe the task scope, when to use the skill, and any authorization boundary that matters.

## Help First

Use this section for direct CLI skills whose installed help output should be the source of truth.

```sh
tool --help
```

## Sandbox Paths

Use this section when the skill ships custom files.

- Wrapper script: `/root/.agents/skills/example-skill/scripts/example-skill.sh`
- Supporting files: `/root/.agents/skills/example-skill/scripts/support`

## Custom Script

Use this section when the skill ships a wrapper script. Always call custom scripts by absolute path.

```sh
/root/.agents/skills/example-skill/scripts/example-skill.sh [options] <input>
```

## Common Commands

Provide bounded examples that match the sandbox command model.

```sh
tool --help
/root/.agents/skills/example-skill/scripts/example-skill.sh --help
```

## Output

Describe what to report: command used, scope, relevant findings, output paths, and failures that affect completion.
