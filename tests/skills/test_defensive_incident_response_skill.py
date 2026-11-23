from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "customizations/skills/defensive-incident-response/SKILL.md"


def test_skill_metadata_sections_and_scope_are_publishable():
    text = SKILL.read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]
    description = re.search(r"^description: (.+)$", frontmatter, re.MULTILINE).group(1)

    assert len(description) <= 60
    assert description.endswith(".")
    assert re.search(r"^author: Reynaldo Rodriguez \(Rey\); Hermes Agent$", frontmatter, re.MULTILINE)
    assert re.search(r"^license: MIT$", frontmatter, re.MULTILINE)
    assert re.search(r"^platforms: \[linux\]$", frontmatter, re.MULTILINE)

    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)
    assert headings == [
        "When to Use", "Prerequisites", "How to Run", "Quick Reference",
        "Procedure", "Pitfalls", "Verification",
    ]
    assert all(f"`{tool}`" in text for tool in ("terminal", "read_file", "search_files"))
    assert "no ejecuta una respuesta automatizada" in text


def test_skill_contains_no_machine_local_or_credential_material():
    text = SKILL.read_text(encoding="utf-8")
    assert "/" + "home" + "/" not in text
    assert not re.search(r"[A-Za-z]:\\Users\\", text)
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    assert not re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", text)
    assert not re.search(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b", text)
