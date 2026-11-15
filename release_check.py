"""
Before a release: check that app/version.py has moved on from every release so far, and print the version.

Releases are the git tags v<version> (.github/workflows/release.yml makes one for each). Readers and BBMS
tell firmware apart by the version alone, so two releases must never share one, and a reader should never
be offered something that sorts below what it has.

    python release_check.py           the version must be higher than every tag's
    python release_check.py v1.2.0    for a release from that tag: the tag must be this version's, and the
                                      version higher than every other tag's
"""

import re
import subprocess
import sys


def number(version):
    """ "1.2.10" as (1, 2, 10), or None if it isn't numbers and dots."""
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        return None
    return tuple(int(part) for part in version.split("."))


def problem(version, tags, tag=None):
    """Why this version can't be released, or None if it can. tags: every tag in the repository."""
    if number(version) is None:
        return 'the version in app/version.py is "%s": it must be numbers and dots, like 1.2.0' % version
    if tag and tag != "v" + version:
        return "the tag is %s but app/version.py says %s: the tag should be v%s" % (tag, version, version)
    released = [t[1:] for t in tags if t != tag and t.startswith("v") and number(t[1:]) is not None]
    if version in released:
        return "version %s has been released already: change VERSION in app/version.py" % version
    newest = max(released, key=number, default=None)
    if newest and number(version) <= number(newest):
        return "version %s is not higher than %s, which has been released: change VERSION in app/version.py" % (
            version,
            newest,
        )
    return None


def main():
    with open("app/version.py") as f:
        version = re.search(r'^VERSION = "([^"]+)"', f.read(), re.MULTILINE).group(1)
    tags = subprocess.run(["git", "tag", "--list"], capture_output=True, text=True, check=True).stdout.split()
    tag = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
    why = problem(version, tags, tag)
    if why:
        raise SystemExit("Not released: " + why)
    print(version)


if __name__ == "__main__":
    main()
