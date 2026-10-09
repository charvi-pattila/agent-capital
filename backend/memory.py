from core import DATA_DIR, project_dir


MEMORY_FILES = {
    "last_session": "memory/last_session.md",
    "errors": "memory/errors.md",
    "skills": "memory/skills.md",
}

# Global memory: one free-form note shared across every project (unlike per-project
# memory, "shared across all projects" doesn't need the last_session/errors/skills
# taxonomy — it's a single running note, e.g. "always use tabs not spaces").
GLOBAL_MEMORY_DIR = DATA_DIR.parent / "global_memory"
GLOBAL_MEMORY_PATH = GLOBAL_MEMORY_DIR / "notes.md"


def get_global_memory():
    GLOBAL_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return GLOBAL_MEMORY_PATH.read_text() if GLOBAL_MEMORY_PATH.exists() else ""


def set_global_memory(content):
    GLOBAL_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    GLOBAL_MEMORY_PATH.write_text(content)


def get_memory(pid):
    base = project_dir(pid)
    result = {}
    for key, rel in MEMORY_FILES.items():
        p = base / rel
        result[key] = p.read_text() if p.exists() else ""

    comp_dir = base / "memory" / "components"
    components = []
    if comp_dir.exists():
        for f in sorted(comp_dir.iterdir()):
            if f.suffix == ".md":
                components.append({"name": f.stem, "content": f.read_text()})

    return {"memory": result, "components": components}


def set_memory(pid, memory_type, content, component=None):
    base = project_dir(pid)
    if component:
        p = base / "memory" / "components" / f"{component}.md"
    elif memory_type in MEMORY_FILES:
        p = base / MEMORY_FILES[memory_type]
    else:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return True


def build_context_prompt(pid):
    mem = get_memory(pid)
    lines = ["# Project Context - Read this to know where we left off\n"]

    global_note = get_global_memory()
    if global_note:
        lines.append(f"## Global Memory (shared across all projects)\n{global_note}\n")
    if mem["memory"]["last_session"]:
        lines.append(f"## Last Session\n{mem['memory']['last_session']}\n")
    if mem["memory"]["errors"]:
        lines.append(f"## Known Errors & Fixes\n{mem['memory']['errors']}\n")
    if mem["memory"]["skills"]:
        lines.append(f"## Skills & Patterns\n{mem['memory']['skills']}\n")
    for c in mem["components"]:
        if c["content"]:
            lines.append(f"## Component: {c['name']}\n{c['content']}\n")

    if len(lines) == 1:
        return None
    return "\n".join(lines)
