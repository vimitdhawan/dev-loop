"""Versioned prompts.

Layout: `prompts/<role>/v<N>.md` plus `prompts/_contract.md`, which is
appended to every role prompt and states the file-contract rules. The
newest version is used unless `DEVLOOP_PROMPT_<ROLE>=v<N>` pins one.

The recorded version is `v<N>+<sha8>` over the exact template text, so an
edit made without bumping the file name still shows up as a different
version in `agent_runs` — the improvement engine must never compare two
prompts that both claim to be `v1`.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from string import Template

from devloop.contracts.runs import Role
from devloop.paths import prompts_dir

_VERSION_FILE = re.compile(r"^v(\d+)\.md$")


@dataclass(frozen=True)
class Prompt:
    role: Role
    version: str
    template: str

    def render(self, **values: str) -> str:
        # `substitute`, not `safe_substitute`: a missing variable is a bug
        # in the prompt, and it should fail here rather than reach a model.
        return Template(self.template).substitute(**values)


def load_prompt(role: Role) -> Prompt:
    role_dir = prompts_dir() / role.value
    pinned = os.environ.get(f"DEVLOOP_PROMPT_{role.value.upper()}")
    if pinned:
        name = pinned
    else:
        versions = sorted(
            (int(m.group(1)), f.stem)
            for f in role_dir.glob("v*.md")
            if (m := _VERSION_FILE.match(f.name))
        )
        if not versions:
            raise FileNotFoundError(f"no prompt versions in {role_dir}")
        name = versions[-1][1]

    text = (role_dir / f"{name}.md").read_text()
    shared = prompts_dir() / "_contract.md"
    if shared.exists():
        text = f"{text.rstrip()}\n\n{shared.read_text()}"
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    return Prompt(role=role, version=f"{name}+{digest}", template=text)
