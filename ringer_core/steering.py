from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

STEERING_STATUSES = {"candidate", "confirmed", "refuted", "stale-pending-reverify"}
STEERING_AUDIENCES = {"driver", "worker"}
STEERING_RULE_HEADING_RE = re.compile(r"^## R\d+ · ([^\n]+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class SteeringRule:
    id: str
    status: str
    audience: str
    inject: str


@dataclass(frozen=True)
class SteeringProfile:
    model: str
    profile_version: str
    slug: str
    rules: tuple[SteeringRule, ...]


def _steering_yaml_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line != line.lstrip():
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
        if not match:
            continue
        value = match.group(2).strip()
        value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[match.group(1)] = value
    return values


def parse_steering_profile(text: str, *, slug: str | None = None) -> SteeringProfile | None:
    """Parse the small, documented subset of steering profile markdown. Never raise."""
    try:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return None
        frontmatter_end = next(
            index for index in range(1, len(lines)) if lines[index].strip() == "---"
        )
        frontmatter = _steering_yaml_values("\n".join(lines[1:frontmatter_end]))
        model = frontmatter.get("model", "").strip()
        profile_version = frontmatter.get("profile_version", "").strip()
        if not model or not profile_version:
            return None

        body = "\n".join(lines[frontmatter_end + 1 :])
        headings = list(STEERING_RULE_HEADING_RE.finditer(body))
        rules: list[SteeringRule] = []
        for heading in headings:
            next_section = re.search(r"^## ", body[heading.end() :], re.MULTILINE)
            section_end = (
                heading.end() + next_section.start() if next_section is not None else len(body)
            )
            section = body[heading.end() : section_end]
            yaml_match = re.search(r"```yaml[ \t]*\n(.*?)^```[ \t]*$", section, re.MULTILINE | re.DOTALL)
            if yaml_match is None:
                continue
            metadata = _steering_yaml_values(yaml_match.group(1))
            rule_id = metadata.get("id", "").strip()
            status = metadata.get("status", "").strip().lower()
            audience = metadata.get("audience", "driver").strip().lower() or "driver"
            if not rule_id or status not in STEERING_STATUSES or audience not in STEERING_AUDIENCES:
                continue

            inject_parts: list[str] = []
            found_inject = False
            for line in section.splitlines():
                stripped = line.strip()
                if not found_inject:
                    if stripped.startswith("**Inject:**"):
                        found_inject = True
                        first = stripped.removeprefix("**Inject:**").strip()
                        if first:
                            inject_parts.append(first)
                    continue
                if not stripped:
                    break
                inject_parts.append(stripped)
            inject_text = " ".join(inject_parts).strip()
            if not inject_text:
                continue
            rules.append(
                SteeringRule(
                    id=rule_id,
                    status=status,
                    audience=audience,
                    inject=inject_text,
                )
            )
        if not rules:
            return None
        return SteeringProfile(
            model=model,
            profile_version=profile_version,
            slug=(slug or model).strip(),
            rules=tuple(rules),
        )
    except Exception:
        return None

def load_steering_profile(path: Path) -> SteeringProfile | None:
    try:
        return parse_steering_profile(
            path.read_text(encoding="utf-8", errors="replace"),
            slug=path.stem,
        )
    except Exception:
        return None

def steering_profile_candidates(steering_dir: Path, resolved_model: str) -> tuple[Path, ...]:
    model = resolved_model.strip().lower()
    if not model:
        return ()
    slugs = [model.replace("/", "-"), model.rsplit("/", 1)[-1]]
    unique_slugs = tuple(dict.fromkeys(slugs))
    return tuple(steering_dir / "profiles" / f"{slug}.md" for slug in unique_slugs)

def resolve_steering_profile(
    steering_dir: Path | None, resolved_model: str
) -> SteeringProfile | None:
    """Return the first matching profile path, even when that file is malformed."""
    try:
        if steering_dir is None:
            return None
        for candidate in steering_profile_candidates(steering_dir, resolved_model):
            if candidate.is_file():
                return load_steering_profile(candidate)
    except Exception:
        return None
    return None

def steering_worker_rules(
    profile: SteeringProfile, *, inject_candidates: bool = True
) -> tuple[SteeringRule, ...]:
    try:
        return tuple(
            rule
            for rule in profile.rules
            if rule.audience == "worker"
            and (
                rule.status in {"confirmed", "stale-pending-reverify"}
                or (rule.status == "candidate" and inject_candidates)
            )
        )
    except Exception:
        return ()

def inject_steering_spec(
    spec: str,
    profile: SteeringProfile | None,
    *,
    inject_candidates: bool = True,
) -> tuple[str, tuple[str, ...]]:
    """Prepend qualifying worker rules, returning the original spec on any error."""
    try:
        if profile is None:
            return spec, ()
        rules = steering_worker_rules(profile, inject_candidates=inject_candidates)
        if not rules:
            return spec, ()
        lines = [
            f"[Steering profile {profile.model} v{profile.profile_version} — auto-injected by ringer.py]"
        ]
        for rule in rules:
            prefix = ""
            if rule.status == "candidate":
                prefix = "(candidate) "
            elif rule.status == "stale-pending-reverify":
                prefix = "(unverified on current model version) "
            lines.append(f"- {prefix}{rule.inject}")
        lines.append("[End steering profile]")
        return "\n".join(lines) + "\n\n" + spec, tuple(rule.id for rule in rules)
    except Exception:
        return spec, ()
