import json
import os
import re
import urllib.request


def get_arch_version(pkg_name: str) -> str | None:
    """Queries the Arch Linux packages API to get the latest version (fast)."""
    url = f"https://archlinux.org/packages/search/json/?name={pkg_name}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])
            for r in results:
                if r.get("pkgname") == pkg_name:
                    return r.get("pkgver")
    except Exception:
        pass
    return None


def get_latest_upstream_version(pkg_name: str) -> str:
    """Finds the latest stable version of a package upstream."""
    arch_ver = get_arch_version(pkg_name)
    if arch_ver:
        return arch_ver

    # Fallback to Gemini with search grounding
    from google import genai
    from google.genai import types

    if not os.environ.get("GOOGLE_API_KEY") and os.environ.get("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]

    try:
        client = genai.Client()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                f"What is the latest stable upstream version of the package '{pkg_name}'? "
                "Respond with ONLY the version string (e.g. '1.3.1') and nothing else."
            ),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0
            )
        )
        version_text = response.text.strip()
        match = re.search(r"([0-9]+(?:\.[0-9]+)+[a-z]?)", version_text)
        if match:
            return match.group(1)
        return version_text
    except Exception:
        return "Unknown"


def pkgbuild_exists_on_arch(pkg_name: str) -> bool:
    """Checks if a PKGBUILD for the given package exists on the Arch raw repository."""
    url = f"https://gitlab.archlinux.org/archlinux/packaging/packages/{pkg_name}/-/raw/main/PKGBUILD"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        try:
            req_get = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req_get, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

