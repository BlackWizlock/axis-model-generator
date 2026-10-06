"""Report contract; a technical result never implies regulatory approval."""

from dataclasses import asdict, dataclass, field
import json

from . import __version__


@dataclass
class Finding:
    rule_id: str
    status: str
    file: str
    actual: object
    expected: object
    message: str
    scope: str = "technical"

    def __post_init__(self):
        if self.status not in {"pass", "fail", "warn", "not_checked"}:
            raise ValueError("Invalid finding status")
        if self.scope not in {"technical", "profile", "procedure", "external"}:
            raise ValueError("Invalid finding scope")


@dataclass
class Report:
    input_sha256: str
    profile: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    schema_version: int = 1
    tool_version: str = __version__

    def has_failures(self):
        return any(item.status == "fail" for item in self.findings)

    def to_json(self):
        result = asdict(self)
        result["files"] = sorted(result["files"], key=lambda item: item.get("file", ""))
        result["findings"] = sorted(result["findings"], key=lambda item: (
            item["file"], item["scope"], item["rule_id"], item["message"]))
        return json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
