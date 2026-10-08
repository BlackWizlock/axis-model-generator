"""Bound strict JSON diagnostics without source paths or regulatory promotion."""
import json
import math
import re


REMOVED = 'unsafe_source_value_removed'
SURROGATE = re.compile(r'[\ud800-\udfff]')
# A slash inside a relative logical path is safe; punctuation surrounding a
# source token is not part of that path. Drive/UNC/URL forms are intrinsically
# absolute, including when embedded in a validator-provided model name.
SOURCE = re.compile(r'(?<![\w./\\~-])/|[A-Za-z]:[\\/]|\\\\|(?<![a-zA-Z0-9+.-])[a-zA-Z][a-zA-Z0-9+.-]*://')


def sanitize_report(report: dict, *, max_bytes: int = 4194304, max_findings: int = 10000) -> dict:
    if not isinstance(report, dict) or isinstance(max_bytes, bool) or max_bytes < 256 or max_bytes > 4194304:
        raise ValueError('Invalid report budget')
    if isinstance(max_findings, bool) or not 1 <= max_findings <= 10000:
        raise ValueError('Invalid findings budget')
    removed = 0
    truncated = False
    seen = set()

    def clean(value, depth):
        nonlocal removed, truncated
        if depth > 16:
            truncated = True
            return 'report_depth_limit'
        if isinstance(value, str):
            if SURROGATE.search(value) or SOURCE.search(value):
                removed += 1
                return REMOVED
            data = value.encode('utf-8')
            if len(data) > 4096:
                truncated = True
                return data[:4096].decode('utf-8', errors='ignore')
            return value
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            truncated = True
            return 'non_finite_value_removed'
        if isinstance(value, (dict, list, tuple)):
            if id(value) in seen:
                truncated = True
                return 'report_cycle_removed'
            seen.add(id(value))
            try:
                if isinstance(value, dict):
                    output = {}
                    for key, item in value.items():
                        if not isinstance(key, str):
                            truncated = True
                            continue
                        safe_key = clean(key, depth + 1)
                        if safe_key in output:
                            truncated = True
                            continue
                        output[safe_key] = clean(item, depth + 1)
                    return output
                return [clean(item, depth + 1) for item in value]
            finally:
                seen.remove(id(value))
        truncated = True
        return 'unsupported_value_removed'

    result = clean(report, 0)
    findings = result.get('findings', [])
    if not isinstance(findings, list):
        raise ValueError('Invalid findings')
    original_count = len(findings)
    result['findings'] = findings[:max_findings]
    truncated = truncated or original_count > max_findings
    result['report_truncated'] = truncated
    result['original_findings_count'] = original_count
    result['removed_source_values'] = removed
    def size():
        return len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':'), sort_keys=True).encode('utf-8'))
    if size() > max_bytes and result['findings']:
        result['report_truncated'] = True
        # Search a deterministic prefix rather than serialize/pop every tail:
        # 10,000 bounded findings must not cause quadratic encoding work.
        candidates=result['findings']
        low,high=0,len(candidates)
        while low<high:
            middle=(low+high+1)//2
            result['findings']=candidates[:middle]
            if size()<=max_bytes: low=middle
            else: high=middle-1
        result['findings']=candidates[:low]
    if size() > max_bytes:
        # Keep the four axes and research profile intact; omit oversized ancillary data.
        result = {key: result[key] for key in ('schema_version', 'tool_version', 'input_sha256', 'profile', 'coverage') if key in result}
        result.update(findings=[], report_truncated=True, original_findings_count=original_count, removed_source_values=removed)
    if size() > max_bytes:
        raise ValueError('Report cannot fit budget')
    return result
