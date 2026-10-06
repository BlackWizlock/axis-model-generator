"""Public technical research thresholds; regulatory applicability is not checked."""

from .diagnostics import Finding

PROFILE = {"id": "technical-research-v1", "status": "research",
           "source": "public technical demonstration thresholds",
           "applicability": "not_checked", "normative_readiness": False}


def check_image(image, filename):
    findings = []
    def check(rule, status, actual, expected, message):
        findings.append(Finding(rule, status, filename, actual, expected, message, "profile"))
    width, height = image["width"], image["height"]
    if width == height == 128:
        status = "not_checked"
        message = "Research 128 pixel stub exception requires material classification"
    else:
        status = "pass" if width == height and width in {256, 512, 1024, 2048} else "fail"
        message = "Research baseline for texture dimensions"
    check("profile.png_dimensions", status, [width, height], [256, 512, 1024, 2048], message)
    check("profile.png_depth", "pass" if image["bit_depth"] == 8 else "fail", image["bit_depth"], 8, "PNG bit depth")
    check("profile.png_alpha", "fail" if image["alpha"] else "pass", image["alpha"], False, "Research baseline excludes alpha")
    size = image["bytes"]
    status = "pass" if size <= 3000000 else "not_checked" if size <= 3 * 1024**2 else "fail"
    check("profile.png_size", status, size, "3 MB; decimal/binary unit not confirmed", "Texture file size")
    return findings
