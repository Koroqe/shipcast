"""Jinja2 prompt-template renderer shared across AI stages.

Stages 02 / 06 / 07 / 08 each render a prompt before calling Claude. This
module exposes ONE function — `render_prompt(template_name, **vars) -> str`
— that loads the named template from the repo's `prompts/` directory and
returns the rendered string.

Design constraints (architect verdict for stage_02_write_script):

* **Stateless.** Each call constructs a fresh `jinja2.Environment`. There is
  no module-level cache. This keeps testing trivial (every call independent)
  and the cost is negligible at our call rate (one render per stage run).
  See architect REC-1.
* **Single public symbol.** `__all__ = ["render_prompt"]` — no `Environment`,
  no `FileSystemLoader`, no path helper exported. See architect REC-3.
* **No autoescape.** Prompts go to LLMs as plain text, not HTML. Operator-
  supplied `notes` containing markdown formatting is rendered verbatim.
* **No `{% include %}` outside `prompts/`.** The `FileSystemLoader` is
  bound to `shipcast.paths.default_prompts_path()` only. Jinja2 will refuse
  to load templates from anywhere else.
* **`TemplateNotFound` propagates unchanged.** No custom wrapping. The
  dispatcher's existing failure path captures it as `error.type` in the
  manifest (FR-2.2).
"""

from __future__ import annotations

from jinja2 import Environment, FileSystemLoader

from shipcast.paths import default_prompts_path

__all__ = ["render_prompt"]


def render_prompt(template_name: str, **template_vars: object) -> str:
    """Render `template_name` from `prompts/` with the supplied variables.

    Constructs a fresh `Environment` per call (stateless — REC-1). The
    template file is resolved via `FileSystemLoader(default_prompts_path())`.

    Args:
        template_name: filename relative to `prompts/`, e.g. `"02_script.md.j2"`.
        **template_vars: variables passed to `template.render(**vars)`.

    Returns:
        The rendered template as a string.

    Raises:
        jinja2.TemplateNotFound: when `template_name` does not exist under
            `prompts/`. The dispatcher records this as a stage failure
            (FR-2.2) without wrapping the exception.
        jinja2.TemplateSyntaxError: when the template itself is malformed.
            Also propagates unchanged.
    """
    # autoescape=False is intentional: output goes to an LLM as plain text,
    # not to HTML; operator-supplied `notes` markdown is rendered verbatim.
    env = Environment(
        loader=FileSystemLoader(str(default_prompts_path())),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template(template_name)
    return template.render(**template_vars)
