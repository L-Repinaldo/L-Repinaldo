
from __future__ import annotations

import base64
import json
import os
import re
import tomllib
import xml.sax.saxutils as xml_utils

from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import requests
import yaml


GITHUB_API = "https://api.github.com"

USERNAME = os.getenv("GITHUB_USERNAME", "L-Repinaldo")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

BASE_DIR = Path(__file__).resolve().parent.parent

TECHNOLOGIES_FILE = BASE_DIR / "config" / "technologies.yaml"
OUTPUT_DIR = BASE_DIR / "assets"
RANKING_FILE = OUTPUT_DIR / "tech-ranking.svg"

IGNORE_ARCHIVED = True
IGNORE_FORKS = True

TOP_TECHNOLOGIES = 3
MAX_FILES_PER_REPOSITORY = 100

WEIGHTS = {
    "repository": 5,
    "extension": 2,
    "dependency": 3,
    "import": 2,
}

DEPENDENCY_FILES = {
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements-prod.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}

SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx", ".mts", ".cts", ".java",
    ".kt", ".kts", ".ipynb",
}

session = requests.Session()

session.headers.update({
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})

if GITHUB_TOKEN:
    session.headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

def load_technologies() -> dict:

    with TECHNOLOGIES_FILE.open(encoding="utf-8") as file:
        technologies = yaml.safe_load(file)

    if not isinstance(technologies, dict) or not technologies:
        raise ValueError(
            f"Invalid or empty configuration: {TECHNOLOGIES_FILE}"
        )

    for name, config in technologies.items():
        if not isinstance(config, dict):
            raise ValueError(f"Invalid configuration for {name}")

        for key in ("extensions", "dependencies", "imports"):
            values = config.get(key, [])

            if not isinstance(values, list):
                raise ValueError(f"{name}.{key} must be a list")

            config[key] = [
                str(value).strip().lower()
                for value in values
            ]

    return technologies


TECHNOLOGIES = load_technologies()



def github_get(url: str, **kwargs) -> dict | list:

    response = session.get(url, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json()


def get_repositories() -> list[dict]:

    repositories = []
    page = 1

    while True:
        data = github_get(
            f"{GITHUB_API}/users/{USERNAME}/repos",
            params={
                "per_page": 100,
                "page": page,
                "type": "owner",
                "sort": "updated",
            },
        )

        if not data:
            break

        repositories.extend(data)
        page += 1

    return [
        repo
        for repo in repositories
        if not (
            (IGNORE_FORKS and repo["fork"])
            or (IGNORE_ARCHIVED and repo["archived"])
        )
    ]


def get_repository_tree(repo: dict) -> list[dict]:

    owner = repo["owner"]["login"]
    name = repo["name"]
    branch = quote(repo["default_branch"], safe="")

    data = github_get(
        f"{GITHUB_API}/repos/{owner}/{name}/git/trees/{branch}",
        params={"recursive": "1"},
    )

    if data.get("truncated"):
        print(f"Warning: tree truncated for {owner}/{name}")

    return [
        item
        for item in data.get("tree", [])
        if item.get("type") == "blob"
    ]


def get_file_content(repo: dict, path: str) -> str | None:

    owner = repo["owner"]["login"]
    name = repo["name"]
    encoded_path = quote(path, safe="/")

    try:
        data = github_get(
            f"{GITHUB_API}/repos/{owner}/{name}/contents/{encoded_path}"
        )

        if data.get("encoding") != "base64":
            return None

        raw = base64.b64decode(data["content"], validate=False)
        return raw.decode("utf-8", errors="replace")

    except (requests.RequestException, ValueError, KeyError) as error:
        print(f"Warning: could not read {repo['full_name']}/{path}: {error}")
        return None


def normalize_dependency(value: str) -> str:

    value = value.strip().lower()

    value = re.split(r"[<>=!~;\[]", value, maxsplit=1)[0].strip()

    return re.sub(r"[-_.]+", "-", value)


def extract_dependencies(files: list[tuple[str, str]]) -> set[str]:

    dependencies = set()

    def add_dependency(value: str) -> None:
        name = normalize_dependency(value)

        if name:
            dependencies.add(name)

    for path, content in files:
        filename = Path(path).name.lower()

        if filename.startswith("requirements") and filename.endswith(".txt"):
            for line in content.splitlines():
                line = line.strip()

                if not line or line.startswith(("#", "-")):
                    continue

                if "://" in line or line.startswith(("git+", "-e ")):
                    continue

                add_dependency(line)

        elif filename == "pyproject.toml":
            try:
                project = tomllib.loads(content)

                for value in project.get("project", {}).get(
                    "dependencies", []
                ):
                    add_dependency(re.split(r"[<>=!~;\[]", value, 1)[0])

                optional = project.get("project", {}).get(
                    "optional-dependencies", {}
                )

                for values in optional.values():
                    for value in values:
                        add_dependency(
                            re.split(r"[<>=!~;\[]", value, 1)[0]
                        )

                poetry = project.get("tool", {}).get("poetry", {})
                for name in poetry.get("dependencies", {}):
                    if name.lower() != "python":
                        add_dependency(name)

                for group in poetry.get("group", {}).values():
                    for name in group.get("dependencies", {}):
                        add_dependency(name)

            except (tomllib.TOMLDecodeError, TypeError, AttributeError):
                print(f"Warning: invalid TOML file: {path}")

        elif filename == "package.json":
            try:
                package = json.loads(content)

                for section in (
                    "dependencies",
                    "devDependencies",
                    "peerDependencies",
                    "optionalDependencies",
                ):
                    values = package.get(section, {})

                    if isinstance(values, dict):
                        for name in values:
                            add_dependency(name)

            except (json.JSONDecodeError, TypeError):
                print(f"Warning: invalid JSON file: {path}")

        elif filename in {"package-lock.json", "npm-shrinkwrap.json"}:
            try:
                lock = json.loads(content)

                for name in lock.get("packages", {}):
                    if name.startswith("node_modules/"):
                        package_name = name[len("node_modules/"):]

                        # Scoped packages can contain another slash.
                        if package_name.startswith("@"):
                            add_dependency(package_name)
                        elif "/" not in package_name:
                            add_dependency(package_name)

            except (json.JSONDecodeError, TypeError):
                print(f"Warning: invalid lock file: {path}")

        elif filename == "yarn.lock":
            # Yarn v1 lockfile entries start with package names.
            for line in content.splitlines():
                if not line or line[0].isspace() or not line.endswith(":"):
                    continue

                for entry in line[:-1].split(","):
                    entry = entry.strip().strip('"').strip("'")
                    add_dependency(re.split(r"@", entry, 1)[0]
                                   if not entry.startswith("@")
                                   else entry.rsplit("@", 1)[0])

        elif filename == "pnpm-lock.yaml":
            # Handles common pnpm v6+ importer and package keys.
            try:
                lock = yaml.safe_load(content) or {}

                importers = lock.get("importers", {})
                if isinstance(importers, dict):
                    for importer in importers.values():
                        for section in (
                            "dependencies",
                            "devDependencies",
                            "optionalDependencies",
                        ):
                            values = importer.get(section, {})
                            if isinstance(values, dict):
                                for name in values:
                                    add_dependency(name)

                packages = lock.get("packages", {})
                if isinstance(packages, dict):
                    for key in packages:
                        # Examples: /react@18.2.0 and
                        # /@scope/package@1.0.0
                        key = str(key).lstrip("/")
                        match = re.match(
                            r"(@[^/]+/[^@]+|[^@/]+)@",
                            key,
                        )
                        if match:
                            add_dependency(match.group(1))

            except (yaml.YAMLError, AttributeError, TypeError):
                print(f"Warning: invalid pnpm lock file: {path}")

    return dependencies



PYTHON_IMPORT_PATTERN = re.compile(
    r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_.]*)",
    re.MULTILINE,
)

JS_IMPORT_PATTERNS = [
    re.compile(r"""^\s*import\s+(?:.*?\s+from\s+)?["']([^"']+)["']""",
               re.MULTILINE),
    re.compile(r"""^\s*export\s+.*?\s+from\s+["']([^"']+)["']""",
               re.MULTILINE),
    re.compile(r"""require\(\s*["']([^"']+)["']\s*\)"""),
    re.compile(r"""import\(\s*["']([^"']+)["']\s*\)"""),
]


def detect_imports(content: str, extension: str) -> set[str]:
    """Detect import names for Python, JavaScript and TypeScript."""

    imports = set()
    extension = extension.lower()

    if extension == ".py":
        for match in PYTHON_IMPORT_PATTERN.finditer(content):
            imports.add(match.group(1).split(".")[0].lower())

    elif extension in {
        ".js", ".jsx", ".mjs", ".cjs",
        ".ts", ".tsx", ".mts", ".cts",
    }:
        for pattern in JS_IMPORT_PATTERNS:
            for match in pattern.finditer(content):
                module = match.group(1)

                # Ignore relative and absolute filesystem imports.
                if module.startswith((".", "/", "#")):
                    continue

                # Extract package name from scoped and unscoped imports.
                parts = module.split("/")
                package = "/".join(parts[:2]) if module.startswith("@") else parts[0]
                imports.add(package.lower())

    return imports



def analyze_repository(repo: dict) -> dict:
    """Analyze technology evidence within one repository."""

    full_name = repo["full_name"]
    print(f"Analyzing: {full_name}")

    tree = get_repository_tree(repo)
    files = [item["path"] for item in tree]

    # Count extensions separately. ".js" and ".java" never overlap.
    extension_counts = defaultdict(int)

    for path in files:
        extension_counts[Path(path).suffix.lower()] += 1

    candidate_files = []

    for path in files:
        filename = Path(path).name.lower()
        extension = Path(path).suffix.lower()

        if filename in DEPENDENCY_FILES:
            candidate_files.append(path)
        elif extension in SOURCE_EXTENSIONS:
            candidate_files.append(path)

    # Prioritize manifests, then source files.
    candidate_files.sort(
        key=lambda path: (
            Path(path).name.lower() not in DEPENDENCY_FILES,
            path,
        )
    )
    candidate_files = candidate_files[:MAX_FILES_PER_REPOSITORY]

    contents = []

    for path in candidate_files:
        content = get_file_content(repo, path)

        if content is not None:
            contents.append((path, content))

    dependencies = extract_dependencies(contents)

    all_imports = set()

    for path, content in contents:
        extension = Path(path).suffix.lower()
        all_imports.update(detect_imports(content, extension))

    scores = defaultdict(float)
    evidence = defaultdict(
        lambda: {
            "repositories": set(),
            "extensions": 0,
            "dependencies": 0,
            "imports": 0,
        }
    )

    for technology, config in TECHNOLOGIES.items():
        extensions = set(config.get("extensions", []))
        technology_dependencies = set(config.get("dependencies", []))
        technology_imports = set(config.get("imports", []))

        extension_matches = sum(
            count
            for extension, count in extension_counts.items()
            if extension in extensions
        )

        matched_dependencies = dependencies & technology_dependencies
        matched_imports = all_imports & technology_imports

    
        if extension_matches:
            scores[technology] += WEIGHTS["repository"]
            scores[technology] += (
                min(extension_matches, 10) * WEIGHTS["extension"]
            )
            evidence[technology]["extensions"] += extension_matches

        scores[technology] += (
            len(matched_dependencies) * WEIGHTS["dependency"]
        )
        evidence[technology]["dependencies"] += len(matched_dependencies)

        scores[technology] += (
            len(matched_imports) * WEIGHTS["import"]
        )
        evidence[technology]["imports"] += len(matched_imports)

        detected = (
            extension_matches > 0
            or bool(matched_dependencies)
            or bool(matched_imports)
        )

        if detected:
            evidence[technology]["repositories"].add(full_name)

    return {
        "scores": scores,
        "evidence": evidence,
    }


def build_ranking(repositories: list[dict]) -> list[dict]:
    """Aggregate repository scores into a technology ranking."""

    total_scores = defaultdict(float)
    total_evidence = defaultdict(
        lambda: {
            "repositories": set(),
            "extensions": 0,
            "dependencies": 0,
            "imports": 0,
        }
    )

    for repo in repositories:
        result = analyze_repository(repo)

        for technology, score in result["scores"].items():
            total_scores[technology] += score

        for technology, data in result["evidence"].items():
            total_evidence[technology]["repositories"].update(
                data["repositories"]
            )

            for key in ("extensions", "dependencies", "imports"):
                total_evidence[technology][key] += data[key]

    ranking = []

    for technology, score in total_scores.items():
        evidence = total_evidence[technology]

        ranking.append({
            "technology": technology,
            "score": round(score, 2),
            "repositories": len(evidence["repositories"]),
            "extensions": evidence["extensions"],
            "dependencies": evidence["dependencies"],
            "imports": evidence["imports"],
        })

    ranking.sort(
        key=lambda item: (
            item["score"],
            item["repositories"],
            item["technology"],
        ),
        reverse=True,
    )

    return ranking


def escape_xml(value: str) -> str:
    """Escape text before inserting it into SVG."""

    return xml_utils.escape(value, {'"': "&quot;", "'": "&apos;"})


def generate_svg(ranking: list[dict]) -> None:
    """Generate an SVG card with the top technologies."""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    top = ranking[:TOP_TECHNOLOGIES]

    width = 900
    row_height = 64
    height = 100 + row_height * max(len(top), 1)

    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg"',
        f'     width="{width}" height="{height}"',
        f'     viewBox="0 0 {width} {height}">',
        f'  <rect width="{width}" height="{height}" rx="12" fill="#0d1117"/>',
        '  <text x="32" y="42" fill="#8b949e"',
        '        font-family="monospace" font-size="18">',
        '    Most used technologies',
        '  </text>',
    ]

    if not top:
        svg.extend([
            '  <text x="32" y="90" fill="#8b949e"',
            '        font-family="monospace" font-size="14">',
            '    No technologies detected',
            '  </text>',
        ])

    max_score = max((item["score"] for item in top), default=1) or 1

    for index, item in enumerate(top):
        y = 76 + index * row_height
        label = escape_xml(item["technology"])
        score = item["score"]
        bar_width = int(400 * score / max_score)

        svg.extend([
            f'  <text x="32" y="{y + 16}" fill="#c9d1d9"',
            '        font-family="monospace" font-size="15">',
            f'    {label}',
            '  </text>',
            f'  <rect x="32" y="{y + 25}" width="400" height="10"',
            '        rx="5" fill="#21262d"/>',
            f'  <rect x="32" y="{y + 25}" width="{bar_width}" height="10"',
            '        rx="5" fill="#7ee787"/>',
            f'  <text x="455" y="{y + 34}" fill="#8b949e"',
            '        font-family="monospace" font-size="13">',
            f'    {score:g} pts',
            '  </text>',
        ])

    svg.append("</svg>")

    RANKING_FILE.write_text("\n".join(svg), encoding="utf-8")



def main() -> None:
    print(f"Collecting repositories for {USERNAME}...")

    try:
        repositories = get_repositories()
        print(f"Found {len(repositories)} repositories.")

        ranking = build_ranking(repositories)
        generate_svg(ranking)

        print("\nTop technologies:\n")

        for item in ranking[:10]:
            print(
                f"{item['technology']:<20}"
                f"{item['score']:>7g} points | "
                f"{item['repositories']} repositories"
            )

        print(f"\nSVG saved to: {RANKING_FILE}")

    except requests.RequestException as error:
        print(f"GitHub API error: {error}")
        raise
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Configuration or file error: {error}")
        raise


if __name__ == "__main__":
    main()
