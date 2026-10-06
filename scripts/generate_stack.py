from __future__ import annotations

import base64
import json
import os
import re
from collections import defaultdict
from pathlib import Path

import requests
import yaml

from dotenv import load_dotenv


#load_dotenv()


GITHUB_API = "https://api.github.com"

USERNAME = os.getenv(
    "GITHUB_USERNAME",
    "L-Repinaldo",
)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

BASE_DIR = Path(__file__).resolve().parent.parent

TECHNOLOGIES_FILE = (
    BASE_DIR / "config" / "technologies.yaml"
)

OUTPUT_DIR = BASE_DIR / "assets"

RANKING_FILE = (
    OUTPUT_DIR / "tech-ranking.svg"
)

IGNORE_ARCHIVED = True
IGNORE_FORKS = True

TOP_TECHNOLOGIES = 3


WEIGHTS = {
    "repository": 5,
    "extension": 2,
    "dependency": 3,
    "import": 2,
}


session = requests.Session()

headers = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

if GITHUB_TOKEN:
    headers["Authorization"] = (
        f"Bearer {GITHUB_TOKEN}"
    )

session.headers.update(headers)


def load_technologies() -> dict:
    """Load technology definitions from YAML."""

    with TECHNOLOGIES_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        return yaml.safe_load(file)


TECHNOLOGIES = load_technologies()


def github_get(url: str, **kwargs):
    """Perform a GET request against GitHub API."""

    response = session.get(
        url,
        timeout=30,
        **kwargs,
    )

    response.raise_for_status()

    return response.json()


def get_repositories() -> list[dict]:
    """Return repositories owned by the user."""

    repositories = []
    page = 1

    while True:
        url = (
            f"{GITHUB_API}/users/"
            f"{USERNAME}/repos"
        )

        data = github_get(
            url,
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
            or (
                IGNORE_ARCHIVED
                and repo["archived"]
            )
        )
    ]


def get_repository_tree(
    repo: dict,
) -> list[dict]:
    """Return repository files."""

    owner = repo["owner"]["login"]
    name = repo["name"]
    branch = repo["default_branch"]

    url = (
        f"{GITHUB_API}/repos/"
        f"{owner}/{name}/git/trees/{branch}"
    )

    data = github_get(
        url,
        params={"recursive": "1"},
    )

    if data.get("truncated"):
        print(
            f"Warning: repository tree truncated: "
            f"{owner}/{name}"
        )

    return [
        item
        for item in data.get("tree", [])
        if item["type"] == "blob"
    ]


def get_file_content(
    repo: dict,
    path: str,
) -> str | None:
    """Return text content of a repository file."""

    owner = repo["owner"]["login"]
    name = repo["name"]

    url = (
        f"{GITHUB_API}/repos/"
        f"{owner}/{name}/contents/{path}"
    )

    try:
        data = github_get(url)

        if data.get("encoding") != "base64":
            return None

        return base64.b64decode(
            data["content"]
        ).decode(
            "utf-8",
            errors="ignore",
        )

    except requests.RequestException:
        return None


def normalize_dependency(
    value: str,
) -> str:
    """Normalize a dependency name."""

    value = value.strip().lower()

    return re.split(
        r"[<>=!~;\[\] ]",
        value,
        maxsplit=1,
    )[0]


def extract_dependencies(
    files: list[tuple[str, str]],
) -> set[str]:
    """Extract dependencies from project files."""

    dependencies = set()

    for path, content in files:
        filename = Path(path).name.lower()

        if filename in {
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-test.txt",
        }:
            for line in content.splitlines():
                line = line.strip()

                if (
                    not line
                    or line.startswith("#")
                ):
                    continue

                dependency = normalize_dependency(
                    line
                )

                if dependency:
                    dependencies.add(
                        dependency
                    )

        elif filename == "pyproject.toml":
            for match in re.finditer(
                r"""["']([A-Za-z0-9_.-]+)""",
                content,
            ):
                dependencies.add(
                    normalize_dependency(
                        match.group(1)
                    )
                )

        elif filename == "package.json":
            try:
                package = json.loads(
                    content
                )

                for section in (
                    "dependencies",
                    "devDependencies",
                ):
                    for dependency in package.get(
                        section,
                        {},
                    ):
                        dependencies.add(
                            normalize_dependency(
                                dependency
                            )
                        )

            except json.JSONDecodeError:
                continue

    return dependencies


def detect_imports(
    content: str,
) -> set[str]:
    """Detect Python-style imports."""

    imports = set()

    pattern = (
        r"^\s*(?:from|import)\s+"
        r"([A-Za-z0-9_.]+)"
    )

    for match in re.finditer(
        pattern,
        content,
        re.MULTILINE,
    ):
        module = match.group(1).split(".")[0]

        imports.add(module.lower())

    return imports


def analyze_repository(
    repo: dict,
) -> dict:
    """Analyze a repository."""

    print(
        f"Analyzing: "
        f"{repo['full_name']}"
    )

    tree = get_repository_tree(repo)

    files = [
        item["path"]
        for item in tree
    ]

    scores = defaultdict(float)

    evidence = defaultdict(
        lambda: {
            "repositories": set(),
            "extensions": 0,
            "dependencies": 0,
            "imports": 0,
        }
    )

    candidate_files = []

    dependency_files = {
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "pyproject.toml",
        "package.json",
    }

    for path in files:
        filename = Path(path).name.lower()
        extension = Path(path).suffix.lower()

        if filename in dependency_files:
            candidate_files.append(path)
            continue

        for config in TECHNOLOGIES.values():
            if extension in config.get(
                "extensions",
                [],
            ):
                candidate_files.append(path)
                break

    candidate_files = candidate_files[:100]

    contents = []

    for path in candidate_files:
        content = get_file_content(
            repo,
            path,
        )

        if content is not None:
            contents.append(
                (path, content)
            )

    dependencies = extract_dependencies(
        contents
    )

    normalized_dependencies = {
        normalize_dependency(dep)
        for dep in dependencies
    }

    all_imports = set()

    for path, content in contents:
        if (
            Path(path).suffix.lower()
            == ".py"
        ):
            all_imports.update(
                detect_imports(content)
            )

    for technology, config in TECHNOLOGIES.items():
        repo_detected = False

        extensions = config.get(
            "extensions",
            [],
        )

        technology_dependencies = (
            config.get(
                "dependencies",
                [],
            )
        )

        technology_imports = config.get(
            "imports",
            [],
        )

        extension_matches = sum(
            Path(path).suffix.lower()
            in extensions
            for path in files
        )

        if extension_matches:
            scores[technology] += (
                WEIGHTS["repository"]
            )

            scores[technology] += (
                min(
                    extension_matches,
                    10,
                )
                * WEIGHTS["extension"]
            )

            evidence[technology][
                "repositories"
            ].add(
                repo["full_name"]
            )

            evidence[technology][
                "extensions"
            ] += extension_matches

            repo_detected = True

        for dependency in (
            technology_dependencies
        ):
            if (
                dependency.lower()
                in normalized_dependencies
            ):
                scores[technology] += (
                    WEIGHTS["dependency"]
                )

                evidence[technology][
                    "dependencies"
                ] += 1

                repo_detected = True

        for imported in technology_imports:
            if (
                imported.lower()
                in all_imports
            ):
                scores[technology] += (
                    WEIGHTS["import"]
                )

                evidence[technology][
                    "imports"
                ] += 1

                repo_detected = True

        if repo_detected:
            evidence[technology][
                "repositories"
            ].add(
                repo["full_name"]
            )

    return {
        "scores": scores,
        "evidence": evidence,
    }


def build_ranking(
    repositories: list[dict],
) -> list[dict]:
    """Build technology ranking."""

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

        for technology, score in result[
            "scores"
        ].items():
            total_scores[technology] += score

        for technology, data in result[
            "evidence"
        ].items():
            total_evidence[technology][
                "repositories"
            ].update(
                data["repositories"]
            )

            total_evidence[technology][
                "extensions"
            ] += data["extensions"]

            total_evidence[technology][
                "dependencies"
            ] += data["dependencies"]

            total_evidence[technology][
                "imports"
            ] += data["imports"]

    ranking = []

    for technology, score in (
        total_scores.items()
    ):
        evidence = total_evidence[
            technology
        ]

        ranking.append(
            {
                "technology": technology,
                "score": round(score, 2),
                "repositories": len(
                    evidence[
                        "repositories"
                    ]
                ),
                "extensions": evidence[
                    "extensions"
                ],
                "dependencies": evidence[
                    "dependencies"
                ],
                "imports": evidence[
                    "imports"
                ],
            }
        )

    ranking.sort(
        key=lambda item: (
            item["score"],
            item["repositories"],
        ),
        reverse=True,
    )

    return ranking


def escape_xml(
    value: str,
) -> str:
    """Escape text for SVG."""

    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def generate_svg(
    ranking: list[dict],
) -> None:
    """Generate the technology ranking SVG."""

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    top = ranking[:TOP_TECHNOLOGIES]

    width = 900
    height = 280

    positions = [
        (180, 155),
        (450, 95),
        (720, 155),
    ]

    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg"',
        f'     width="{width}"',
        f'     height="{height}"',
        f'     viewBox="0 0 {width} {height}">',
        "",
        f'  <rect width="{width}" '
        f'height="{height}" '
        f'fill="#0d1117"/>',
        "",
        '  <text x="40" y="45"',
        '        fill="#8b949e"',
        '        font-family="monospace"',
        '        font-size="16">',
        "    Most used technologies",
        "  </text>",
        "",
    ]

    for index, item in enumerate(top):
        if index >= len(positions):
            break

        x, y = positions[index]

        technology = escape_xml(
            item["technology"]
        )

        score = item["score"]

        svg.extend(
            [
                f'  <circle cx="{x}" '
                f'cy="{y}" r="58"',
                '          fill="#161b22"',
                '          stroke="#7ee787"',
                '          stroke-width="2"/>',
                "",
                f'  <text x="{x}" '
                f'y="{y - 5}"',
                '        text-anchor="middle"',
                '        fill="#c9d1d9"',
                '        font-family="monospace"',
                '        font-size="15"',
                '        font-weight="bold">',
                f"    {technology}",
                "  </text>",
                "",
                f'  <text x="{x}" '
                f'y="{y + 18}"',
                '        text-anchor="middle"',
                '        fill="#8b949e"',
                '        font-family="monospace"',
                '        font-size="12">',
                f"    {score} pts",
                "  </text>",
                "",
            ]
        )

    svg.append("</svg>")

    RANKING_FILE.write_text(
        "\n".join(svg),
        encoding="utf-8",
    )


def main() -> None:
    print(
        f"Collecting repositories for "
        f"{USERNAME}..."
    )

    repositories = get_repositories()

    print(
        f"Found {len(repositories)} "
        f"repositories."
    )

    ranking = build_ranking(
        repositories
    )

    generate_svg(ranking)

    print("\nTop technologies:\n")

    for item in ranking[:10]:
        print(
            f"{item['technology']:<20}"
            f"{item['score']:>6} points | "
            f"{item['repositories']} "
            f"repositories"
        )

    print(
        f"\nSVG saved to: "
        f"{RANKING_FILE}"
    )


if __name__ == "__main__":
    main()
